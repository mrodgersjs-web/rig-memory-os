"""RIG Memory OS v10 — Postgres-backed Cockpit Store (Yellow #3 + #4).

Externalizes cockpit state (control_state, budget) to Postgres so:
- Multiple processes (Hermes session + cron worker) share ONE kill switch
- Audit log persists across restart
- Cockpit state survives restart

Architecture:
    PostgresCockpitStore = durable control plane
    MemoryCockpit (existing) = in-process control plane (still works
                              standalone, e.g. for tests)

    Runtime usage:
        store = PostgresCockpitStore(dsn, audit_writer=None)
        cockpit = MemoryCockpit(store=store)
        cockpit.engage_kill_switch()  # writes to Postgres
        # Other process reads it back via:
        other_cockpit = MemoryCockpit(store=store)
        assert other_cockpit.is_killed()  # reads from Postgres

Per Opus 5 #7 (round-3), the in-memory RLock still wraps local state
to avoid local torn reads; the Postgres store handles cross-process
synchronization.

Phase 3 (F3/F4):
- MemoryCockpit read-through: state is re-read from this store on every
  control read once `store_read_ttl` (default 0.25 s) has elapsed, so a
  kill engaged by another PROCESS propagates. Bounded staleness, not
  instant: LISTEN/NOTIFY is out of scope for the single-host pilot.
- Every mutating method bootstraps the singleton cockpit_state row
  (INSERT ... ON CONFLICT DO NOTHING) so a fresh deployment cannot
  silently drop a set_budget/adjust_budget.
- Reads run in an explicitly terminated transaction (_read_txn); the
  store no longer leaves connections idle-in-transaction.
- All public methods take `_mutex`. `_lock()` (pg advisory lock) and
  `_mutex` (in-process RLock) are DIFFERENT things; do not merge them.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

try:
    import psycopg
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False
    psycopg = None  # type: ignore


DEFAULT_DSN = "host=/tmp port=5432 dbname=rig_memory_os_phase1 user=rig128gb"

# Phase 3 fix (F4): idempotent singleton-row bootstrap. `cockpit_state` is a
# single-row table (CHECK id = 1); every mutation path must be able to run
# against a freshly deployed database where the row does not exist yet.
_BOOTSTRAP_ROW_SQL = """
INSERT INTO cockpit_state (id, state, budget_remaining, updated_at)
VALUES (1, 'active', 1.0, NOW())
ON CONFLICT (id) DO NOTHING;
"""


@dataclass
class PostgresCockpitStore:
    """Postgres-backed cockpit state store.

    Uses a single connection; writes are advisory-locked via
    pg_advisory_xact_lock to serialize across processes.
    """

    dsn: str = DEFAULT_DSN
    audit_writer: Optional[object] = None  # PostgresWriter for audit_log
    _conn: Optional[object] = field(default=None, init=False, repr=False)
    # Phase 3 fix (F4/C2): psycopg connections are NOT thread-safe and this
    # store shares one. Every public method takes this mutex. NOTE: `_lock`
    # is already taken by the pg_advisory_xact_lock contextmanager below;
    # this is the in-process mutex and is deliberately named differently.
    # Lock ordering is always MemoryCockpit._lock -> PostgresCockpitStore._mutex;
    # the store never calls back into the cockpit, so no cycle is possible.
    _mutex: object = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if not PSYCOPG_AVAILABLE:
            raise RuntimeError(
                "psycopg not installed; cannot use PostgresCockpitStore"
            )

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=False)
        return self._conn

    def close(self) -> None:
        with self._mutex:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    @contextmanager
    def _lock(self):
        """Serialize cockpit state changes via pg_advisory_xact_lock."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            # Lock ID = 42 (arbitrary, unique to cockpit control plane)
            cur.execute("SELECT pg_advisory_xact_lock(42);")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def _read_txn(self):
        """Read-only transaction that always terminates.

        Phase 3 fix (F4/C1): read_state() used to `return` before its
        `commit()`, leaving the connection idle-in-transaction — pinning
        xmin against VACUUM and grafting the next write's
        pg_advisory_xact_lock onto a stale transaction. Rollback is the
        correct terminator for a read: nothing to persist.
        """
        conn = self._get_conn()
        try:
            yield conn
            conn.rollback()
        except Exception:
            conn.rollback()
            raise

    def read_state(self) -> tuple[str, float]:
        """Read current state. Returns (state_value, budget_remaining).

        Returns ('active', 1.0) if no row exists yet (fresh deployment).
        Phase 3 fix (F4/C1): the transaction is always terminated via
        _read_txn — the old code returned before its commit(), leaving the
        connection idle-in-transaction.
        """
        with self._mutex:
            with self._read_txn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT state, budget_remaining
                        FROM cockpit_state
                        WHERE id = 1;
                        """
                    )
                    row = cur.fetchone()
        if row is None:
            return ("active", 1.0)
        return (row[0], float(row[1]))

    def ensure_row(self) -> None:
        """Idempotently create the singleton cockpit_state row.

        Phase 3 fix (F4): set_budget was UPDATE-only, so on a fresh database
        it updated 0 rows and read_state then returned the ('active', 1.0)
        default — a silently lost budget. Mutating paths bootstrap first;
        MemoryCockpit._hydrate_from_store also calls this at init.
        """
        with self._mutex:
            with self._lock() as conn:
                with conn.cursor() as cur:
                    cur.execute(_BOOTSTRAP_ROW_SQL)

    def write_state(
        self, actor: str, action: str,
        before: str, after: str, budget: float,
    ) -> None:
        """Atomic state transition with audit entry.

        Updates cockpit_state + inserts audit_log row in one transaction.
        """
        with self._mutex:
            with self._lock() as conn:
                with conn.cursor() as cur:
                    # Upsert cockpit state
                    cur.execute(
                        """
                        INSERT INTO cockpit_state (id, state, budget_remaining, updated_at)
                        VALUES (1, %s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            state = EXCLUDED.state,
                            budget_remaining = EXCLUDED.budget_remaining,
                            updated_at = NOW();
                        """,
                        (after, budget),
                    )
                    # Insert audit row
                    cur.execute(
                        """
                        INSERT INTO cockpit_log (
                            actor, action, before_state, after_state
                        ) VALUES (%s, %s, %s, %s);
                        """,
                        (actor, action, before, after),
                    )
        # If a PostgresWriter was supplied, also write to audit_log table
        if self.audit_writer is not None:
            self.audit_writer.write_audit_entry(
                actor=actor, action=action,
                before_state=before, after_state=after,
            )

    def write_budget(self, actor: str, state: str, budget: float) -> None:
        """Budget-only control write with audit entries.

        Phase 4: unlike write_state, this NEVER touches the state column —
        a process holding a stale local state must not be able to clobber
        a remote kill merely by setting the budget.
        """
        with self._mutex:
            with self._lock() as conn:
                with conn.cursor() as cur:
                    cur.execute(_BOOTSTRAP_ROW_SQL)
                    cur.execute(
                        """
                        UPDATE cockpit_state SET budget_remaining = %s,
                            updated_at = NOW()
                        WHERE id = 1;
                        """,
                        (budget,),
                    )
                    cur.execute(
                        """
                        INSERT INTO cockpit_log (
                            actor, action, before_state, after_state
                        ) VALUES (%s, %s, %s, %s);
                        """,
                        (actor, "set_budget", state, state),
                    )
        if self.audit_writer is not None:
            self.audit_writer.write_audit_entry(
                actor=actor, action="set_budget",
                before_state=state, after_state=state,
            )

    def adjust_budget(self, amount: float) -> tuple[bool, float]:
        """Atomic budget decrement. Returns (success, new_budget).

        Phase 3 fix (F4): bootstraps the singleton row first, so a fresh
        database decrements from the 1.0 default instead of losing the call.
        """
        with self._mutex:
            with self._lock() as conn:
                with conn.cursor() as cur:
                    cur.execute(_BOOTSTRAP_ROW_SQL)
                    cur.execute(
                        """
                        UPDATE cockpit_state SET
                            budget_remaining = budget_remaining - %s,
                            updated_at = NOW()
                        WHERE id = 1 AND budget_remaining >= %s
                        RETURNING budget_remaining;
                        """,
                        (amount, amount),
                    )
                    row = cur.fetchone()
                    if row is None:
                        # Get current budget
                        cur.execute(
                            "SELECT budget_remaining FROM cockpit_state WHERE id = 1;"
                        )
                        cur_row = cur.fetchone()
                        if cur_row is None:
                            # Phase 3 re-review fix (BUDGET_ADJUST_DEFAULT):
                            # the bootstrap above guarantees the row exists;
                            # a missing row here means DB corruption or a
                            # concurrent DROP — surface it, never mask it
                            # behind a fabricated 1.0.
                            raise RuntimeError(
                                "cockpit_state row missing after bootstrap"
                            )
                        return (False, float(cur_row[0]))
                    return (True, float(row[0]))

    def set_budget(self, value: float) -> None:
        """Atomic budget set (clamped 0..1 by caller).

        Phase 3 fix (F4): bootstraps the singleton row first, so a fresh
        database no longer silently drops the update.
        """
        with self._mutex:
            with self._lock() as conn:
                with conn.cursor() as cur:
                    cur.execute(_BOOTSTRAP_ROW_SQL)
                    cur.execute(
                        """
                        UPDATE cockpit_state SET budget_remaining = %s, updated_at = NOW()
                        WHERE id = 1;
                        """,
                        (value,),
                    )

    def read_log(self, limit: int = 20) -> list[dict]:
        """Recent cockpit audit entries (Postgres-side)."""
        with self._mutex:
            with self._read_txn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT log_id, actor, action, before_state, after_state, recorded_at
                        FROM cockpit_log ORDER BY recorded_at DESC LIMIT %s;
                        """,
                        (limit,),
                    )
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]


# Schema additions for cockpit state
COCKPIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cockpit_state (
    id                INTEGER PRIMARY KEY DEFAULT 1,
    state             TEXT NOT NULL DEFAULT 'active',
    budget_remaining  REAL NOT NULL DEFAULT 1.0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);
CREATE TABLE IF NOT EXISTS cockpit_log (
    log_id            BIGSERIAL PRIMARY KEY,
    actor             TEXT NOT NULL,
    action            TEXT NOT NULL,
    before_state      TEXT,
    after_state       TEXT NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cockpit_log_recorded ON cockpit_log (recorded_at DESC);
"""