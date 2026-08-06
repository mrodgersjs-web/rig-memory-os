"""SQLite-backed durable store. Postgres-portable.

Phase 1 invariants:
- WAL mode for concurrent reads + serialized writes
- Foreign keys on
- All writes through typed helpers
- Leases are atomic (BEGIN IMMEDIATE) and time-bounded
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .contracts import (
    NodeStatus,
    OpportunityContract,
    OpportunityStage,
    WorkItemContract,
    WorkItemStatus,
    WorkResultContract,
    ApprovalRequestContract,
    ApprovalLane,
    Verdict,
    ProofPacket,
)

DEFAULT_DB_PATH = Path.home() / ".rig" / "founder-runtime" / "state.db"


# ------------------------------------------------------------------ JSON helpers


def _json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _loads(s: Optional[str]) -> Any:
    return json.loads(s) if s else None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


# ------------------------------------------------------------------ Connection


class Store:
    """Thread-safe connection pool around a single SQLite file."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,  # autocommit; we use explicit BEGIN IMMEDIATE
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Context manager for an exclusive write transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn


# ------------------------------------------------------------------ Bootstrap


def init_db(store: Store, migration_path: Path | str) -> None:
    """Apply the migration SQL. Idempotent (CREATE TABLE IF NOT EXISTS)."""
    import re
    sql = Path(migration_path).read_text(encoding="utf-8")
    # Strip line comments (-- ...) then split on ; for statement boundaries
    sql = re.sub(r"--[^\n]*", "", sql)
    statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().upper().startswith("PRAGMA")]
    with store.tx(immediate=False) as conn:
        for stmt in statements:
            conn.execute(stmt)


# ------------------------------------------------------------------ Nodes


def register_node(store: Store, node: dict[str, Any]) -> None:
    with store.tx() as conn:
        conn.execute(
            """
            INSERT INTO nodes (node_id, hostname, status, capabilities,
                max_concurrency, lan_address, tailnet_address, worker_version,
                health_details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                hostname=excluded.hostname,
                status=excluded.status,
                capabilities=excluded.capabilities,
                max_concurrency=excluded.max_concurrency,
                lan_address=excluded.lan_address,
                tailnet_address=excluded.tailnet_address,
                worker_version=excluded.worker_version,
                health_details=excluded.health_details
            """,
            (
                node["node_id"],
                node["hostname"],
                node.get("status", NodeStatus.ONLINE.value),
                _json(node.get("capabilities", [])),
                int(node.get("max_concurrency", 2)),
                node.get("lan_address"),
                node.get("tailnet_address"),
                node.get("worker_version", "0.1.0"),
                _json(node.get("health_details", {})),
            ),
        )


def heartbeat(store: Store, node_id: str, load: int = 0) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE nodes SET last_heartbeat = ?, current_load = ? WHERE node_id = ?",
            (_iso(datetime.now(timezone.utc)), load, node_id),
        )


def list_nodes(store: Store) -> list[dict[str, Any]]:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM nodes ORDER BY node_id"
        ).fetchall()
    return [_row_node(r) for r in rows]


def mark_offline_stale_nodes(store: Store, stale_after_seconds: int = 180) -> int:
    """Flip nodes with stale heartbeats to OFFLINE_UNVERIFIED. Returns count.

    A node that has never sent a heartbeat is *not* stale — it's just registered.
    Only flip nodes whose heartbeat is older than the threshold.
    """
    with store.tx() as conn:
        cur = conn.execute(
            """
            UPDATE nodes
               SET status = ?
             WHERE status IN (?, ?)
               AND last_heartbeat IS NOT NULL
               AND (julianday('now') - julianday(last_heartbeat)) * 86400 > ?
            """,
            (
                NodeStatus.OFFLINE_UNVERIFIED.value,
                NodeStatus.ONLINE.value,
                NodeStatus.DRAINING.value,
                stale_after_seconds,
            ),
        )
    return cur.rowcount


def _row_node(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "node_id": r["node_id"],
        "hostname": r["hostname"],
        "status": r["status"],
        "capabilities": _loads(r["capabilities"]) or [],
        "max_concurrency": r["max_concurrency"],
        "current_load": r["current_load"],
        "last_heartbeat": r["last_heartbeat"],
        "lan_address": r["lan_address"],
        "tailnet_address": r["tailnet_address"],
        "worker_version": r["worker_version"],
        "health_details": _loads(r["health_details"]) or {},
    }


