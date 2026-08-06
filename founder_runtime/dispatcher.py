"""Phase 1 — Dispatcher.

The single business scheduler. Runs every 60 seconds.

Responsibilities:
1. Recover expired leases.
2. Detect free node capacity.
3. Match capability-eligible work to free nodes (the lease).
4. Push ranking for ready work (priority aging for starved items).
5. Emit metrics.

This is deterministic code, not an LLM. Hermes cron invokes it on a 60s tick.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from .contracts import (
    NodeCapabilityContract,
    WorkItemContract,
    WorkItemStatus,
    ApprovalLane,
)
from .store import (
    Store,
    claim_next_work_item,
    recover_expired_leases,
    queue_metrics,
    list_nodes,
    heartbeat,
    mark_offline_stale_nodes,
    enqueue_work_item,
    append_audit,
)


def dispatch_tick(
    store: Store,
    *,
    lease_seconds: int = 300,
    stale_node_seconds: int = 180,
    priority_aging_threshold_minutes: int = 30,
) -> dict[str, Any]:
    """One 60-second dispatcher tick. Returns metrics for observability."""
    now = datetime.now(timezone.utc)

    # 1. Mark stale nodes OFFLINE_UNVERIFIED
    stale = mark_offline_stale_nodes(store, stale_after_seconds=stale_node_seconds)

    # 2. Recover expired leases (zombies)
    recovered = recover_expired_leases(store)

    # 3. Priority aging — bump priority of long-waiting READY items
    aging_count = _apply_priority_aging(store, threshold_minutes=priority_aging_threshold_minutes)

    # 4. Find healthy nodes with capacity and try to lease one item per node
    nodes = [n for n in list_nodes(store) if n["status"] == "ONLINE" and n["current_load"] < n["max_concurrency"]]
    leased = 0
    lease_log: list[dict[str, Any]] = []
    for node in nodes:
        caps = node.get("capabilities") or []
        item = claim_next_work_item(store, node_id=node["node_id"], capabilities=caps, lease_seconds=lease_seconds)
        if item is not None:
            leased += 1
            heartbeat(store, node["node_id"], load=node["current_load"] + 1)
            lease_log.append({
                "node": node["node_id"],
                "work_item_id": item.work_item_id,
                "work_type": item.work_type,
                "priority": item.priority,
            })

    # 5. Snapshot metrics
    metrics = queue_metrics(store)
    result = {
        "ts": now.isoformat(),
        "stale_nodes_marked": stale,
        "expired_leases_recovered": recovered,
        "priority_aging_bumps": aging_count,
        "items_leased": leased,
        "lease_log": lease_log,
        "queue": metrics,
        "healthy_nodes": len(nodes),
    }

    append_audit(store, actor="dispatcher", action="tick", target=None, detail=result)
    return result


def _apply_priority_aging(store: Store, threshold_minutes: int) -> int:
    """Bump priority by 1 for READY items waiting longer than threshold, up to cap 100."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)).isoformat()
    with store.tx() as conn:
        cur = conn.execute(
            """
            UPDATE work_items
               SET priority = MIN(100, priority + 1),
                   updated_at = ?
             WHERE status = 'READY'
               AND created_at < ?
            """,
            (datetime.now(timezone.utc).isoformat(), cutoff),
        )
    return cur.rowcount


# Convenience: enqueue helpers used by Jake founder loop

def enqueue_signal_research(
    store: Store,
    *,
    source_uri: str,
    summary_seed: str,
    objective: str,
    source_type: str = "http",
    opportunity_id: str | None = None,
    required_capabilities: list[str] | None = None,
    priority: int = 60,
    idempotency_key: str | None = None,
    approval_lane: str = "autonomous_local",
) -> WorkItemContract:
    import uuid as _uuid
    item = WorkItemContract(
        work_type="signal_research",
        objective=objective,
        opportunity_id=opportunity_id,
        payload={"source_uri": source_uri, "source_type": source_type, "summary_seed": summary_seed},
        required_capabilities=required_capabilities or ["signal_research", "web_scrape"],
        priority=priority,
        idempotency_key=idempotency_key or f"signal:{_uuid.uuid4()}",
        approval_lane=ApprovalLane(approval_lane) if isinstance(approval_lane, str) else approval_lane,
    )
    enqueue_work_item(store, item)
    return item