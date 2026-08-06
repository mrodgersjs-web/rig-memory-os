"""Phase 3 — Jake founder loop.

Maintains the opportunity portfolio: ORIENT → SENSE → RANK → DECIDE → DELEGATE → VERIFY → LEARN.

This is the only place that creates missions and changes opportunity stage.
It does not run work; it enqueues typed WorkItems that workers lease.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .contracts import (
    OpportunityContract,
    OpportunityStage,
    WorkItemContract,
    WorkItemStatus,
    ApprovalLane,
    Verdict,
)
from .store import (
    Store,
    upsert_opportunity,
    list_opportunities,
    enqueue_work_item,
    queue_metrics,
    append_audit,
)


PORTFOLIO_BUCKETS = (
    "FOCUS",
    "WATCH",
    "TEST",
    "BUILD",
    "SELL_READY",
)


def founder_review(store: Store) -> dict[str, Any]:
    """Hourly founder review. Returns up to three portfolio changes.

    Reads current opportunities, ranks them, picks at most three actions:
    - Promote (move stage forward)
    - Hold (leave stage, set next_action)
    - Kill (set stage=KILLED with reason)

    Pure deterministic code; the model is only used to explain or challenge.
    """
    opps = list_opportunities(store, limit=500)
    changes: list[dict[str, Any]] = []

    # FOCUS: top 3 by composite score, stage >= QUALIFIED
    ranked = sorted(
        [o for o in opps if o["stage"] in {OpportunityStage.QUALIFIED.value,
                                            OpportunityStage.EXPERIMENT_READY.value,
                                            OpportunityStage.EXPERIMENTING.value,
                                            OpportunityStage.SELL_READY.value,
                                            OpportunityStage.BUILD_READY.value}],
        key=lambda o: _composite(o),
        reverse=True,
    )

    # Promote the top candidate to EXPERIMENT_READY if currently VALIDATING
    for o in ranked[:3]:
        if o["stage"] == OpportunityStage.QUALIFIED.value and o.get("confidence", 0) >= 5:
            changes.append({"opportunity_id": o["opportunity_id"], "action": "hold",
                            "next_action": f"Run smallest test for {o['title']}"})

    # Kill any opportunity with confidence < 2 and no movement in 14 days
    cutoff = (datetime.now(timezone.utc).timestamp()) - (14 * 86400)
    for o in opps:
        updated = o.get("next_action_due_at") or o.get("updated_at")
        try:
            updated_ts = datetime.fromisoformat(updated).timestamp() if updated else 0
        except Exception:
            updated_ts = 0
        if (o.get("confidence", 0) < 2
                and updated_ts < cutoff
                and o["stage"] not in {OpportunityStage.WON.value, OpportunityStage.LOST.value,
                                       OpportunityStage.KILLED.value, OpportunityStage.PARKED.value}):
            changes.append({"opportunity_id": o["opportunity_id"], "action": "kill",
                            "reason": "low confidence + stale"})

    # Cap at three changes per review (handoff §6.4)
    changes = changes[:3]

    for ch in changes:
        append_audit(store, actor="jake", action=ch["action"], target=ch["opportunity_id"], detail=ch)

    return {"changes": changes, "ranked_focus": [o["opportunity_id"] for o in ranked[:3]],
            "ts": datetime.now(timezone.utc).isoformat()}


def _composite(o: dict[str, Any]) -> float:
    weights = {
        "direction_fit": 2.0, "pain_evidence": 1.5, "urgency_evidence": 1.0,
        "buyer_access": 1.0, "proof_advantage": 1.5, "speed_to_test": 0.5,
        "recurrence_potential": 1.0, "ip_reuse_potential": 0.5,
        "delivery_burden": -0.5, "confidence": 0.5,
    }
    return sum(weights.get(k, 0) * (o.get(k) or 0) for k in weights)


def morning_brief(store: Store) -> str:
    """Decision-dense morning brief — top of founder console."""
    metrics = queue_metrics(store)
    opps = list_opportunities(store, limit=200)
    focus = [o for o in opps if o["stage"] in {OpportunityStage.QUALIFIED.value,
                                                OpportunityStage.EXPERIMENT_READY.value,
                                                OpportunityStage.EXPERIMENTING.value}][:5]
    sell_ready = [o for o in opps if o["stage"] == OpportunityStage.SELL_READY.value][:5]
    killed = [o for o in opps if o["stage"] == OpportunityStage.KILLED.value][:5]

    lines = [
        "## RIG Morning Brief",
        f"- generated: {datetime.now(timezone.utc).isoformat()}",
        f"- queue: {metrics}",
        "",
        "### Focus (top 5 by composite)",
    ]
    for o in focus:
        lines.append(f"- [{o['stage']}] {o['title']} (priority={o['priority']:.1f}, conf={o['confidence']:.1f})")
    lines.append("")
    lines.append("### Sell-ready (Mike sign-off lane)")
    for o in sell_ready:
        lines.append(f"- {o['title']} (vertical={o.get('vertical')})")
    lines.append("")
    lines.append(f"### Recently killed: {len(killed)}")
    return "\n".join(lines)


def enqueue_mission(
    store: Store,
    *,
    work_type: str,
    objective: str,
    opportunity_id: str | None = None,
    required_capabilities: list[str] | None = None,
    priority: int = 60,
    payload: dict[str, Any] | None = None,
    approval_lane: str = "autonomous_local",
    max_attempts: int = 2,
    idempotency_key: str | None = None,
) -> WorkItemContract:
    """Jake creates a typed mission."""
    import uuid as _uuid
    item = WorkItemContract(
        work_type=work_type,
        objective=objective,
        opportunity_id=opportunity_id,
        required_capabilities=required_capabilities or [],
        priority=priority,
        payload=payload or {},
        idempotency_key=idempotency_key or f"mission:{work_type}:{_uuid.uuid4()}",
        approval_lane=ApprovalLane(approval_lane) if isinstance(approval_lane, str) else approval_lane,
        max_attempts=max_attempts,
    )
    enqueue_work_item(store, item)
    append_audit(store, actor="jake", action="enqueue_mission", target=item.work_item_id,
                  detail={"work_type": work_type, "objective": objective, "priority": priority})
    return item