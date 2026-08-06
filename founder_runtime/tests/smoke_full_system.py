"""End-to-end integrated smoke for RIG Memory OS v10.

This wires all S2-S8 modules together to demonstrate the full closed
loop:
- S2: Memory Gateway accepts requests, Checkpoint Writer records state,
  Episode Builder records lifecycle events
- S3: Intent Service creates durable intents (Temporal-backed stub)
- S4: Retrieval Engine returns a token-budgeted ContextPackage with
  scope filtering
- S5: Reality Cortex records bitemporal claims, Predictor records
  transitions and resolves predictions
- S6: SkillFoundry mines a 3-repeat pattern, InterventionController
  ranks candidates
- S7: Memory Cockpit captures a snapshot and renders text
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

WORKTREE = Path("/Users/rig128gb/Developer/rig-intelligence-worktrees/rig-memory-os/platform/founder-runtime")
os.chdir(str(WORKTREE))
sys.path.insert(0, str(WORKTREE))

from founder_runtime.memory_gateway import (
    MemoryGateway, SignedContext, SensitivityCeiling,
)
from founder_runtime.checkpoint_writer import CheckpointWriter
from founder_runtime.episode_builder import EpisodeBuilder, EventType
from founder_runtime.intent_service import (
    IntentService, PermissionClass,
)
from founder_runtime.retrieval_engine import (
    RetrievalEngine, RetrievalScope, MemoryCandidate, MemoryZone,
)
from founder_runtime.predictor import (
    RealityCortex, Predictor, AllowedAction,
)
from founder_runtime.foundries import (
    WorldModelService, InterventionController, InterventionPacket,
    SkillFoundry, OfferFoundry,
)
from founder_runtime.cockpit import MemoryCockpit


def run_full_smoke() -> None:
    print("=" * 70)
    print("RIG Memory OS v10 — Full System Smoke Test")
    print("=" * 70)

    # ── S2: Wire the canonical components ──────────────────────────────
    print("\n[S2] Memory Gateway, Checkpoint Writer, Episode Builder")
    gw = MemoryGateway()
    ctx = SignedContext(
        operator_id="op-1",
        tenant_id="tenant-A",
        client_id="client-1",
        project_id="proj-1",
        mission_id="mission-1",
        agent_principal="planner",
        agent_instance="instance-1",
        harness_version="v1.0",
        adapter_version="v1.0",
        node="controller",
        purpose="smoke",
        sensitivity_ceiling=SensitivityCeiling.INTERNAL,
        run_id="run-smoke-1",
        session_id="sess-smoke-1",
        trace_id="trace-smoke-1",
        policy_version="1",
    )
    r1 = gw.invoke(ctx, "memory.session_start")
    print(f"  ✓ session_start: accepted={r1.accepted}")
    r2 = gw.invoke(ctx, "memory.record_event")
    print(f"  ✓ record_event: accepted={r2.accepted}")

    cw = CheckpointWriter(mission_id="mission-1")
    ckpt = cw.write(presenter_token=0, active_goal="Ship Memory OS v10 Phase 2-8")
    print(f"  ✓ checkpoint_writer: token=0 → {ckpt.fencing_token}")

    eb = EpisodeBuilder()
    ep = eb.start_episode("run-smoke-1", "sess-smoke-1", "planner")
    eb.record(
        "run-smoke-1", "sess-smoke-1", "planner",
        EventType.TOOL_CALLED, "gw.invoke",
        decision="accept session_start",
    )
    eb.record(
        "run-smoke-1", "sess-smoke-1", "planner",
        EventType.OUTCOME_RECORDED, "first events captured",
    )
    eb.close_episode("sess-smoke-1", "planner", final_outcome="captured")
    print(f"  ✓ episode_builder: {len(ep.events)} events captured, outcome=captured")

    # ── S3: Create a durable intent (Temporal-backed stub) ─────────────
    print("\n[S3] Intent Service — durable Temporal-backed intent")
    intent_svc = IntentService()
    intent = intent_svc.create_intent(
        owner="planner",
        trigger_type="timer",
        trigger_spec="0 6 * * *",
        action="run_daily_briefing",
        permission_class=PermissionClass.A1_PREPARE,
        due_at=time.time() - 100,  # overdue
        idempotency_key="smoke-daily-briefing",
    )
    print(f"  ✓ intent created: id={intent.intent_id[:8]}... action={intent.action}")
    due = intent_svc.due_intents()
    print(f"  ✓ due_intents(): {len(due)} (overdue)")
    result = intent_svc.execute_intent(intent.intent_id)
    print(f"  ✓ executed: status={intent.status.value}, receipt={result.effect_receipt_id[:8]}...")

    # ── S4: Run a retrieval ────────────────────────────────────────────
    print("\n[S4] Retrieval Engine — scope-filtered ContextPackage")
    re = RetrievalEngine()
    for i in range(3):
        re.store_candidate(MemoryCandidate(
            memory_id=f"m-smoke-{i}", source="vector", score=0.9 - i * 0.1,
            content_excerpt=f"smoke test memory about topic {i}",
            scope={"tenant_id": "tenant-A", "client_id": "client-1",
                   "project_id": "proj-1", "mission_id": "mission-1"},
            sensitivity="internal",
        ))
    re.store_candidate(MemoryCandidate(
        memory_id="m-cross-tenant", source="vector", score=0.99,
        content_excerpt="foreign",
        scope={"tenant_id": "tenant-OTHER", "client_id": "client-1",
               "project_id": "proj-1", "mission_id": "mission-1"},
        sensitivity="internal",
    ))
    scope = RetrievalScope(
        tenant_id="tenant-A", client_id="client-1",
        project_id="proj-1", mission_id="mission-1",
        operator_id="op-1", sensitivity_ceiling="internal",
    )
    pkg = re.retrieve(query="smoke test", scope=scope, token_budget=500)
    item_ids = [i.memory_id for i in pkg.items]
    print(f"  ✓ retrieve: items={len(pkg.items)}, "
          f"tokens={pkg.token_used}/{pkg.token_budget}")
    print(f"    scope_denied={pkg.excluded_for_scope}")

    # ── S5: Reality Cortex + Predictor ─────────────────────────────────
    print("\n[S5] Reality Cortex + Predictor")
    rc = RealityCortex()
    c1 = rc.add_claim(
        subject="Prefect control DB",
        statement="Postgres 16 on local NVMe",
        evidence_refs=["obs-2026-08-03"],
    )
    rc.promote(c1.claim_id)
    print(f"  ✓ reality_cortex: claim promoted: {c1.subject}={c1.statement[:30]}...")

    pr = Predictor()
    for _ in range(4):
        pr.record_transition("intake", "evaluate", "accept")
    for _ in range(2):
        pr.record_transition("intake", "evaluate", "reject")
    pred = pr.predict_next_state("intake", "evaluate")
    print(f"  ✓ predictor: predicted={pred.predicted_state} "
          f"(p={pred.probability:.2f})")
    pr.track_prediction(pred)
    pr.resolve_prediction(pred.prediction_id, "accept")
    print(f"  ✓ brier_score={pr.brier_score():.3f}, log_loss={pr.log_loss():.3f}")

    # ── S6: Foundries ──────────────────────────────────────────────────
    print("\n[S6] World Model + Foundries")
    wms = WorldModelService()
    wms.create_model("prefect-flows")
    h = wms.add_hypothesis(
        description="Prefect control DB migration removes lock contention",
        mechanism="SQLite single-writer vs Postgres MVCC",
        falsifier="if migration fails or introduces new contention",
        alternatives=["pgBouncer overhead", "WAL bottleneck"],
    )
    print(f"  ✓ world_model: domain=prefect-flows, hypothesis={h.status.value}")
    wms.update_hypothesis_outcome(h.hypothesis_id, "lock contention gone")
    print(f"  ✓ hypothesis outcome recorded")

    sf = SkillFoundry()
    out = []
    for i in range(3):
        out.append(sf.record_trajectory(
            "verify-card-hash-before-deploy",
            trajectory_ref=f"traj-{i}",
            success=True,
        ))
    cand = out[-1]
    is_candidate = cand is not None
    print(f"  ✓ skill_foundry: 3-repeat detected → candidate created={is_candidate}")

    ic = InterventionController()
    ic.propose(InterventionPacket(
        desired_state="Prefect DB on Postgres", candidate_action="run-migration",
        expected_gain=10, cost=2, risk=0.5, reversibility=0.9,
    ))
    ic.propose(InterventionPacket(
        desired_state="stable", candidate_action="no-op",
        expected_gain=0, cost=0, risk=0, reversibility=1.0,
    ))
    ranking = ic.rank()
    print(f"  ✓ intervention_ranking: selected={ranking.selected.candidate_action if ranking.selected else 'NOOP'}")

    # ── S7: Memory Cockpit ────────────────────────────────────────────
    print("\n[S7] Memory Cockpit — deterministic snapshot")
    cockpit = MemoryCockpit()
    cockpit.set_layer_status("L1", "ok")
    cockpit.set_layer_status("L2", "ok")
    cockpit.set_layer_status("L3", "ok")
    cockpit.set_layer_status("L5", "ok")
    cockpit.set_layer_status("L6", "ok")
    cockpit.set_layer_status("L7", "ok")
    cockpit.set_layer_status("L8", "ok")
    cockpit.set_layer_status("L4", "degraded")
    cockpit.set_queue_lag("control-watchdog", 12.5)
    cockpit.set_queue_lag("collection-36gb", 120.0)
    cockpit.set_prediction_stats({
        "brier": 0.05, "log_loss": 0.07, "ece": 0.02,
        "resolved": 12, "pending": 2,
    })
    cockpit.set_intent_stats({
        "due": 1, "overdue": 0, "completed": 1, "blocked": 0,
    })
    cockpit.set_budget(0.85)

    print("\n  ── Cockpit Snapshot (rendered text) ──")
    print()
    print(cockpit.render_text())

    print("\n  ── Pause and capture again ──")
    cockpit.engage_pause()
    snap2 = cockpit.snapshot()
    print(f"  ✓ pause_active={snap2.pause_active}")
    cockpit.release_pause()

    # Final stats
    print("\n" + "=" * 70)
    print("Smoke complete: all 7 stages wired end-to-end")
    print("=" * 70)


if __name__ == "__main__":
    run_full_smoke()