# ------------------------------------------------------------------ Opportunities


def upsert_opportunity(store: Store, opp: OpportunityContract) -> None:
    with store.tx() as conn:
        conn.execute(
            """
            INSERT INTO opportunities (opportunity_id, title, vertical, company_id,
                stage, direction_fit, pain_evidence, urgency_evidence, buyer_access,
                proof_advantage, speed_to_test, delivery_burden, recurrence_potential,
                ip_reuse_potential, confidence, priority, owner, next_action,
                next_action_due_at, evidence, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(opportunity_id) DO UPDATE SET
                title=excluded.title,
                vertical=excluded.vertical,
                stage=excluded.stage,
                direction_fit=excluded.direction_fit,
                pain_evidence=excluded.pain_evidence,
                urgency_evidence=excluded.urgency_evidence,
                buyer_access=excluded.buyer_access,
                proof_advantage=excluded.proof_advantage,
                speed_to_test=excluded.speed_to_test,
                delivery_burden=excluded.delivery_burden,
                recurrence_potential=excluded.recurrence_potential,
                ip_reuse_potential=excluded.ip_reuse_potential,
                confidence=excluded.confidence,
                priority=excluded.priority,
                owner=excluded.owner,
                next_action=excluded.next_action,
                next_action_due_at=excluded.next_action_due_at,
                evidence=excluded.evidence,
                updated_at=excluded.updated_at
            """,
            (
                opp.opportunity_id, opp.title, opp.vertical, opp.company_id,
                opp.stage.value, opp.direction_fit, opp.pain_evidence,
                opp.urgency_evidence, opp.buyer_access, opp.proof_advantage,
                opp.speed_to_test, opp.delivery_burden, opp.recurrence_potential,
                opp.ip_reuse_potential, opp.confidence, opp.priority,
                opp.owner, opp.next_action, _iso(opp.next_action_due_at),
                _json(opp.evidence), _iso(opp.created_at), _iso(opp.updated_at),
            ),
        )


def list_opportunities(
    store: Store,
    *,
    stage: Optional[OpportunityStage] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = "SELECT * FROM opportunities"
    args: list[Any] = []
    if stage:
        q += " WHERE stage = ?"
        args.append(stage.value)
    q += " ORDER BY priority DESC, updated_at DESC LIMIT ?"
    args.append(limit)
    with store.read() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row_opp(r) for r in rows]


def _row_opp(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "opportunity_id": r["opportunity_id"],
        "title": r["title"],
        "vertical": r["vertical"],
        "company_id": r["company_id"],
        "stage": r["stage"],
        "direction_fit": r["direction_fit"],
        "pain_evidence": r["pain_evidence"],
        "urgency_evidence": r["urgency_evidence"],
        "buyer_access": r["buyer_access"],
        "proof_advantage": r["proof_advantage"],
        "speed_to_test": r["speed_to_test"],
        "delivery_burden": r["delivery_burden"],
        "recurrence_potential": r["recurrence_potential"],
        "ip_reuse_potential": r["ip_reuse_potential"],
        "confidence": r["confidence"],
        "priority": r["priority"],
        "owner": r["owner"],
        "next_action": r["next_action"],
        "next_action_due_at": r["next_action_due_at"],
        "evidence": _loads(r["evidence"]) or [],
    }


# ------------------------------------------------------------------ Work Items


def enqueue_work_item(store: Store, item: WorkItemContract) -> None:
    """Insert a new work item; idempotency_key is UNIQUE so duplicate enqueue is a no-op."""
    with store.tx() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO work_items (work_item_id, opportunity_id,
                work_type, objective, payload, required_capabilities, status,
                priority, idempotency_key, approval_lane, max_attempts,
                attempt_count, available_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item.work_item_id, item.opportunity_id, item.work_type,
                item.objective, _json(item.payload),
                _json(item.required_capabilities), item.status.value,
                item.priority, item.idempotency_key, item.approval_lane.value,
                item.max_attempts, item.attempt_count, _iso(item.available_at),
                _iso(item.created_at), _iso(item.updated_at),
            ),
        )


