"""Phase 6 — Night compounding window.

Hours 20:00-05:00 local. Unattended fleet hours for:
- market and competitor research
- source verification
- vertical intelligence
- offer and audit package drafts
- dormant landing pages and demos
- code and workflow prototypes
- lead/company enrichment
- regression and quality evaluation
- knowledge cleanup and linking
- postmortems
- next-day meeting and decision preparation

The runtime's deterministic code seeds this; the worker then executes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    OpportunityContract,
    OpportunityStage,
    WorkItemContract,
    ApprovalLane,
)
from .store import (
    Store,
    enqueue_work_item,
    list_opportunities,
    upsert_opportunity,
    append_audit,
)


logger = logging.getLogger(__name__)


NIGHT_WORK_TYPES = (
    ("market_map_refresh", "Refresh a vertical market map with new signals.", 40),
    ("knowledge_cleanup", "Reconcile GBrain/Obsidian knowledge; link orphans.", 30),
    ("landing_build", "Produce a dormant landing page / demo asset for a vertical.", 35),
    ("experiment_design", "Design the smallest falsifiable experiment for a candidate.", 45),
    ("offer_draft", "Draft a $7.5K pricing-audit offer or $40K enterprise offer.", 50),
)


def run_night_cycle(store: Store, *, max_work_items: int = 20) -> dict[str, Any]:
    """Seed and dispatch one cycle of night compounding work.

    Returns metrics for the night brief.
    """
    enqueued: list[str] = []
    skipped = 0

    # 1. Look at current portfolio — what needs refreshing?
    opps = list_opportunities(store, limit=100)

    # 2. Identify verticals in QUALIFIED or VALIDATING that need a market map refresh
    verticals_list = sorted(v for v in {
        o.get("vertical") for o in opps
        if o.get("vertical") and o["stage"] in {
            OpportunityStage.QUALIFIED.value, OpportunityStage.VALIDATING.value,
            OpportunityStage.EXPERIMENT_READY.value, OpportunityStage.EXPERIMENTING.value,
        }
    } if v)
    if not verticals_list:
        # Default to Mike's signed vertical starting set
        verticals_list = sorted(["construction", "law", "medspa", "healthcare",
                                "dentistry", "cpa", "services", "manufacturing"])

    # 3. Enqueue market_map_refresh for each active vertical
    for vt in verticals_list:
        if len(enqueued) >= max_work_items:
            break
        item = WorkItemContract(
            work_type="market_map_refresh",
            objective=f"Refresh {vt} market map: new signals, new sources, fresh evidence timestamps.",
            payload={"vertical": vt},
            required_capabilities=["signal_research", "vertical_research"],
            priority=40,
            idempotency_key=f"night:mktmap:{vt}:{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            approval_lane=ApprovalLane.autonomous_local,
        )
        enqueue_work_item(store, item)
        enqueued.append(item.work_item_id)

    # 4. Enqueue knowledge_cleanup for the largest stale opportunity (if any)
    stale = sorted(opps, key=lambda o: o.get("next_action_due_at") or "")
    if stale and len(enqueued) < max_work_items:
        opp = stale[0]
        item = WorkItemContract(
            work_type="knowledge_cleanup",
            objective=f"Reconcile evidence chain for '{opp.get('title', 'unknown')}' and link orphan sources.",
            opportunity_id=opp["opportunity_id"],
            payload={"opportunity_id": opp["opportunity_id"]},
            required_capabilities=["synthesis"],
            priority=30,
            idempotency_key=f"night:knowledge:{opp['opportunity_id']}:{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            approval_lane=ApprovalLane.autonomous_local,
        )
        enqueue_work_item(store, item)
        enqueued.append(item.work_item_id)

    # 5. Enqueue landing_build for the top-scoring opportunity (dormant asset, no send)
    if opps:
        scored = sorted(opps, key=lambda o: float(o.get("priority") or 0), reverse=True)
        top = scored[0]
        if len(enqueued) < max_work_items and top.get("stage") in {
            OpportunityStage.QUALIFIED.value, OpportunityStage.EXPERIMENT_READY.value,
            OpportunityStage.SELL_READY.value, OpportunityStage.BUILD_READY.value,
        }:
            item = WorkItemContract(
                work_type="landing_build",
                objective=f"Build dormant landing page for '{top['title']}' — never publish without Mike sign-off.",
                opportunity_id=top["opportunity_id"],
                payload={"opportunity_id": top["opportunity_id"], "dormant": True},
                required_capabilities=["landing_build", "creative_qa"],
                priority=35,
                idempotency_key=f"night:landing:{top['opportunity_id']}:{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                approval_lane=ApprovalLane.autonomous_local,
            )
            enqueue_work_item(store, item)
            enqueued.append(item.work_item_id)

    # 6. Enqueue experiment_design for any opportunity lacking one
    designed = {o.get("opportunity_id") for o in opps if o.get("stage") == OpportunityStage.EXPERIMENT_READY.value}
    queued_ids = {i.opportunity_id for i in [WorkItemContract(work_type="x", objective="x", idempotency_key="x")] if i.opportunity_id}
    # Actually grab which opportunities already have open experiment_design work items
    from .store import queue_metrics
    with store.read() as conn:
        existing = conn.execute(
            "SELECT DISTINCT opportunity_id FROM work_items WHERE work_type='experiment_design' AND opportunity_id IS NOT NULL"
        ).fetchall()
    has_design = {r[0] for r in existing}

    needs_design = [
        o for o in opps
        if o["stage"] == OpportunityStage.QUALIFIED.value
        and o["opportunity_id"] not in has_design
        and len(enqueued) < max_work_items
    ]
    for o in needs_design[:3]:
        item = WorkItemContract(
            work_type="experiment_design",
            objective=f"Design the smallest falsifiable experiment for '{o['title']}'.",
            opportunity_id=o["opportunity_id"],
            payload={"opportunity_id": o["opportunity_id"]},
            required_capabilities=["synthesis", "strategy"],
            priority=45,
            idempotency_key=f"night:exp:{o['opportunity_id']}:{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            approval_lane=ApprovalLane.autonomous_local,
        )
        enqueue_work_item(store, item)
        enqueued.append(item.work_item_id)

    append_audit(
        store,
        actor="night_cycle",
        action="run_night_cycle",
        target=None,
        detail={"enqueued": len(enqueued), "skipped": skipped, "ts": datetime.now(timezone.utc).isoformat()},
    )

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "enqueued_count": len(enqueued),
        "enqueued_work_items": enqueued,
        "skipped": skipped,
        "verticals_seeded": verticals_list,
        "queue_after": queue_metrics(store),
    }


def run_closeout(store: Store) -> dict[str, Any]:
    """End-of-night closeout: write a night summary into the audit log."""
    from .store import queue_metrics
    summary = {
        "queue_state": queue_metrics(store),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    append_audit(store, actor="night_cycle", action="closeout", target=None, detail=summary)
    return summary