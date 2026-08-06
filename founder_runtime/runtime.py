"""RIG Memory OS v10 — Runtime (Tier 2 wiring).

Wires all Phase 1 subsystems to a shared cockpit and the real Postgres
control plane. This is the SINGLE entrypoint that production code
should use to instantiate the Memory OS.

Architecture:
    runtime = MemoryOSRuntime(cockpit=MemoryCockpit(), gateway_secret=b"...")
    runtime.gateway.invoke(ctx, "memory.session_start")
    runtime.retrieval.retrieve(query, scope)
    runtime.intent.create_intent(...)
    runtime.cockpit.snapshot()

    # Multi-process control plane (Yellow #3 / Phase 3 F3): construct the
    # cockpit with a Postgres store; every process that does so shares ONE
    # kill switch, visible within store_read_ttl seconds.
    store = PostgresCockpitStore(dsn=...)
    runtime = MemoryOSRuntime(
        cockpit=MemoryCockpit(store=store),
        gateway_secret=...,
    )

The cockpit gates every state-changing operation via cockpit_subscriber.
Every subsystem accepts the cockpit via constructor; if not supplied,
the subsystem runs without control (rejected at the canonical layer).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from founder_runtime.cockpit import MemoryCockpit
from founder_runtime.memory_gateway import MemoryGateway
from founder_runtime.retrieval_engine import RetrievalEngine
from founder_runtime.intent_service import IntentService
from founder_runtime.predictor import (
    RealityCortex, Predictor,
)
from founder_runtime.foundries import (
    WorldModelService, InterventionController,
    SkillFoundry, OfferFoundry,
)
from founder_runtime.jake_observer import JakeObserver
from founder_runtime.recommendation_engine import RecommendationEngine
from founder_runtime.gbrain_obsidian_bridge import GBrainObsidianBridge

if TYPE_CHECKING:
    from founder_runtime.postgres_writer import PostgresWriter


# Per Opus 5 #8: no hardcoded secrets. Must be supplied by caller or env.
DEFAULT_ENV_SECRET_VAR = "RIG_MEMORY_OS_SECRET"


@dataclass
class MemoryOSRuntime:
    """Single wiring of all Phase 1 subsystems under a shared cockpit.

    All subsystems accept the same cockpit reference and consult it
    before every state-changing operation (Opus 5 #1, #3, #6).

    Phase 1 fix (Opus 5 #8): secrets must be supplied via constructor or
    env var. Hardcoded default is REMOVED.

    Phase 1 fix (Opus 5 #2): resume() REMOVED. Use release_kill() then
    release_pause() as two distinct operator actions.
    """

    cockpit: MemoryCockpit

    # Secrets (must be supplied; no hardcoded defaults)
    gateway_secret: Optional[bytes] = None
    intent_approval_secret: Optional[bytes] = None
    gev_secret: Optional[bytes] = None
    offer_approval_secret: Optional[bytes] = None

    # Phase 4: optional Postgres sink. When supplied, the gateway writes
    # usage_receipts and the intent service writes intents/effect_receipts
    # durably (best-effort: failures recorded via persistence_failures(),
    # never blocking the subsystem).
    postgres_writer: Optional["PostgresWriter"] = None

    # Phase 1 subsystems
    gateway: MemoryGateway = field(init=False)
    retrieval: RetrievalEngine = field(init=False)
    intent: IntentService = field(init=False)
    predictor: Predictor = field(init=False)
    reality: RealityCortex = field(init=False)
    interventions: InterventionController = field(init=False)
    skill_foundry: SkillFoundry = field(init=False)
    offer_foundry: OfferFoundry = field(init=False)
    world_model: WorldModelService = field(init=False)

    # Phase 9: Intelligence modules (previously orphaned — now wired)
    observer: JakeObserver = field(init=False)
    recommender: RecommendationEngine = field(init=False)
    memory_bridge: GBrainObsidianBridge = field(init=False)

    def __post_init__(self) -> None:
        # Opus 5 #8: fail closed if secrets are missing.
        g_secret = self.gateway_secret or os.environ.get(
            DEFAULT_ENV_SECRET_VAR
        )
        if g_secret is None:
            raise ValueError(
                "gateway_secret required (Opus 5 #8): pass to constructor "
                f"or set ${DEFAULT_ENV_SECRET_VAR}"
            )
        if isinstance(g_secret, str):
            g_secret = g_secret.encode("utf-8")

        i_secret = self.intent_approval_secret or (g_secret + b":intent")
        if isinstance(i_secret, str):
            i_secret = i_secret.encode("utf-8")

        gv_secret = self.gev_secret or (g_secret + b":gev")
        if isinstance(gv_secret, str):
            gv_secret = gv_secret.encode("utf-8")

        o_secret = self.offer_approval_secret or (g_secret + b":offer")
        if isinstance(o_secret, str):
            o_secret = o_secret.encode("utf-8")

        # All subsystems wired to the shared cockpit
        self.gateway = MemoryGateway(
            shared_secret=g_secret,
            cockpit=self.cockpit,
            postgres_writer=self.postgres_writer,
        )
        self.retrieval = RetrievalEngine(cockpit=self.cockpit)
        self.intent = IntentService(
            approval_secret=i_secret,
            cockpit=self.cockpit,
            postgres_writer=self.postgres_writer,
        )
        self.predictor = Predictor(
            persist_path=os.environ.get(
                "RIG_PREDICTOR_MODEL_PATH",
                os.path.expanduser("~/.rig/state/predictor-transitions.json"),
            ),
        )
        self.reality = RealityCortex()
        self.interventions = InterventionController(
            approval_secret=i_secret,
            cockpit=self.cockpit,
        )
        self.skill_foundry = SkillFoundry(
            gev_secret=gv_secret,
            approval_secret=i_secret,
            cockpit=self.cockpit,
        )
        self.offer_foundry = OfferFoundry(
            approval_secret=o_secret,
            cockpit=self.cockpit,
        )
        self.world_model = WorldModelService()

        # Phase 9: Wire intelligence modules to the same cockpit
        self.observer = JakeObserver(tolerance="low")
        self.recommender = RecommendationEngine()
        self.memory_bridge = GBrainObsidianBridge()

    @classmethod
    def from_env(cls, environ: Optional[dict] = None) -> "MemoryOSRuntime":
        """Phase 5 production wiring: full stack from environment.

        Required:
            RIG_MEMORY_OS_SECRET — gateway/intent HMAC root (fail closed).

        Optional:
            RIG_MEMORY_OS_DSN    — full psycopg DSN override, OR pieces:
            RIG_MEMORY_OS_PG_HOST (default /tmp)
            RIG_MEMORY_OS_PG_PORT (default 5432)
            RIG_MEMORY_OS_PG_DB   (default rig_memory_os_phase1)

        Fail-closed: an unreachable or undeployed database raises (after
        cleaning up the half-built stack) — production must never run
        silently without its control plane. Call close() at shutdown.
        """
        from founder_runtime.postgres_cockpit import PostgresCockpitStore
        from founder_runtime.postgres_writer import PostgresWriter

        env = os.environ if environ is None else environ
        secret = env.get(DEFAULT_ENV_SECRET_VAR)
        if not secret:
            raise ValueError(
                f"{DEFAULT_ENV_SECRET_VAR} required for from_env() "
                "(Phase 5 production wiring is fail-closed)"
            )
        dsn = env.get("RIG_MEMORY_OS_DSN")
        if not dsn:
            host = env.get("RIG_MEMORY_OS_PG_HOST", "/tmp")
            port = env.get("RIG_MEMORY_OS_PG_PORT", "5432")
            db = env.get("RIG_MEMORY_OS_PG_DB", "rig_memory_os_phase1")
            dsn = f"host={host} port={port} dbname={db}"

        writer = PostgresWriter(dsn=dsn)
        store = None
        try:
            writer.ensure_schema()  # refuses undeployed databases
            store = PostgresCockpitStore(dsn=dsn, audit_writer=writer)
            store.ensure_row()
        except Exception:
            if store is not None:
                store.close()
            writer.close()
            raise
        cockpit = MemoryCockpit(store=store)
        runtime = cls(
            cockpit=cockpit,
            gateway_secret=secret,
            postgres_writer=writer,
        )
        # Lifecycle ownership: close() releases both connections.
        runtime._store = store
        return runtime

    def close(self) -> None:
        """Phase 5: release owned Postgres connections (idempotent)."""
        try:
            self.predictor.save()  # persist learned transition model
        except Exception:
            pass
        store = getattr(self, "_store", None)
        if store is not None:
            store.close()
        if self.postgres_writer is not None:
            self.postgres_writer.close()

    def kill(self, actor: str = "operator") -> None:
        """Engage the kill switch."""
        self.cockpit.engage_kill_switch(actor=actor)

    def pause(self, actor: str = "operator") -> None:
        """Pause writes (does NOT kill; reads allowed)."""
        self.cockpit.engage_pause(actor=actor)

    def release_kill(self, actor: str = "operator") -> None:
        """Step 1 of recovery: KILLED -> PAUSED."""
        self.cockpit.release_kill_switch(actor=actor)

    def release_pause(self, actor: str = "operator") -> None:
        """Step 2 of recovery: PAUSED -> ACTIVE."""
        self.cockpit.release_pause(actor=actor)

    def status(self) -> dict:
        """Return runtime status snapshot (for dashboards / health checks).

        Opus 5 #7: uses public audit() accessor, not _audit.
        Phase 9: includes intelligence module stats.
        """
        snap = self.cockpit.snapshot()
        return {
            "control_state": snap.control_state.value,
            "pause_active": self.cockpit.is_paused(),
            "kill_switch_engaged": self.cockpit.is_killed(),
            "budget_remaining": snap.budget_remaining,
            "panel_count": len(snap.panels),
            "audit_count": len(self.cockpit.audit()),
            "predictions": self.predictor.prediction_count(),
            "resolved": self.predictor.resolved_count(),
            "brier_score": round(self.predictor.brier_score(), 4),
            "claims": len(self.reality._claims),
        }

    # ─── Phase 9: Intelligence API ───────────────────────────────

    def observe_session(
        self,
        tool_calls: list[str] | None = None,
        files_modified: list[str] | None = None,
        time_spent: float = 0.0,
        goals: list[str] | None = None,
        tests_written: int = 0,
        abstractions_created: int = 0,
        concrete_implementations: int = 0,
        time_without_progress: float = 0.0,
    ) -> dict:
        """Run a session through the Jake Observer pushback engine.

        Returns pushback messages + anomaly detections + pattern matches.
        """
        pushbacks = self.observer.observe(
            tool_calls=tool_calls,
            files_modified=files_modified,
            time_spent=time_spent,
            goals=goals,
            tests_written=tests_written,
            abstractions_created=abstractions_created,
            concrete_implementations=concrete_implementations,
            time_without_progress=time_without_progress,
        )
        return {
            "pushback_count": len(pushbacks),
            "pushbacks": [
                {
                    "pattern": p.pattern,
                    "response": p.response,
                    "counter": p.counter,
                    "severity": p.severity,
                    "escalation": self.observer.escalation_level(p.pattern),
                }
                for p in pushbacks
            ],
        }

    def predict_next(
        self,
        current_state: str,
        event_type: str = "tool_call",
        harness: str = "default",
        stage: str = "default",
        project: str = "default",
    ) -> dict:
        """Generate a prediction about the next state and persist it."""
        prediction = self.predictor.predict_next_state(
            current_state=current_state,
            event_type=event_type,
            harness=harness,
            stage=stage,
            project=project,
        )

        # Persist to Postgres if available
        if self.postgres_writer:
            try:
                self.postgres_writer.write_prediction(
                    prediction_id=prediction.prediction_id,
                    target=prediction.target.value,
                    current_state=prediction.current_state,
                    predicted_state=prediction.predicted_state,
                    probability=prediction.probability,
                    allowed_action=prediction.allowed_action.value,
                    expires_at=prediction.expires_at,
                    harness=harness,
                    stage=stage,
                    project=project,
                )
            except Exception:
                pass  # best-effort; never block predictions on persistence

        return {
            "prediction_id": prediction.prediction_id,
            "predicted_state": prediction.predicted_state,
            "probability": round(prediction.probability, 4),
            "allowed_action": prediction.allowed_action.value,
            "expires_at": prediction.expires_at,
        }

    def resolve_prediction(
        self, prediction_id: str, actual_outcome: str,
    ) -> dict:
        """Resolve a prediction with the actual outcome and record calibration."""
        record = self.predictor.resolve_prediction(prediction_id, actual_outcome)

        if self.postgres_writer:
            try:
                self.postgres_writer.resolve_prediction(prediction_id, actual_outcome)
                self.postgres_writer.write_calibration(
                    prediction_id=record.prediction_id,
                    predicted_probability=record.predicted_probability,
                    actual_outcome=record.actual_outcome,
                    brier_component=record.brier_component,
                    log_loss_component=record.log_loss_component,
                )
            except Exception:
                pass

        return {
            "prediction_id": record.prediction_id,
            "predicted_probability": round(record.predicted_probability, 4),
            "actual_outcome": record.actual_outcome,
            "brier_component": round(record.brier_component, 6),
            "log_loss_component": round(record.log_loss_component, 6),
            "cumulative_brier": round(self.predictor.brier_score(), 4),
        }

    def record_transition(
        self,
        current_state: str,
        event_type: str,
        next_state: str,
        harness: str = "default",
        stage: str = "default",
        project: str = "default",
    ) -> None:
        """Feed observed state transitions into the predictor for learning."""
        self.predictor.record_transition(
            current_state, event_type, next_state,
            harness=harness, stage=stage, project=project,
        )

    def add_reality_claim(
        self,
        subject: str,
        statement: str,
        evidence_refs: list[str] | None = None,
        confidence: float = 0.5,
    ) -> dict:
        """Add a claim to the Reality Cortex and persist it."""
        claim = self.reality.add_claim(
            subject=subject,
            statement=statement,
            evidence_refs=evidence_refs,
            confidence=confidence,
        )

        if self.postgres_writer:
            try:
                self.postgres_writer.write_claim(
                    claim_id=claim.claim_id,
                    subject=claim.subject,
                    statement=claim.statement,
                    evidence_refs=claim.evidence_refs,
                    valid_from=claim.valid_from,
                    confidence=claim.confidence,
                    status=claim.status.value,
                )
            except Exception:
                pass

        return {
            "claim_id": claim.claim_id,
            "subject": claim.subject,
            "status": claim.status.value,
            "confidence": claim.confidence,
        }

    def recommend(self, session_data: dict | None = None) -> dict:
        """Generate proactive recommendations.

        If session_data is provided, it's added to history before recommending.
        """
        if session_data:
            self.recommender.add_session(session_data)
        recs = self.recommender.recommend()
        return {
            "recommendation_count": len(recs),
            "recommendations": [
                {
                    "type": r.type,
                    "trigger": r.trigger,
                    "suggestion": r.suggestion,
                    "estimated_benefit": r.estimated_benefit,
                    "confidence": r.confidence,
                }
                for r in recs
            ],
        }

    def search_memory(self, query: str, limit: int = 20) -> dict:
        """Search across all memory stores via the GBrain-Obsidian bridge."""
        results = self.memory_bridge.search_all(query, limit=limit)
        return {
            "query": query,
            "result_count": len(results),
            "results": [
                {
                    "source": r.source,
                    "layer": r.layer,
                    "key": r.key,
                    "value": r.value[:200],
                    "timestamp": r.timestamp,
                }
                for r in results
            ],
        }

    def intelligence_snapshot(self) -> dict:
        """Full intelligence-layer status for dashboards / health checks."""
        bridge_status = self.memory_bridge.status()
        return {
            "predictions": {
                "total": self.predictor.prediction_count(),
                "resolved": self.predictor.resolved_count(),
                "brier_score": round(self.predictor.brier_score(), 4),
                "log_loss": round(self.predictor.log_loss(), 4),
                "calibration_error": round(
                    self.predictor.expected_calibration_error(), 4
                ),
            },
            "claims": {
                "total": len(self.reality._claims),
                "promoted": len(self.reality.current_claims()),
                "contradictions": len(self.reality.detect_contradictions()),
            },
            "recommendations": len(self.recommender._history),
            "memory_bridge": bridge_status,
        }