def claim_next_work_item(
    store: Store,
    *,
    node_id: str,
    capabilities: list[str],
    lease_seconds: int = 300,
) -> Optional[WorkItemContract]:
    """Atomically lease the highest-priority eligible work item for this node.

    Eligibility:
    - status in (READY, REOPENED, FAILED-and-attempts<max)
    - available_at <= now
    - required_capabilities ⊆ node capabilities

    Lease is bound to node_id and expires in lease_seconds.
    Returns None if nothing eligible.
    """
    now = datetime.now(timezone.utc)
    caps_json = _json(capabilities)

    with store.tx() as conn:
        # Find the best candidate (no FOR UPDATE needed — BEGIN IMMEDIATE serializes)
        row = conn.execute(
            """
            SELECT work_item_id, opportunity_id, work_type, objective, payload,
                   required_capabilities, status, priority, idempotency_key,
                   approval_lane, max_attempts, attempt_count, available_at,
                   lease_owner, lease_expires_at, created_at, updated_at
              FROM work_items
             WHERE status IN ('READY', 'REOPENED')
               AND available_at <= ?
               AND attempt_count < max_attempts
             ORDER BY priority DESC, created_at ASC
             LIMIT 1
            """,
            (_iso(now),),
        ).fetchone()

        if row is None:
            return None

        # Capability gate — required_caps must be a subset of node caps
        required = set(_loads(row["required_capabilities"]) or [])
        if required and not required.issubset(set(capabilities)):
            # Defer: bump available_at so the next eligible node picks it up
            conn.execute(
                "UPDATE work_items SET available_at = ?, updated_at = ? WHERE work_item_id = ?",
                (_iso(now.replace(microsecond=0)), _iso(now), row["work_item_id"]),
            )
            return None

        lease_expires = now.timestamp() + lease_seconds
        lease_iso = datetime.fromtimestamp(lease_expires, tz=timezone.utc).isoformat()

        conn.execute(
            """
            UPDATE work_items
               SET status = 'LEASED',
                   lease_owner = ?,
                   lease_expires_at = ?,
                   attempt_count = attempt_count + 1,
                   updated_at = ?
             WHERE work_item_id = ?
            """,
            (node_id, lease_iso, _iso(now), row["work_item_id"]),
        )

    return WorkItemContract(
        work_item_id=row["work_item_id"],
        opportunity_id=row["opportunity_id"],
        work_type=row["work_type"],
        objective=row["objective"],
        payload=_loads(row["payload"]) or {},
        required_capabilities=_loads(row["required_capabilities"]) or [],
        status=WorkItemStatus.LEASED,
        priority=row["priority"],
        idempotency_key=row["idempotency_key"],
        approval_lane=row["approval_lane"],
        max_attempts=row["max_attempts"],
        attempt_count=row["attempt_count"] + 1,
        available_at=_parse_dt(row["available_at"]) or now,
        lease_owner=node_id,
        lease_expires_at=_parse_dt(lease_iso),
        created_at=_parse_dt(row["created_at"]) or now,
        updated_at=now,
    )


def renew_lease(store: Store, work_item_id: str, node_id: str, lease_seconds: int = 300) -> bool:
    now = datetime.now(timezone.utc)
    lease_iso = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=timezone.utc).isoformat()
    with store.tx() as conn:
        cur = conn.execute(
            """
            UPDATE work_items
               SET lease_expires_at = ?, updated_at = ?
             WHERE work_item_id = ?
               AND lease_owner = ?
               AND status = 'LEASED'
            """,
            (_iso(datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=timezone.utc)), _iso(now), work_item_id, node_id),
        )
    return cur.rowcount == 1


