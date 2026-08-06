"""RIG Memory OS v10 — Phase 3 FAIL remediation tests.

Covers the four Phase-3 FAIL items per convergence/merged-plan.md:
  F3 — multi-process kill switch (read-through from PostgresCockpitStore)
  F4 — budget bootstrap on a fresh DB + single canonical audit path
  F5 — panels report measured values or `no_data`, never fabrications
  F6 — TOCTOU token fence has real production callers

Postgres-backed tests create and drop their OWN database. They never touch
the live `cockpit_state` row in rig_memory_os_phase1 (hard constraint 6).
They skip cleanly when psycopg or the socket at /tmp:5432 is unavailable —
and Step 12 of the merged plan requires confirming they RAN, not skipped.

NOTE on floats: cockpit_state.budget_remaining is Postgres REAL (float4).
Budget assertions use only exactly-representable values (0.25 / 0.5 / 0.75 /
0.0625) or assertAlmostEqual.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PG_HOST = "/tmp"
PG_PORT = "5432"

# Import root for child processes spawned by the F3 two-process test:
# .../founder-runtime/founder_runtime/tests/this_file.py -> parents[2]
PKG_ROOT = str(Path(__file__).resolve().parents[2])

# Mirrors the audit_log block of deploy.py SCHEMA_SQL. Duplicated rather than
# imported because deploy.py is a top-level script, not an importable package
# module, and its full SCHEMA_SQL would drag in eight unrelated tables.
_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id            BIGSERIAL PRIMARY KEY,
    actor               TEXT NOT NULL,
    action              TEXT NOT NULL,
    before_state        TEXT,
    after_state         TEXT,
    context_hash        TEXT,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _psql(dbname: str, sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", "-h", PG_HOST, "-p", PG_PORT, "-d", dbname,
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True,
    )


def _postgres_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    try:
        return _psql("template1", "SELECT 1;").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class PostgresTestCase(unittest.TestCase):
    """Base for tests needing a private cockpit database.

    Subclasses set DB_NAME. setUpClass creates it and applies
    COCKPIT_SCHEMA_SQL + audit_log; tearDownClass terminates stragglers
    and drops it.
    """

    DB_NAME = "rig_test_phase3_base"
    dsn = ""

    @classmethod
    def setUpClass(cls):
        if not _postgres_available():
            raise unittest.SkipTest("postgres not reachable at /tmp:5432")
        from founder_runtime.postgres_cockpit import COCKPIT_SCHEMA_SQL
        cls._drop()
        r = _psql("template1", f"CREATE DATABASE {cls.DB_NAME};")
        if r.returncode != 0:
            raise unittest.SkipTest(f"cannot create {cls.DB_NAME}: {r.stderr}")
        r = _psql(cls.DB_NAME, COCKPIT_SCHEMA_SQL + _AUDIT_LOG_DDL)
        if r.returncode != 0:
            cls._drop()
            raise unittest.SkipTest(f"cannot apply schema: {r.stderr}")
        cls.dsn = f"host={PG_HOST} port={PG_PORT} dbname={cls.DB_NAME}"

    @classmethod
    def tearDownClass(cls):
        cls._drop()

    @classmethod
    def _drop(cls):
        # DROP DATABASE fails while any backend is connected. Tests register
        # store.close() via addCleanup, but a leaked child connection must not
        # wedge the whole suite.
        _psql("template1",
              "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
              f"WHERE datname = '{cls.DB_NAME}' AND pid <> pg_backend_pid();")
        _psql("template1", f"DROP DATABASE IF EXISTS {cls.DB_NAME};")

    def row_count(self) -> int:
        r = _psql(self.DB_NAME, "SELECT COUNT(*) FROM cockpit_state;")
        return int(r.stdout.split("\n")[2].strip())

    def reset_state_row(self) -> None:
        _psql(self.DB_NAME,
              "INSERT INTO cockpit_state (id, state, budget_remaining) "
              "VALUES (1, 'active', 1.0) "
              "ON CONFLICT (id) DO UPDATE SET state = 'active', "
              "budget_remaining = 1.0;")

    def make_store(self, **kw):
        """Build a store against the scratch DB and guarantee it closes."""
        from founder_runtime.postgres_cockpit import PostgresCockpitStore
        store = PostgresCockpitStore(dsn=self.dsn, **kw)
        self.addCleanup(store.close)
        return store

    def make_writer(self):
        """PostgresWriter against the scratch DB (audit_log only)."""
        from founder_runtime.postgres_writer import PostgresWriter
        w = PostgresWriter(dsn=self.dsn)
        self.addCleanup(w.close)
        return w


class TestHarness(PostgresTestCase):
    DB_NAME = "rig_test_phase3_harness"

    def test_scratch_db_has_cockpit_tables_and_no_rows(self):
        self.assertEqual(self.row_count(), 0)
        r = _psql(self.DB_NAME, "SELECT COUNT(*) FROM cockpit_log;")
        self.assertEqual(r.returncode, 0)
        r = _psql(self.DB_NAME, "SELECT COUNT(*) FROM audit_log;")
        self.assertEqual(r.returncode, 0)

    def test_scratch_db_is_not_the_live_db(self):
        self.assertNotIn("rig_memory_os_phase1", self.dsn)


# =========================================================================
# F4a — store bootstrap, transaction hygiene, thread safety
# =========================================================================

class TestStoreBootstrap(PostgresTestCase):
    DB_NAME = "rig_test_phase3_store"

    def tearDown(self):
        _psql(self.DB_NAME, "DELETE FROM cockpit_state;")

    def test_set_budget_bootstraps_missing_row(self):
        self.assertEqual(self.row_count(), 0)
        store = self.make_store()
        store.set_budget(0.25)
        self.assertEqual(self.row_count(), 1)
        state, budget = store.read_state()
        self.assertEqual(state, "active")
        self.assertAlmostEqual(budget, 0.25, places=5)

    def test_adjust_budget_bootstraps_missing_row(self):
        self.assertEqual(self.row_count(), 0)
        store = self.make_store()
        ok, remaining = store.adjust_budget(0.25)
        self.assertTrue(ok)
        self.assertAlmostEqual(remaining, 0.75, places=5)
        self.assertEqual(self.row_count(), 1)

    def test_adjust_budget_refuses_overdraw(self):
        store = self.make_store()
        store.set_budget(0.25)
        ok, remaining = store.adjust_budget(0.5)
        self.assertFalse(ok)
        self.assertAlmostEqual(remaining, 0.25, places=5)
        self.assertAlmostEqual(store.read_state()[1], 0.25, places=5)

    def test_ensure_row_is_idempotent(self):
        store = self.make_store()
        store.ensure_row()
        store.ensure_row()
        self.assertEqual(self.row_count(), 1)
        self.assertEqual(store.read_state(), ("active", 1.0))

    def test_ensure_row_does_not_clobber_existing_state(self):
        store = self.make_store()
        store.write_state(actor="t", action="engage_kill_switch",
                          before="active", after="killed", budget=0.5)
        store.ensure_row()
        state, budget = store.read_state()
        self.assertEqual(state, "killed")
        self.assertAlmostEqual(budget, 0.5, places=5)

    def test_read_state_leaves_no_open_transaction(self):
        import psycopg
        store = self.make_store()
        store.read_state()
        status = store._get_conn().info.transaction_status
        self.assertEqual(status, psycopg.pq.TransactionStatus.IDLE)

    def test_read_log_leaves_no_open_transaction(self):
        import psycopg
        store = self.make_store()
        store.read_log()
        status = store._get_conn().info.transaction_status
        self.assertEqual(status, psycopg.pq.TransactionStatus.IDLE)

    def test_store_is_thread_safe_under_concurrent_adjust(self):
        store = self.make_store()
        store.set_budget(1.0)
        failures: list = []

        def worker():
            try:
                for _ in range(2):
                    ok, _ = store.adjust_budget(0.0625)
                    if not ok:
                        failures.append("adjust refused")
            except Exception as exc:  # noqa: BLE001 — collect, assert below
                failures.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(failures, [])
        self.assertAlmostEqual(store.read_state()[1], 0.0, places=7)


# =========================================================================
# F3 — read-through TTL cache + two-process kill propagation
# =========================================================================

class TestKillSwitchReadThrough(PostgresTestCase):
    DB_NAME = "rig_test_phase3_killswitch"

    def setUp(self):
        self.reset_state_row()

    def test_kill_in_one_cockpit_is_visible_in_another_after_ttl(self):
        from founder_runtime.cockpit import ControlState, MemoryCockpit
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        self.assertFalse(a.is_killed())
        b.engage_kill_switch(actor="b")
        self.assertTrue(a.is_killed())
        self.assertEqual(a.state, ControlState.KILLED)

    def test_kill_is_enforced_not_merely_observed(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, OperationKind, assert_active,
        )
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b.engage_kill_switch(actor="b")
        with self.assertRaises(ControlBlocked):
            assert_active(a, "w", kind=OperationKind.WRITE)

    def test_ttl_window_is_respected(self):
        from founder_runtime.cockpit import MemoryCockpit
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=30.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b.engage_kill_switch(actor="b")
        # Within the TTL the cached (stale) value is served — the documented
        # bounded-staleness window.
        self.assertFalse(a.is_killed())
        a._last_store_read = 0.0
        self.assertTrue(a.is_killed())

    def test_remote_transition_bumps_local_fence(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.process_token import issue_token, verify_token
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        token = issue_token(a, "op")
        self.assertTrue(verify_token(a, token))
        b.engage_kill_switch(actor="b")
        a.is_killed()
        self.assertFalse(verify_token(a, token))

    def test_remote_observation_recorded_in_local_audit(self):
        from founder_runtime.cockpit import MemoryCockpit
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b.engage_kill_switch(actor="b")
        a.is_killed()
        entry = a.audit()[-1]
        self.assertEqual(entry["action"], "observed_remote_state")
        self.assertEqual(entry["after"], "killed")
        self.assertEqual(entry["actor"], "store")

    def test_store_failure_keeps_last_known_killed_state(self):
        from founder_runtime.cockpit import MemoryCockpit
        store_a = self.make_store()
        a = MemoryCockpit(store=store_a, store_read_ttl=0.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b.engage_kill_switch(actor="b")
        self.assertTrue(a.is_killed())
        store_a.read_state = mock.Mock(side_effect=RuntimeError("pg down"))
        a._last_store_read = 0.0
        # A store outage must never un-kill.
        self.assertTrue(a.is_killed())

    def test_release_kill_propagates(self):
        from founder_runtime.cockpit import MemoryCockpit
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b.engage_kill_switch(actor="b")
        self.assertTrue(a.is_killed())
        b.release_kill_switch(actor="b")
        self.assertFalse(a.is_killed())
        # KILLED -> PAUSED, never directly back to ACTIVE.
        self.assertTrue(a.is_paused())

    def test_budget_set_in_one_cockpit_visible_in_another(self):
        from founder_runtime.cockpit import MemoryCockpit
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b = MemoryCockpit(store=self.make_store(), store_read_ttl=0.0)
        b.set_budget(0.25)
        self.assertAlmostEqual(a.budget, 0.25, places=5)


class TestReadThroughIsInert(unittest.TestCase):
    """Without a store, the read-through path must be a complete no-op."""

    def test_cockpit_without_store_never_reads(self):
        from founder_runtime.cockpit import MemoryCockpit
        c = MemoryCockpit()
        c.is_killed()
        c.is_paused()
        c.snapshot()
        _ = c.budget
        self.assertEqual(c._last_store_read, 0.0)

    def test_default_ttl_is_quarter_second(self):
        from founder_runtime.cockpit import MemoryCockpit
        self.assertEqual(MemoryCockpit().store_read_ttl, 0.25)


_CHILD_KILL_SCRIPT = """
import sys
sys.path.insert(0, {pkg_root!r})
from founder_runtime.postgres_cockpit import PostgresCockpitStore
from founder_runtime.cockpit import MemoryCockpit
store = PostgresCockpitStore(dsn={dsn!r})
cockpit = MemoryCockpit(store=store, store_read_ttl=0.0)
cockpit.engage_kill_switch(actor="process-b")
assert cockpit.is_killed(), "child failed to kill its own cockpit"
store.close()
print("CHILD_KILLED_OK")
"""

_CHILD_RELEASE_SCRIPT = """
import sys
sys.path.insert(0, {pkg_root!r})
from founder_runtime.postgres_cockpit import PostgresCockpitStore
from founder_runtime.cockpit import MemoryCockpit
store = PostgresCockpitStore(dsn={dsn!r})
cockpit = MemoryCockpit(store=store, store_read_ttl=0.0)
cockpit.release_kill_switch(actor="process-b")
store.close()
print("CHILD_RELEASED_OK")
"""


class TestKillSwitchTwoProcess(PostgresTestCase):
    """Constraint 7 acceptance: REAL two-process propagation."""

    DB_NAME = "rig_test_phase3_twoproc"

    def setUp(self):
        self.reset_state_row()

    def _run_child(self, template: str, sentinel: str):
        script = template.format(pkg_root=PKG_ROOT, dsn=self.dsn)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0,
                         f"child failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}")
        self.assertIn(sentinel, proc.stdout)

    def test_kill_engaged_in_process_b_is_visible_in_process_a(self):
        from founder_runtime.cockpit import ControlState, MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, OperationKind, assert_active,
        )
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.2)
        self.assertFalse(a.is_killed())
        fence_before = a._fence.current()
        self._run_child(_CHILD_KILL_SCRIPT, "CHILD_KILLED_OK")
        time.sleep(0.3)  # > TTL; belt-and-braces (child startup exceeds it)
        self.assertTrue(a.is_killed())
        self.assertEqual(a.state, ControlState.KILLED)
        self.assertGreater(a._fence.current(), fence_before)
        # Propagated kill is ENFORCED, not merely observed.
        with self.assertRaises(ControlBlocked):
            assert_active(a, "post-kill-write", kind=OperationKind.WRITE)

    def test_kill_from_process_b_invalidates_process_a_in_flight_token(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.process_token import issue_token, verify_token
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.2)
        token = issue_token(a, "long-op")
        self.assertTrue(verify_token(a, token))
        self._run_child(_CHILD_KILL_SCRIPT, "CHILD_KILLED_OK")
        time.sleep(0.3)
        a.is_killed()
        # The cross-process TOCTOU proof: a token issued pre-kill is stale.
        self.assertFalse(verify_token(a, token))

    def test_release_from_process_b_is_visible_in_process_a(self):
        from founder_runtime.cockpit import MemoryCockpit
        a = MemoryCockpit(store=self.make_store(), store_read_ttl=0.2)
        self._run_child(_CHILD_KILL_SCRIPT, "CHILD_KILLED_OK")
        time.sleep(0.3)
        self.assertTrue(a.is_killed())
        self._run_child(_CHILD_RELEASE_SCRIPT, "CHILD_RELEASED_OK")
        time.sleep(0.3)
        self.assertFalse(a.is_killed())
        self.assertTrue(a.is_paused())

    def test_child_process_writes_cockpit_log_row(self):
        store_a = self.make_store()
        self._run_child(_CHILD_KILL_SCRIPT, "CHILD_KILLED_OK")
        rows = store_a.read_log(limit=5)
        self.assertTrue(any(
            r["actor"] == "process-b" and r["after_state"] == "killed"
            for r in rows
        ))


# =========================================================================
# F4b — exactly one audit_log row per transition
# =========================================================================

class TestAuditSingleWrite(PostgresTestCase):
    DB_NAME = "rig_test_phase3_audit"

    def tearDown(self):
        # DELETE (ROW EXCLUSIVE), not TRUNCATE (ACCESS EXCLUSIVE): writer
        # connections may hold idle-in-transaction SELECTs (known gap #6),
        # and TRUNCATE would block on them until the suite wedges.
        _psql(self.DB_NAME,
              "DELETE FROM cockpit_state; DELETE FROM cockpit_log; "
              "DELETE FROM audit_log;")

    def test_transition_writes_exactly_one_audit_row(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        store = self.make_store(audit_writer=w)
        c = MemoryCockpit(store=store, postgres_writer=w)
        before = w.audit_count()
        c.engage_kill_switch(actor="t")
        self.assertEqual(w.audit_count(), before + 1)

    def test_two_transitions_write_exactly_two_audit_rows(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        store = self.make_store(audit_writer=w)
        c = MemoryCockpit(store=store, postgres_writer=w)
        c.engage_kill_switch(actor="t")
        c.release_kill_switch(actor="t")
        self.assertEqual(w.audit_count(), 2)

    def test_distinct_writers_same_db_still_one_row(self):
        """Regression for the identity-guard mistake: two DISTINCT writer
        objects pointing at the same database must still produce one row."""
        from founder_runtime.cockpit import MemoryCockpit
        w_store = self.make_writer()
        w_cockpit = self.make_writer()
        store = self.make_store(audit_writer=w_store)
        c = MemoryCockpit(store=store, postgres_writer=w_cockpit)
        c.engage_kill_switch(actor="t")
        self.assertEqual(w_store.audit_count(), 1)
        self.assertEqual(w_cockpit.audit_count(), 1)  # same table, same 1 row

    def test_fallback_writes_when_store_has_no_audit_writer(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        store = self.make_store()  # audit_writer=None
        c = MemoryCockpit(store=store, postgres_writer=w)
        c.engage_kill_switch(actor="t")
        self.assertEqual(w.audit_count(), 1)

    def test_fallback_writes_when_no_store(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        c = MemoryCockpit(postgres_writer=w)
        c.engage_kill_switch(actor="t")
        self.assertEqual(w.audit_count(), 1)

    def test_fallback_writes_when_store_write_fails(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()

        class _FailingStore:
            audit_writer = None

            def ensure_row(self):
                pass

            def read_state(self):
                return ("active", 1.0)

            def write_state(self, **kw):
                raise RuntimeError("pg down")

        c = MemoryCockpit(store=_FailingStore(), postgres_writer=w)
        c.engage_kill_switch(actor="t")
        # A store outage must not also lose the audit trail.
        self.assertEqual(w.audit_count(), 1)

    def test_cockpit_log_and_audit_log_are_both_populated(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        store = self.make_store(audit_writer=w)
        c = MemoryCockpit(store=store, postgres_writer=w)
        c.engage_kill_switch(actor="t")
        self.assertEqual(len(store.read_log()), 1)
        self.assertEqual(w.audit_count(), 1)

    def test_no_op_transition_still_audits_once(self):
        from founder_runtime.cockpit import MemoryCockpit
        w = self.make_writer()
        c = MemoryCockpit(postgres_writer=w)
        c.engage_pause(actor="t")
        n = w.audit_count()
        c.engage_pause(actor="t")  # before == after == paused
        self.assertEqual(w.audit_count(), n + 1)


# =========================================================================
# F5 — real counters + honest panels
# =========================================================================

def _scope(ceiling: str = "internal"):
    from founder_runtime.retrieval_engine import RetrievalScope
    return RetrievalScope(
        tenant_id="t", client_id="c", project_id="p",
        mission_id="m", operator_id="op", sensitivity_ceiling=ceiling,
    )


class TestRetrievalCounters(unittest.TestCase):

    def test_query_count_starts_at_zero(self):
        from founder_runtime.retrieval_engine import RetrievalEngine
        e = RetrievalEngine()
        self.assertEqual(e.query_count(), 0)
        self.assertEqual(e.blocked_count(), 0)

    def test_query_count_increments_per_retrieve(self):
        from founder_runtime.retrieval_engine import RetrievalEngine
        e = RetrievalEngine()
        e.retrieve("q", _scope())
        self.assertEqual(e.query_count(), 1)
        e.retrieve("q2", _scope())
        self.assertEqual(e.query_count(), 2)

    def test_query_count_counts_cache_hits(self):
        from founder_runtime.retrieval_engine import RetrievalEngine
        e = RetrievalEngine()
        e.retrieve("same", _scope())
        pkg = e.retrieve("same", _scope(), use_cache=True)
        self.assertEqual(e.query_count(), 2)
        self.assertTrue(pkg.retrieval_reason.startswith("cache_hit"))

    def test_killed_cockpit_does_not_increment_query_count(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.retrieval_engine import RetrievalEngine
        c = MemoryCockpit()
        c.engage_kill_switch()
        e = RetrievalEngine(cockpit=c)
        pkg = e.retrieve("q", _scope())
        self.assertTrue(pkg.blocked)
        self.assertEqual(e.query_count(), 0)
        self.assertEqual(e.blocked_count(), 1)

    def test_budget_exhausted_increments_blocked_not_query(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.retrieval_engine import RetrievalEngine
        c = MemoryCockpit()
        c.set_budget(0.0)
        e = RetrievalEngine(cockpit=c)
        pkg = e.retrieve("q", _scope())
        self.assertTrue(pkg.blocked)
        self.assertEqual(e.query_count(), 0)
        self.assertEqual(e.blocked_count(), 1)

    def test_paused_cockpit_still_serves_reads_and_counts(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.retrieval_engine import RetrievalEngine
        c = MemoryCockpit()
        c.engage_pause()
        e = RetrievalEngine(cockpit=c)
        pkg = e.retrieve("q", _scope())
        # PAUSE allows reads (Opus 5 #3); over-blocking is a bug.
        self.assertFalse(pkg.blocked)
        self.assertEqual(e.query_count(), 1)

    def test_accessors_are_public_methods(self):
        from founder_runtime.retrieval_engine import RetrievalEngine
        self.assertTrue(callable(RetrievalEngine().query_count))
        self.assertTrue(callable(RetrievalEngine().blocked_count))


class TestPanelHonesty(unittest.TestCase):

    def _runtime(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.runtime import MemoryOSRuntime
        c = MemoryCockpit()
        r = MemoryOSRuntime(cockpit=c, gateway_secret=b"test-secret")
        return r, c

    def _populate_no_pg(self, r, c, **kw):
        from founder_runtime.panel_data import populate_panels_from_runtime
        with mock.patch(
            "founder_runtime.postgres_writer.PostgresWriter",
            side_effect=Exception("no pg"),
        ):
            populate_panels_from_runtime(r, c, **kw)

    def _panel(self, c, name):
        snap = c.snapshot()
        return next(p for p in snap.panels if p.name == name)

    def test_gbrain_panel_reports_no_data_without_source(self):
        r, c = self._runtime()
        self._populate_no_pg(r, c)
        panel = self._panel(c, "gBrain")
        self.assertEqual(panel.metrics["status"], "no_data")
        self.assertNotIn("synced", panel.metrics)
        self.assertNotIn("autopilot_lock_clear", panel.metrics)

    def test_gbrain_panel_uses_supplied_stats(self):
        r, c = self._runtime()
        self._populate_no_pg(r, c, gbrain_stats={"status": "ok", "orphans": 0})
        panel = self._panel(c, "gBrain")
        self.assertEqual(panel.metrics["status"], "ok")
        self.assertEqual(panel.metrics["orphans"], 0)

    def test_queue_lag_omitted_when_nothing_pending(self):
        r, c = self._runtime()
        self._populate_no_pg(r, c)
        panel = self._panel(c, "L1-L8 health")
        self.assertEqual(panel.metrics["queue_lag_seconds"], {})

    def test_queue_lag_is_measured_age_not_sixty_per_item(self):
        from founder_runtime.intent_service import PermissionClass
        r, c = self._runtime()
        for i in range(3):
            r.intent.create_intent(
                owner="p", trigger_type="manual", trigger_spec="x",
                action="a", permission_class=PermissionClass.A1_PREPARE,
                idempotency_key=f"p3-{i}",
            )
        self._populate_no_pg(r, c)
        panel = self._panel(c, "L1-L8 health")
        lag = panel.metrics["queue_lag_seconds"]["intents"]
        # The fabricated formula would produce 3 * 60.0 = 180.0.
        self.assertLess(lag, 5.0)
        self.assertGreaterEqual(lag, 0.0)

    def test_retrieval_panel_shows_measured_query_count(self):
        r, c = self._runtime()
        r.retrieval.retrieve("q1", _scope())
        r.retrieval.retrieve("q2", _scope())
        self._populate_no_pg(r, c)
        panel = self._panel(c, "Retrieval")
        self.assertEqual(panel.metrics["queries"], 2)

    def test_retrieval_panel_reports_denials_via_public_accessor(self):
        from founder_runtime.retrieval_engine import MemoryCandidate
        r, c = self._runtime()
        r.retrieval.store_candidate(MemoryCandidate(
            memory_id="m1", source="s", score=1.0, content_excerpt="x",
            scope={"tenant_id": "t", "client_id": "c", "project_id": "p",
                   "mission_id": "m", "operator_id": "op"},
            sensitivity="secret",
        ))
        r.retrieval.retrieve("q", _scope(ceiling="public"))
        self._populate_no_pg(r, c)
        panel = self._panel(c, "Retrieval")
        self.assertGreaterEqual(panel.metrics["denials"], 1)

    def test_existing_positional_call_signature_still_works(self):
        from founder_runtime.panel_data import populate_panels_from_runtime
        r, c = self._runtime()
        with mock.patch(
            "founder_runtime.postgres_writer.PostgresWriter",
            side_effect=Exception("no pg"),
        ):
            populate_panels_from_runtime(r, c)  # two positional args

    def test_gbrain_panel_status_is_no_data_when_unpopulated(self):
        from founder_runtime.cockpit import MemoryCockpit
        snap = MemoryCockpit().snapshot()
        panel = next(p for p in snap.panels if p.name == "gBrain")
        self.assertEqual(panel.status, "no_data")

    def test_gbrain_panel_status_follows_supplied_status(self):
        from founder_runtime.cockpit import MemoryCockpit
        c = MemoryCockpit()
        c.set_gbrain_stats({"status": "ok"})
        panel = next(p for p in c.snapshot().panels if p.name == "gBrain")
        self.assertEqual(panel.status, "ok")
        c.set_gbrain_stats({"status": "degraded"})
        panel = next(p for p in c.snapshot().panels if p.name == "gBrain")
        self.assertEqual(panel.status, "degraded")

    def test_gbrain_panel_status_matches_its_metrics(self):
        r, c = self._runtime()
        self._populate_no_pg(r, c)
        panel = self._panel(c, "gBrain")
        # The anti-contradiction assertion: status can never read "ok"
        # while its own metrics read "no_data".
        self.assertEqual(panel.status, panel.metrics["status"])

    def test_all_eight_panels_still_present(self):
        from founder_runtime.cockpit import MemoryCockpit
        snap = MemoryCockpit().snapshot()
        self.assertEqual(len(snap.panels), 8)
        names = {p.name for p in snap.panels}
        self.assertEqual(names, {
            "L1-L8 health", "Predictions", "Intentions", "Events / episodes",
            "Retrieval", "gBrain", "Procedures", "Backup / restore",
        })


# =========================================================================
# F6 — token gate plumbing + real callers
# =========================================================================

class TestTokenGate(unittest.TestCase):

    def test_assert_active_signature_is_unchanged(self):
        from founder_runtime.cockpit_subscriber import (
            OperationKind, assert_active,
        )
        params = list(inspect.signature(assert_active).parameters)
        self.assertEqual(params, ["cockpit", "operation", "kind"])
        self.assertEqual(
            inspect.signature(assert_active).parameters["kind"].default,
            OperationKind.WRITE,
        )

    def test_returns_token_when_active(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import assert_active_with_token
        from founder_runtime.process_token import ProcessToken
        tok = assert_active_with_token(MemoryCockpit(), "op")
        self.assertIsInstance(tok, ProcessToken)
        self.assertEqual(tok.operation, "op")

    def test_returns_none_for_none_cockpit(self):
        from founder_runtime.cockpit_subscriber import assert_active_with_token
        self.assertIsNone(assert_active_with_token(None, "op"))

    def test_raises_when_killed(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, assert_active_with_token,
        )
        c = MemoryCockpit()
        c.engage_kill_switch()
        with self.assertRaises(ControlBlocked):
            assert_active_with_token(c, "op")

    def test_raises_when_paused_for_writes(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, assert_active_with_token,
        )
        c = MemoryCockpit()
        c.engage_pause()
        with self.assertRaises(ControlBlocked):
            assert_active_with_token(c, "op")

    def test_allows_reads_when_paused(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            OperationKind, assert_active_with_token,
        )
        c = MemoryCockpit()
        c.engage_pause()
        tok = assert_active_with_token(c, "op", kind=OperationKind.READ)
        self.assertIsNotNone(tok)

    def test_verify_or_abort_passes_without_transition(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            assert_active_with_token, verify_or_abort,
        )
        c = MemoryCockpit()
        tok = assert_active_with_token(c, "op")
        verify_or_abort(c, tok, "op")  # must not raise

    def test_verify_or_abort_raises_after_kill(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, assert_active_with_token, verify_or_abort,
        )
        c = MemoryCockpit()
        tok = assert_active_with_token(c, "op")
        c.engage_kill_switch()
        with self.assertRaises(ControlBlocked) as ctx:
            verify_or_abort(c, tok, "op")
        self.assertIn("killed mid-operation", str(ctx.exception))

    def test_verify_or_abort_raises_after_pause(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, assert_active_with_token, verify_or_abort,
        )
        c = MemoryCockpit()
        tok = assert_active_with_token(c, "op")
        c.engage_pause()
        with self.assertRaises(ControlBlocked):
            verify_or_abort(c, tok, "op")

    def test_verify_or_abort_raises_after_budget_zero(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, assert_active_with_token, verify_or_abort,
        )
        c = MemoryCockpit()
        tok = assert_active_with_token(c, "op")
        c.set_budget(0.0)
        with self.assertRaises(ControlBlocked):
            verify_or_abort(c, tok, "op")

    def test_verify_or_abort_is_noop_without_cockpit(self):
        from founder_runtime.cockpit_subscriber import verify_or_abort
        self.assertIsNone(verify_or_abort(None, None, "op"))


class TestCheckpointWriterGate(unittest.TestCase):

    def test_ungated_writer_still_accepts(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        w = CheckpointWriter("m")
        self.assertTrue(w.write(0, "goal").accepted)

    def test_refuses_when_killed(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        c = MemoryCockpit()
        c.engage_kill_switch()
        w = CheckpointWriter("m", cockpit=c)
        with self.assertRaises(ControlBlocked):
            w.write(0, "goal")
        self.assertEqual(w.history(), [])
        self.assertEqual(w.fencing_token, 0)
        self.assertIsNone(w.current)

    def test_refuses_when_paused(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        c = MemoryCockpit()
        c.engage_pause()
        w = CheckpointWriter("m", cockpit=c)
        with self.assertRaises(ControlBlocked):
            w.write(0, "goal")
        self.assertEqual(w.history(), [])

    def test_refuses_when_budget_zero(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        c = MemoryCockpit()
        c.set_budget(0.0)
        w = CheckpointWriter("m", cockpit=c)
        with self.assertRaises(ControlBlocked):
            w.write(0, "goal")

    def test_allows_when_active(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        from founder_runtime.cockpit import MemoryCockpit
        w = CheckpointWriter("m", cockpit=MemoryCockpit())
        self.assertTrue(w.write(0, "goal").accepted)
        self.assertEqual(len(w.history()), 1)

    def test_aborts_on_kill_landing_mid_operation(self):
        """Deterministic TOCTOU proof: no threads, no sleeps."""
        import founder_runtime.checkpoint_writer as cw_mod
        from founder_runtime.checkpoint_writer import CheckpointWriter
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, OperationKind,
        )
        real = cw_mod.assert_active_with_token

        def racer(cockpit, operation, kind=OperationKind.WRITE):
            tok = real(cockpit, operation, kind=kind)
            cockpit.engage_kill_switch(actor="racer")
            return tok

        c = MemoryCockpit()
        w = CheckpointWriter("m", cockpit=c)
        with mock.patch.object(cw_mod, "assert_active_with_token", racer):
            with self.assertRaises(ControlBlocked) as ctx:
                w.write(0, "goal")
        self.assertIn("killed mid-operation", str(ctx.exception))
        self.assertEqual(w.history(), [])
        self.assertEqual(w.fencing_token, 0)

    def test_abort_leaves_no_lease_or_log_file(self):
        import founder_runtime.checkpoint_writer as cw_mod
        from founder_runtime.checkpoint_writer import CheckpointWriter
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, OperationKind,
        )
        real = cw_mod.assert_active_with_token

        def racer(cockpit, operation, kind=OperationKind.WRITE):
            tok = real(cockpit, operation, kind=kind)
            cockpit.engage_kill_switch(actor="racer")
            return tok

        with tempfile.TemporaryDirectory() as d:
            lease = Path(d) / "lease.json"
            history = Path(d) / "history.jsonl"
            c = MemoryCockpit()
            w = CheckpointWriter("m", storage_path=history, lease_path=lease,
                                 cockpit=c)
            with mock.patch.object(cw_mod, "assert_active_with_token", racer):
                with self.assertRaises(ControlBlocked):
                    w.write(0, "goal")
            self.assertFalse(lease.exists())
            self.assertFalse(history.exists())

    def test_cockpit_param_is_optional_and_trailing(self):
        from founder_runtime.checkpoint_writer import CheckpointWriter
        sig = inspect.signature(CheckpointWriter.__init__)
        params = list(sig.parameters)
        # Phase 4 appended postgres_writer after cockpit; both optional.
        self.assertEqual(params[-1], "postgres_writer")
        self.assertIn("cockpit", params)
        self.assertIsNone(sig.parameters["cockpit"].default)
        self.assertIsNone(sig.parameters["postgres_writer"].default)


class TestEpisodeBuilderGate(unittest.TestCase):

    def _start(self, b, run_id="r1", session_id="s1", actor="t"):
        return b.start_episode(run_id=run_id, session_id=session_id, actor=actor)

    def test_ungated_builder_still_records(self):
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        b = EpisodeBuilder()
        self._start(b)
        b.record(run_id="r1", session_id="s1", actor="t",
                 event_type=EventType.HEARTBEAT, action="hb")
        self.assertEqual(b.total_event_count(), 2)

    def test_start_episode_refuses_when_killed(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        from founder_runtime.episode_builder import EpisodeBuilder
        c = MemoryCockpit()
        c.engage_kill_switch()
        b = EpisodeBuilder(cockpit=c)
        with self.assertRaises(ControlBlocked):
            self._start(b)
        # No orphan open episode left behind.
        self.assertEqual(b.all_open_episodes(), [])

    def test_record_refuses_when_killed(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        c = MemoryCockpit()
        b = EpisodeBuilder(cockpit=c)
        self._start(b)
        c.engage_kill_switch()
        with self.assertRaises(ControlBlocked):
            b.record(run_id="r1", session_id="s1", actor="t",
                     event_type=EventType.HEARTBEAT, action="hb")
        self.assertEqual(len(b.get_episode("s1").events), 1)

    def test_record_refuses_when_paused(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        c = MemoryCockpit()
        b = EpisodeBuilder(cockpit=c)
        self._start(b)
        c.engage_pause()
        with self.assertRaises(ControlBlocked):
            b.record(run_id="r1", session_id="s1", actor="t",
                     event_type=EventType.HEARTBEAT, action="hb")

    def test_record_refuses_when_budget_zero(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import ControlBlocked
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        c = MemoryCockpit()
        b = EpisodeBuilder(cockpit=c)
        self._start(b)
        c.set_budget(0.0)
        with self.assertRaises(ControlBlocked):
            b.record(run_id="r1", session_id="s1", actor="t",
                     event_type=EventType.HEARTBEAT, action="hb")

    def test_record_allows_when_active(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        b = EpisodeBuilder(cockpit=MemoryCockpit())
        self._start(b)
        ev = b.record(run_id="r1", session_id="s1", actor="t",
                      event_type=EventType.HEARTBEAT, action="hb")
        self.assertIsNotNone(ev)
        self.assertEqual(b.total_event_count(), 2)

    def test_aborts_on_kill_landing_mid_operation(self):
        import founder_runtime.episode_builder as eb_mod
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, OperationKind,
        )
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        real = eb_mod.assert_active_with_token

        def racer(cockpit, operation, kind=OperationKind.WRITE):
            tok = real(cockpit, operation, kind=kind)
            cockpit.engage_kill_switch(actor="racer")
            return tok

        c = MemoryCockpit()
        b = EpisodeBuilder(cockpit=c)
        self._start(b)
        n_before = b.total_event_count()
        with mock.patch.object(eb_mod, "assert_active_with_token", racer):
            with self.assertRaises(ControlBlocked) as ctx:
                b.record(run_id="r1", session_id="s1", actor="t",
                         event_type=EventType.HEARTBEAT, action="hb",
                         idempotency_key="k-abort")
        self.assertIn("killed mid-operation", str(ctx.exception))
        self.assertEqual(b.total_event_count(), n_before)
        # The aborted write must not poison the idempotency index.
        self.assertNotIn("k-abort", b._idempotency_index)

    def test_abort_writes_nothing_to_jsonl(self):
        import founder_runtime.episode_builder as eb_mod
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.cockpit_subscriber import (
            ControlBlocked, OperationKind,
        )
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        real = eb_mod.assert_active_with_token

        def racer(cockpit, operation, kind=OperationKind.WRITE):
            tok = real(cockpit, operation, kind=kind)
            cockpit.engage_kill_switch(actor="racer")
            return tok

        with tempfile.TemporaryDirectory() as d:
            storage = Path(d) / "events.jsonl"
            c = MemoryCockpit()
            b = EpisodeBuilder(storage_path=storage, cockpit=c)
            self._start(b)
            lines_before = storage.read_text().count("\n")
            with mock.patch.object(eb_mod, "assert_active_with_token", racer):
                with self.assertRaises(ControlBlocked):
                    b.record(run_id="r1", session_id="s1", actor="t",
                             event_type=EventType.HEARTBEAT, action="hb")
            self.assertEqual(storage.read_text().count("\n"), lines_before)

    def test_idempotent_replay_is_not_gated_twice(self):
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.episode_builder import EpisodeBuilder, EventType
        b = EpisodeBuilder(cockpit=MemoryCockpit())
        self._start(b)
        ev1 = b.record(run_id="r1", session_id="s1", actor="t",
                       event_type=EventType.HEARTBEAT, action="hb",
                       idempotency_key="k1")
        n = b.total_event_count()
        ev2 = b.record(run_id="r1", session_id="s1", actor="t",
                       event_type=EventType.HEARTBEAT, action="hb",
                       idempotency_key="k1")
        self.assertEqual(ev1.event_id, ev2.event_id)
        self.assertEqual(b.total_event_count(), n)

    def test_cockpit_param_is_optional_and_trailing(self):
        from founder_runtime.episode_builder import EpisodeBuilder
        sig = inspect.signature(EpisodeBuilder.__init__)
        params = list(sig.parameters)
        # Phase 4 appended postgres_writer after cockpit; both optional.
        self.assertEqual(params[-1], "postgres_writer")
        self.assertIn("cockpit", params)
        self.assertIsNone(sig.parameters["cockpit"].default)
        self.assertIsNone(sig.parameters["postgres_writer"].default)


# =========================================================================
# Cross-family re-review fixes (family-B blocking findings)
# =========================================================================

class TestWriterFailureDiscipline(PostgresTestCase):
    """UNHANDLED_WRITER_FAILURE: a failed write must not poison the shared
    connection, and read helpers must not leak idle transactions."""

    DB_NAME = "rig_test_phase3_writer"

    def tearDown(self):
        _psql(self.DB_NAME, "DELETE FROM audit_log;")

    def test_failed_write_does_not_poison_writer(self):
        w = self.make_writer()
        # actor=None violates the NOT NULL constraint -> execute raises.
        with self.assertRaises(Exception):
            w.write_audit_entry(actor=None, action="boom")
        # Old behavior: connection stuck in aborted transaction; this second
        # call fails too. New behavior: rollback on error, then success.
        w.write_audit_entry(actor="t", action="ok", after_state="active")
        self.assertEqual(w.audit_count(), 1)

    def test_failed_write_leaves_no_partial_row(self):
        w = self.make_writer()
        with self.assertRaises(Exception):
            w.write_audit_entry(actor=None, action="boom")
        self.assertEqual(w.audit_count(), 0)

    def test_count_helpers_leave_no_open_transaction(self):
        import psycopg
        w = self.make_writer()
        w.audit_count()
        status = w._get_conn().info.transaction_status
        self.assertEqual(status, psycopg.pq.TransactionStatus.IDLE)

    def test_adjust_budget_raises_when_row_vanishes(self):
        """BUDGET_ADJUST_DEFAULT: a missing row after bootstrap must raise,
        never report a fabricated 1.0."""
        store = self.make_store()
        fake_conn = mock.MagicMock()
        cur = fake_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None  # UPDATE matches nothing; SELECT too
        with mock.patch.object(store, "_lock") as mock_lock:
            mock_lock.return_value.__enter__.return_value = fake_conn
            with self.assertRaises(RuntimeError):
                store.adjust_budget(0.25)


class TestAuditFailureVisibility(unittest.TestCase):
    """SILENT_AUDIT_FAILURE: a failed fallback audit write must be recorded
    in the surviving in-memory deque, never swallowed — while the control
    plane keeps working (kill still engages)."""

    def _failing_writer(self):
        w = mock.Mock()
        w.write_audit_entry.side_effect = RuntimeError("db down")
        return w

    def test_fallback_audit_failure_is_recorded_not_swallowed(self):
        from founder_runtime.cockpit import MemoryCockpit
        c = MemoryCockpit(postgres_writer=self._failing_writer())
        c.engage_kill_switch(actor="t")
        failures = [e for e in c.audit() if e["action"] == "audit_write_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["lost_entry"]["action"], "engage_kill_switch")
        self.assertIn("db down", failures[0]["error"])

    def test_state_transition_survives_audit_failure(self):
        from founder_runtime.cockpit import MemoryCockpit
        c = MemoryCockpit(postgres_writer=self._failing_writer())
        c.engage_kill_switch(actor="t")  # must not raise
        self.assertTrue(c.is_killed())

    def test_store_failure_plus_writer_failure_records_loss(self):
        from founder_runtime.cockpit import MemoryCockpit

        class _FailingStore:
            audit_writer = None

            def ensure_row(self):
                pass

            def read_state(self):
                return ("active", 1.0)

            def write_state(self, **kw):
                raise RuntimeError("pg down")

        c = MemoryCockpit(store=_FailingStore(),
                          postgres_writer=self._failing_writer())
        c.engage_kill_switch(actor="t")  # both paths fail; must not raise
        failures = [e for e in c.audit() if e["action"] == "audit_write_failed"]
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
