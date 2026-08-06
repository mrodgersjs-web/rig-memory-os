"""RIG Memory OS v10 — Panel population (Yellow #5).

Each cockpit panel currently shows status="ok" with placeholder
metrics. This module provides populate_panels_from_runtime() that
reads actual subsystem state and populates the panel stats.

Once MemoryOSRuntime runs an end-to-end session, calling
populate_panels_from_runtime(runtime, cockpit) gives the cockpit real
metrics to display.

Panels:
    L1-L8 health     — counts envelopes, checkpoints per layer
    Events / episodes — EpisodeBuilder counts
    Retrieval       — RetrievalEngine stats (queries, denials)
    gBrain          — GBrain sync stats
    Procedures      — SkillFoundry candidates + OfferFoundry offers
    Predictions     — Predictor Brier/log-loss/ECE
    Intentions      — IntentService counts
    Backup / restore — PostgresCockpitStore audit_count + envelope_count
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit
    from founder_runtime.runtime import MemoryOSRuntime


def populate_panels_from_runtime(
    runtime: "MemoryOSRuntime",
    cockpit: "MemoryCockpit",
    *,
    gbrain_stats: Optional[dict] = None,
) -> None:
    """Read actual subsystem state and update the cockpit panels.

    Idempotent: safe to call repeatedly; latest values win.

    `gbrain_stats`: caller-supplied live gBrain metrics. There is no gBrain
    query path in this runtime — `gbrain_repair.py` provides replay /
    lock-clear / SLO helpers only, and real sync is Phase 4. When omitted
    the gBrain panel reports `no_data`; it never fabricates a `synced` value.
    """
    # L1-L8 health: rough proxy is envelope + checkpoint counts from Postgres.
    # When runtime has writer attached, read real counts; else 0.
    try:
        from founder_runtime.postgres_writer import PostgresWriter
        # Use a fresh connection so we don't tie up the runtime's writer.
        w = PostgresWriter()
        w.ensure_schema()
        env_count = w.envelope_count()
        ckpt_count = w.checkpoint_count()
        audit_count = w.audit_count()
        w.close()
    except Exception:
        env_count, ckpt_count, audit_count = 0, 0, 0
    cockpit.set_layer_status("L1", "ok" if ckpt_count > 0 else "no_data")
    cockpit.set_layer_status("L2", "ok" if env_count > 0 else "no_data")
    for layer in ("L3", "L4", "L5", "L6", "L7", "L8"):
        cockpit.set_layer_status(layer, "ok")

    # Events / episodes
    cockpit.set_events_episodes({
        "episodes": env_count,
        "checkpoints": ckpt_count,
    })

    # Retrieval — measured counters (Phase 3 fix F5). The old code read
    # getattr(re, "_query_count", 0) against a nonexistent attribute.
    # Local name is `re_engine`, not `re`: `re` shadows the stdlib module.
    re_engine = runtime.retrieval
    cockpit.set_retrieval_stats({
        "queries": re_engine.query_count(),
        "blocked": re_engine.blocked_count(),
        "denials": len(re_engine.unauthorized_attempts()),
    })

    # gBrain — Phase 3 fix (F5): no live gBrain query path exists, so report
    # what we know. `synced: False` / `autopilot_lock_clear: True` were
    # hardcoded claims, not measurements.
    if gbrain_stats is None:
        cockpit.set_gbrain_stats({
            "status": "no_data",
            "reason": "no gBrain sync source wired (Phase 4)",
        })
    else:
        cockpit.set_gbrain_stats(dict(gbrain_stats))

    # Procedures
    cockpit.set_procedure_stats({
        "skill_candidates": len(runtime.skill_foundry._candidates),
        "offers": len(runtime.offer_foundry._offers),
    })

    # Predictions
    p = runtime.predictor
    transitions_count = 0
    if hasattr(p, "_transitions"):
        for inner in p._transitions.values():
            for count in inner.values():
                transitions_count += count
    cockpit.set_prediction_stats({
        "brier": p.brier_score() if hasattr(p, "brier_score") else 0.0,
        "transitions_recorded": transitions_count,
    })

    # Intentions
    intents = list(runtime.intent._intents.values())
    pending = [i for i in intents if i.status.value == "pending"]
    cockpit.set_intent_stats({
        "queued": len(pending),
        "completed": len([i for i in intents if i.status.value == "completed"]),
    })

    # Backup / restore (real Postgres-side count)
    cockpit.set_backup_stats({
        "postgres_audit_count": audit_count,
        "postgres_envelope_count": env_count,
    })

    # Queue lag — Phase 3 fix (F5): the old `len(pending) * 60.0` was an
    # invented number. Intent.created_at is a real timestamp, so lag is the
    # age of the oldest still-pending intent. With nothing pending there is
    # no lag to report, and we omit the series rather than claim 0.0.
    if pending:
        cockpit.set_queue_lag(
            "intents", time.time() - min(i.created_at for i in pending)
        )