def complete_work_item(
    store: Store,
    *,
    work_item_id: str,
    node_id: str,
    result: WorkResultContract,
) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE work_items SET status='COMPLETED', lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE work_item_id=? AND lease_owner=?",
            (_iso(datetime.now(timezone.utc)), work_item_id, node_id),
        )
        conn.execute(
            """
            INSERT INTO work_results (result_id, work_item_id, worker_id, status,
                summary, artifact_paths, source_refs, metrics, proofpacket_path,
                started_at, completed_at, error_class, retryable, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                result.result_id, work_item_id, node_id, result.status.value,
                result.summary, _json(result.artifact_paths),
                _json(result.source_refs), _json(result.metrics),
                result.proofpacket_path, _iso(result.started_at),
                _iso(result.completed_at), result.error_class,
                1 if result.retryable else 0, _iso(result.created_at),
            ),
        )


def fail_work_item(
    store: Store,
    *,
    work_item_id: str,
    node_id: str,
    error_class: str,
    retryable: bool,
    summary: str,
) -> str:
    """Mark the item FAILED or DEAD_LETTERED depending on retryable + attempts."""
    now = datetime.now(timezone.utc)
    with store.tx() as conn:
        row = conn.execute(
            "SELECT max_attempts, attempt_count FROM work_items WHERE work_item_id=? AND lease_owner=?",
            (work_item_id, node_id),
        ).fetchone()
        if row is None:
            return "NOT_FOUND"

        attempts = row["attempt_count"]
        max_a = row["max_attempts"]

        if not retryable or attempts >= max_a:
            new_status = "DEAD_LETTERED"
            available = None
        else:
            new_status = "REOPENED"
            # Requeue with priority aging — backoff 30s per attempt
            backoff = 30 * attempts
            available = datetime.fromtimestamp(now.timestamp() + backoff, tz=timezone.utc).isoformat()

        conn.execute(
            "UPDATE work_items SET status=?, lease_owner=NULL, lease_expires_at=NULL, available_at=COALESCE(?, available_at), updated_at=? WHERE work_item_id=? AND lease_owner=?",
            (new_status, available, _iso(now), work_item_id, node_id),
        )

        conn.execute(
            """
            INSERT INTO work_results (result_id, work_item_id, worker_id, status,
                summary, artifact_paths, source_refs, metrics, proofpacket_path,
                started_at, completed_at, error_class, retryable, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(__import__("uuid").uuid4()), work_item_id, node_id, new_status,
                summary, "[]", "[]", "{}", None, None, _iso(now),
                error_class, 1 if retryable else 0, _iso(now),
            ),
        )
    return new_status


def recover_expired_leases(store: Store) -> int:
    """Flip expired leases back to READY. Returns count."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with store.tx() as conn:
        cur = conn.execute(
            """
            UPDATE work_items
               SET status = 'REOPENED',
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   updated_at = ?
             WHERE status = 'LEASED'
               AND lease_expires_at IS NOT NULL
               AND lease_expires_at < ?
            """,
            (now_iso, now_iso),
        )
    return cur.rowcount


def queue_metrics(store: Store) -> dict[str, int]:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM work_items GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ------------------------------------------------------------------ Approvals


def create_approval_request(store: Store, req: ApprovalRequestContract) -> None:
    with store.tx() as conn:
        conn.execute(
            """
            INSERT INTO approval_requests (approval_id, action_type, target,
                exact_content_or_diff, business_reason, rollback_plan, status,
                requested_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                req.approval_id, req.action_type, req.target,
                _json(req.exact_content_or_diff), req.business_reason,
                req.rollback_plan, req.status, _iso(req.requested_at),
            ),
        )


def list_pending_approvals(store: Store) -> list[dict[str, Any]]:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM approval_requests WHERE status='PENDING' ORDER BY requested_at"
        ).fetchall()
    return [
        {
            "approval_id": r["approval_id"],
            "action_type": r["action_type"],
            "target": r["target"],
            "exact_content_or_diff": _loads(r["exact_content_or_diff"]) or {},
            "business_reason": r["business_reason"],
            "rollback_plan": r["rollback_plan"],
            "status": r["status"],
            "requested_at": r["requested_at"],
        }
        for r in rows
    ]


# ------------------------------------------------------------------ Proof packets


def record_proof_packet(store: Store, pkt: ProofPacket) -> None:
    with store.tx() as conn:
        conn.execute(
            """
            INSERT INTO proof_packets (proof_id, work_item_id, opportunity_id,
                result_id, verifier_node, verifier_model, verdict,
                evidence_hash, packet_path, sealed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pkt.proof_id, pkt.work_item_id, pkt.opportunity_id,
                pkt.result_id, pkt.verifier_node, pkt.verifier_model,
                pkt.verdict.value, pkt.evidence_hash, pkt.packet_path,
                _iso(pkt.sealed_at),
            ),
        )


# ------------------------------------------------------------------ Audit


def append_audit(store: Store, *, actor: str, action: str, target: Optional[str], detail: Optional[dict] = None) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO audit_log (audit_id, actor, action, target, detail, created_at) VALUES (?,?,?,?,?,?)",
            (str(__import__("uuid").uuid4()), actor, action, target, _json(detail or {}), _iso(datetime.now(timezone.utc))),
        )