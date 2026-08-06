"""RIG Memory OS v10 — Real Postgres persistence (Yellow #1).

Provides psycopg writers for the 9 tables created by deploy.py:
- envelopes (L2 episodic event ledger)
- checkpoints (L1 working state)
- usage_receipts (Memory Gateway immutable receipts)
- intents (L8 prospective)
- effect_receipts (intent execution audit)
- audit_log (control plane audit)

This module does NOT change the subsystem APIs (gateway, retrieval,
intent, etc.). Instead it provides a PostgresWriter that subsystems
can use as an optional sink. When supplied, every state-changing
operation is recorded. When None, subsystems fall back to in-process
behavior (the previous default).

Phase 3 (per Opus 5 round-3 follow-up + council decision):
- psycopg[binary] dependency, real Postgres writers
- Idempotent: INSERT ... ON CONFLICT DO NOTHING on every write
- Connection pooling via psycopg_pool would be Phase 4; Phase 3
  uses a single shared connection per writer
- All writes are scoped to a single connection within an explicit
  transaction so a partial write doesn't leave the table half-populated
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import psycopg
    from psycopg.rows import dict_row
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore


# Default connection string for the deployed Postgres
DEFAULT_DSN = "host=/tmp port=5432 dbname=rig_memory_os_phase1 user=rig128gb"


@dataclass
class PostgresWriter:
    """Real Postgres writer for the 9 deployment tables.

    Usage:
        writer = PostgresWriter()
        writer.ensure_schema()
        writer.write_envelope(...)
        writer.write_checkpoint(...)
        ...

    Thread-safety: psycopg connections are NOT thread-safe. Use one
    writer per thread, or wrap calls with an external lock.
    """

    dsn: str = DEFAULT_DSN
    _conn: Optional[object] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not PSYCOPG_AVAILABLE:
            raise RuntimeError(
                "psycopg not installed. Install with: "
                "uv pip install 'psycopg[binary]>=3.1'"
            )

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=False)
        return self._conn

    @contextmanager
    def _txn(self):
        """Write transaction that always terminates.

        Phase 3 re-review fix (cross-family finding UNHANDLED_WRITER_FAILURE):
        previously a failed execute/commit left the shared connection in an
        aborted-transaction state, silently poisoning every later call on
        this writer. Now: commit on success, rollback on any exception
        (which re-raises — callers still see the failure).
        """
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def _read(self):
        """Read transaction that always terminates (rollback; nothing to
        persist). Closes the Phase 3 known-gap-#6 idle-in-transaction leaks
        in the count/query helpers."""
        conn = self._get_conn()
        try:
            yield conn
            conn.rollback()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def ensure_schema(self) -> None:
        """Create schema_version row + apply any pending DDL.

        Idempotent: re-runs are safe.
        """
        # Schema is owned by deploy.py; here we just verify schema_version
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT schema_id FROM schema_version ORDER BY applied_at DESC LIMIT 1;")
                row = cur.fetchone()
        if not row:
            raise RuntimeError(
                "schema_version table is empty. Run deploy.py first."
            )

    # ─── Envelopes (L2 episodic) ───
    def write_envelope(
        self,
        envelope_id: str,
        run_id: str,
        sequence: int,
        actor: str,
        event_type: str,
        action: str,
        content: dict,
        occurred_at: Optional[float] = None,
        state_before_ref: Optional[str] = None,
        state_after_ref: Optional[str] = None,
        decision: Optional[str] = None,
        error: Optional[str] = None,
        correction: Optional[str] = None,
        approval_ref: Optional[str] = None,
        outcome: Optional[str] = None,
        provenance: Optional[str] = None,
        sensitivity: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Append an L2 envelope. Idempotent on envelope_id.

        Insert ON CONFLICT (envelope_id) DO NOTHING means a replayed
        write is a no-op (a property the episodic ledger REQUIRES).
        """
        occurred_at = occurred_at if occurred_at is not None else time.time()
        metadata = metadata or {}
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO envelopes (
                        envelope_id, run_id, sequence, actor, event_type,
                        occurred_at, action, state_before_ref, state_after_ref,
                        decision, error, correction, approval_ref, outcome,
                        provenance, sensitivity, content, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        to_timestamp(%s), %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb, %s::jsonb
                    )
                    ON CONFLICT (envelope_id) DO NOTHING;
                    """,
                    (
                        envelope_id, run_id, sequence, actor, event_type,
                        occurred_at, action, state_before_ref, state_after_ref,
                        decision, error, correction, approval_ref, outcome,
                        provenance, sensitivity,
                        json.dumps(content), json.dumps(metadata),
                    ),
                )

    # ─── Checkpoints (L1 working state) ───
    def write_checkpoint(
        self,
        checkpoint_id: str,
        fencing_token: int,
        mission_id: str,
        sequence: int,
        active_goal: str,
        task_tree: Optional[dict] = None,
        constraints: Optional[list] = None,
        files: Optional[list] = None,
        open_loops: Optional[list] = None,
        next_action: str = "",
        context_budget: int = 0,
        context_used: int = 0,
    ) -> None:
        """Upsert a checkpoint keyed by (mission_id, sequence)."""
        task_tree = task_tree or {}
        constraints = constraints or []
        files = files or []
        open_loops = open_loops or []
        now = time.time()
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO checkpoints (
                        checkpoint_id, fencing_token, mission_id, sequence,
                        created_at, updated_at,
                        active_goal, task_tree, constraints, files,
                        open_loops, next_action, context_budget, context_used
                    ) VALUES (
                        %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s),
                        %s, %s::jsonb, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s, %s, %s
                    )
                    ON CONFLICT (checkpoint_id) DO UPDATE SET
                        fencing_token = EXCLUDED.fencing_token,
                        updated_at = EXCLUDED.updated_at,
                        active_goal = EXCLUDED.active_goal,
                        task_tree = EXCLUDED.task_tree,
                        constraints = EXCLUDED.constraints,
                        files = EXCLUDED.files,
                        open_loops = EXCLUDED.open_loops,
                        next_action = EXCLUDED.next_action,
                        context_budget = EXCLUDED.context_budget,
                        context_used = EXCLUDED.context_used;
                    """,
                    (
                        checkpoint_id, fencing_token, mission_id, sequence,
                        now, now,
                        active_goal, json.dumps(task_tree), json.dumps(constraints),
                        json.dumps(files), json.dumps(open_loops),
                        next_action, context_budget, context_used,
                    ),
                )

    # ─── Usage receipts (Memory Gateway) ───
    def write_usage_receipt(
        self,
        receipt_id: str,
        context_hash: str,
        tool_name: str,
        principal: str,
        run_id: str,
        session_id: str,
        trace_id: str,
        nonce: str,
        latency_ms: int = 0,
        token_count: int = 0,
        ts: Optional[float] = None,
    ) -> None:
        """Insert an immutable gateway usage receipt.

        Idempotent on receipt_id (each receipt is identified by its
        UUID minted at the gateway level).
        """
        ts = ts if ts is not None else time.time()
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO usage_receipts (
                        receipt_id, context_hash, tool_name, ts,
                        principal, run_id, session_id, trace_id, nonce,
                        latency_ms, token_count
                    ) VALUES (
                        %s, %s, %s, to_timestamp(%s),
                        %s, %s, %s, %s, %s,
                        %s, %s
                    )
                    ON CONFLICT (receipt_id) DO NOTHING;
                    """,
                    (
                        receipt_id, context_hash, tool_name, ts,
                        principal, run_id, session_id, trace_id, nonce,
                        latency_ms, token_count,
                    ),
                )

    # ─── Intents (L8 prospective) ───
    def write_intent(
        self,
        intent_id: str,
        owner: str,
        trigger_type: str,
        trigger_spec: str,
        action: str,
        permission_class: str = "A1_prepare",
        retry_policy: str = "next_admission",
        idempotency_key: Optional[str] = None,
        due_at: Optional[float] = None,
        expires_at: Optional[float] = None,
        status: str = "pending",
    ) -> None:
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO intents (
                        intent_id, owner, trigger_type, trigger_spec,
                        action, permission_class, retry_policy,
                        idempotency_key,
                        due_at, expires_at, status, attempts
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s,
                        CASE WHEN %s::float8 IS NULL THEN NULL ELSE to_timestamp(%s) END,
                        CASE WHEN %s::float8 IS NULL THEN NULL ELSE to_timestamp(%s) END,
                        %s, 0
                    )
                    ON CONFLICT (intent_id) DO NOTHING;
                    """,
                    (
                        intent_id, owner, trigger_type, trigger_spec,
                        action, permission_class, retry_policy,
                        idempotency_key,
                        due_at, due_at if due_at is not None else 0,
                        expires_at, expires_at if expires_at is not None else 0,
                        status,
                    ),
                )

    def update_intent_status(
        self,
        intent_id: str,
        status: str,
    ) -> None:
        # Phase 4: completed_at only advances on terminal states; a re-pend
        # (approve_blocked / retry) must not stamp a completion time.
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE intents SET
                        status = %s,
                        completed_at = CASE
                            WHEN %s IN ('completed', 'cancelled', 'expired')
                            THEN NOW()
                            ELSE completed_at
                        END
                    WHERE intent_id = %s;
                    """,
                    (status, status, intent_id),
                )

    # ─── Effect receipts (intent execution) ───
    def write_effect_receipt(
        self,
        receipt_id: str,
        intent_id: str,
        approver_id: Optional[str],
        permission_class: str,
        result: str,
    ) -> None:
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO effect_receipts (
                        receipt_id, intent_id, approver_id,
                        permission_class, result
                    ) VALUES (
                        %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (receipt_id) DO NOTHING;
                    """,
                    (receipt_id, intent_id, approver_id, permission_class, result),
                )

    # ─── Audit log (control plane) ───
    def write_audit_entry(
        self,
        actor: str,
        action: str,
        before_state: Optional[str] = None,
        after_state: Optional[str] = None,
        context_hash: Optional[str] = None,
    ) -> None:
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_log (
                        actor, action, before_state, after_state, context_hash
                    ) VALUES (
                        %s, %s, %s, %s, %s
                    );
                    """,
                    (actor, action, before_state, after_state, context_hash),
                )

    # ─── Query helpers (read-side) ───
    def envelope_count(self) -> int:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM envelopes;")
                return cur.fetchone()[0]

    def checkpoint_count(self) -> int:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM checkpoints;")
                return cur.fetchone()[0]

    def receipt_count(self) -> int:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM usage_receipts;")
                return cur.fetchone()[0]

    def audit_count(self) -> int:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM audit_log;")
                return cur.fetchone()[0]

    def latest_audit_entries(self, limit: int = 10) -> list[dict]:
        with self._read() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT audit_id, actor, action, before_state, "
                    "after_state, recorded_at FROM audit_log "
                    "ORDER BY recorded_at DESC LIMIT %s;",
                    (limit,),
                )
                return list(cur.fetchall())

    # ─── Predictions (Reality Cortex / Predictor) ───

    def write_prediction(
        self,
        prediction_id: str,
        target: str,
        current_state: str,
        predicted_state: str,
        probability: float,
        allowed_action: str = "NOOP",
        expires_at: Optional[float] = None,
        harness: str = "default",
        stage: str = "default",
        project: str = "default",
    ) -> None:
        """Write a prediction to the predictions table. Idempotent on prediction_id."""
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO predictions (
                        prediction_id, target, current_state, predicted_state,
                        probability, allowed_action, expires_at,
                        actual_outcome, resolved,
                        harness, stage, project
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s,
                        CASE WHEN %s = 0 THEN NULL ELSE to_timestamp(%s) END,
                        NULL, false,
                        %s, %s, %s
                    )
                    ON CONFLICT (prediction_id) DO NOTHING;
                    """,
                    (
                        prediction_id, target, current_state, predicted_state,
                        probability, allowed_action,
                        expires_at or 0.0, expires_at or 0.0,
                        harness, stage, project,
                    ),
                )

    def resolve_prediction(
        self,
        prediction_id: str,
        actual_outcome: str,
    ) -> None:
        """Mark a prediction resolved with its actual outcome."""
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE predictions SET
                        actual_outcome = %s,
                        resolved = true
                    WHERE prediction_id = %s;
                    """,
                    (actual_outcome, prediction_id),
                )

    def write_calibration(
        self,
        prediction_id: str,
        predicted_probability: float,
        actual_outcome: bool,
        brier_component: float,
        log_loss_component: float,
    ) -> None:
        """Write a calibration record. Idempotent on prediction_id."""
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO calibration (
                        prediction_id, predicted_probability,
                        actual_outcome, brier_component, log_loss_component
                    ) VALUES (
                        %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (prediction_id) DO NOTHING;
                    """,
                    (
                        prediction_id, predicted_probability,
                        actual_outcome, brier_component, log_loss_component,
                    ),
                )

    def unresolved_predictions(self, limit: int = 100) -> list[dict]:
        """Get predictions that are expired but not yet resolved."""
        with self._read() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT prediction_id, predicted_state, probability,
                           harness, stage, project
                    FROM predictions
                    WHERE resolved = false
                    ORDER BY expires_at DESC NULLS LAST
                    LIMIT %s;
                    """,
                    (limit,),
                )
                return list(cur.fetchall())

    def prediction_count(self) -> int:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM predictions;")
                return cur.fetchone()[0]

    def calibration_count(self) -> int:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM calibration;")
                return cur.fetchone()[0]

    def avg_brier_score(self) -> float:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT AVG(brier_component) FROM calibration;")
                row = cur.fetchone()
                return float(row[0]) if row and row[0] is not None else 0.0

    # ─── Claims (Reality Cortex) ───

    def write_claim(
        self,
        claim_id: str,
        subject: str,
        statement: str,
        evidence_refs: Optional[list] = None,
        valid_from: Optional[float] = None,
        valid_to: Optional[float] = None,
        confidence: float = 0.5,
        status: str = "candidate",
    ) -> None:
        """Write a claim to the claims table. Idempotent on claim_id."""
        evidence_refs = evidence_refs or []
        with self._txn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO claims (
                        claim_id, subject, statement, evidence_refs,
                        valid_from, valid_to, learned_at, confidence, status
                    ) VALUES (
                        %s, %s, %s, %s::jsonb,
                        to_timestamp(%s), CASE WHEN %s = 0 THEN NULL ELSE to_timestamp(%s) END,
                        to_timestamp(%s), %s, %s
                    )
                    ON CONFLICT (claim_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        confidence = EXCLUDED.confidence,
                        valid_to = EXCLUDED.valid_to;
                    """,
                    (
                        claim_id, subject, statement, json.dumps(evidence_refs),
                        valid_from or time.time(),
                        valid_to or 0.0, valid_to or 0.0,
                        time.time(), confidence, status,
                    ),
                )

    def promoted_claims(self, subject: Optional[str] = None) -> list[dict]:
        """Get promoted claims, optionally filtered by subject."""
        with self._read() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if subject:
                    cur.execute(
                        "SELECT * FROM claims WHERE status='promoted' AND subject=%s ORDER BY learned_at DESC;",
                        (subject,),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM claims WHERE status='promoted' ORDER BY learned_at DESC;"
                    )
                return list(cur.fetchall())

    def claim_count(self) -> int:
        with self._read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM claims;")
                return cur.fetchone()[0]