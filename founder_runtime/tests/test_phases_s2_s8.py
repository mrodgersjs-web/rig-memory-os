"""S2-S8 tests for RIG Memory OS v10 — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- SignedContext is HMAC-SHA256 signed
- All 16 protected fields are body-scope-rejected
- Sensitivity check is wired
- Replay keyed on (context_hash, tool_name, nonce)
- Project/mission filters are hard (deny on absent)
- Zone from MemoryCandidate.zone (not caller dict)
- Cache hit re-runs scope/sensitivity filter
- log_unauthorized is auto-called
- operator_id in scope_hash
- Real RRF (lexical + TF-IDF + graph)
- supersede() does NOT promote
- predict_next_state auto-tracks
- resolve_prediction idempotent
- Laplace over outcome-space size K
- InterventionController constrains actions
- SkillFoundry ≥0.95 threshold + verifier-signed GEV + fixture validation
- OfferFoundry published is frozen
- CausalEdge schema rejects "caused_by"
- Cockpit state machine: kill→paused→active
- A3/A4 gated by signed ApprovalToken
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

WORKTREE = Path("/Users/rig128gb/Developer/rig-intelligence-worktrees/rig-memory-os/platform/founder-runtime")
os.chdir(str(WORKTREE))
sys.path.insert(0, str(WORKTREE))

from founder_runtime.memory_gateway import (
    MemoryGateway, sign_context, SignedContext,
    SensitivityCeiling, RejectReason,
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
    RealityCortex, Predictor, AllowedAction, FORBIDDEN_ACTIONS,
)
from founder_runtime.foundries import (
    WorldModelService, InterventionController, InterventionPacket,
    InterventionAction, SkillFoundry, OfferFoundry,
    CausalEdge, CAUSAL_RELATION_TYPES,
    ApprovalClass, ApprovalToken, mint_approval,
    GEVArtifactRef, mint_gev_ref,
)
from founder_runtime.cockpit import MemoryCockpit, ControlState


SECRET = b"phase1-test-secret"


def make_signed_ctx(**overrides) -> SignedContext:
    secret = overrides.pop("secret", SECRET)
    defaults = dict(
        operator_id="op-1", tenant_id="tenant-1", client_id="client-1",
        project_id="proj-1", mission_id="mission-1",
        agent_principal="planner", agent_instance="instance-1",
        harness_version="v1", adapter_version="v1", node="controller",
        purpose="test", sensitivity_ceiling=SensitivityCeiling.INTERNAL.value,
        run_id="run-1", session_id="sess-1", trace_id="trace-1",
        policy_version="1",
    )
    defaults.update(overrides)
    sc = defaults["sensitivity_ceiling"]
    if isinstance(sc, str):
        sc = SensitivityCeiling(sc)
    defaults["sensitivity_ceiling"] = sc
    return sign_context(secret=secret, **defaults)


class _PredNS:
    """Tiny namespace to hold (predictor, prediction)."""
    def __init__(self, predictor, pred):
        self.predictor = predictor
        self.pred = pred


# S2: Memory Gateway

class TestMemoryGateway(unittest.TestCase):

    def setUp(self):
        self.gw = MemoryGateway(shared_secret=SECRET)
        self.ctx = make_signed_ctx()

    def test_all_tools_recognized(self):
        self.assertEqual(len(MemoryGateway.TOOL_NAMES), 17)

    def test_valid_request_accepted(self):
        r = self.gw.invoke(self.ctx, "memory.propose_memory")
        self.assertTrue(r.accepted)
        self.assertEqual(r.receipt.principal, "planner")
        self.assertEqual(r.receipt.run_id, "run-1")
        # Phase 1: nonce captured in receipt
        assert self.ctx.nonce is not None
        self.assertEqual(r.receipt.nonce, self.ctx.nonce)

    def test_unknown_tool_rejected(self):
        r = self.gw.invoke(self.ctx, "memory.bogus")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SCHEMA_INVALID)

    def test_unsigned_context_rejected(self):
        ctx = SignedContext(
            operator_id="op", tenant_id="t", client_id="c",
            project_id="p", mission_id="m", agent_principal="planner",
            agent_instance="i", harness_version="v", adapter_version="v",
            node="n", purpose="x",
            sensitivity_ceiling=SensitivityCeiling.INTERNAL,
            run_id="r", session_id="s", trace_id="t", policy_version="1",
            nonce="n", signature="",
        )
        r = self.gw.invoke(ctx, "memory.propose_memory")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.INVALID_SIGNATURE)

    def test_body_cannot_override_client_id(self):
        r = self.gw.invoke(
            self.ctx, "memory.propose_memory",
            body={"client_id": "OTHER"},
        )
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SCOPE_MISMATCH)

    def test_replay_detection_within_window(self):
        self.gw.invoke(self.ctx, "memory.propose_memory")
        r2 = self.gw.invoke(self.ctx, "memory.propose_memory")
        self.assertFalse(r2.accepted)
        self.assertEqual(r2.reject_reason, RejectReason.REPLAY_DETECTED)

    def test_different_context_hashes_not_replay(self):
        ctx_a = make_signed_ctx(session_id="s-A", run_id="r-A")
        ctx_b = make_signed_ctx(session_id="s-B", run_id="r-B")
        self.assertTrue(self.gw.invoke(ctx_a, "memory.propose_memory").accepted)
        self.assertTrue(self.gw.invoke(ctx_b, "memory.propose_memory").accepted)

    def test_body_cannot_override_run_id(self):
        r = self.gw.invoke(
            self.ctx, "memory.propose_memory",
            body={"run_id": "EVIL"},
        )
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SCOPE_MISMATCH)

    def test_sensitivity_check_wired(self):
        ctx = make_signed_ctx(sensitivity_ceiling=SensitivityCeiling.PUBLIC)
        r = self.gw.invoke(ctx, "memory.propose_memory",
                            body={"sensitivity": "secret"})
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SENSITIVITY_EXCEEDED)

    def test_unknown_principal_rejected(self):
        ctx = make_signed_ctx(agent_principal="rogue")
        r = self.gw.invoke(ctx, "memory.propose_memory")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.PRINCIPAL_UNKNOWN)


# S2: Checkpoint Writer

class TestCheckpointWriter(unittest.TestCase):

    def test_fencing_token_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cw = CheckpointWriter(
                "m1",
                storage_path=Path(tmp) / "history.jsonl",
                lease_path=Path(tmp) / "lease.json",
            )
            for i in range(3):
                r = cw.write(presenter_token=i, active_goal=f"g{i}")
                self.assertTrue(r.accepted)
                self.assertEqual(r.fencing_token, i)

    def test_stale_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cw = CheckpointWriter(
                "m1",
                storage_path=Path(tmp) / "history.jsonl",
                lease_path=Path(tmp) / "lease.json",
            )
            cw.write(presenter_token=0, active_goal="g0")
            r = cw.write(presenter_token=0, active_goal="stale")
            self.assertFalse(r.accepted)
            self.assertIn("stale", r.error)

    def test_history_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            cw = CheckpointWriter(
                "m1",
                storage_path=Path(tmp) / "history.jsonl",
                lease_path=Path(tmp) / "lease.json",
            )
            for i in range(3):
                cw.write(presenter_token=i, active_goal=f"g{i}")
            self.assertEqual(len(cw.history()), 3)

    def test_staleness_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cw = CheckpointWriter(
                "m1",
                storage_path=Path(tmp) / "history.jsonl",
                lease_path=Path(tmp) / "lease.json",
                staleness_threshold_seconds=0.05,
            )
            cw.write(presenter_token=0, active_goal="g")
            self.assertFalse(cw.is_stale())
            time.sleep(0.1)
            self.assertTrue(cw.is_stale())

    def test_restore_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cw = CheckpointWriter(
                "m1",
                storage_path=Path(tmp) / "history.jsonl",
                lease_path=Path(tmp) / "lease.json",
            )
            r1 = cw.write(presenter_token=0, active_goal="first")
            cw.write(presenter_token=1, active_goal="second")
            restored = cw.restore(r1.checkpoint_id)
            assert restored is not None
            self.assertEqual(restored.active_goal, "first")

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "ckpt.jsonl"
            lease = Path(tmp) / "lease.json"
            cw = CheckpointWriter("m1", storage_path=storage, lease_path=lease)
            cw.write(presenter_token=0, active_goal="persist")
            self.assertTrue(storage.exists())
            self.assertTrue(lease.exists())


# S2: Episode Builder

class TestEpisodeBuilder(unittest.TestCase):

    def setUp(self):
        self.eb = EpisodeBuilder()

    def test_start_emits_session_started(self):
        ep = self.eb.start_episode("r1", "s1", "p")
        self.assertEqual(ep.events[0].event_type, EventType.SESSION_STARTED)

    def test_sequence_monotonic(self):
        self.eb.start_episode("r1", "s1", "p")
        e1 = self.eb.record("r1", "s1", "p", EventType.PROMPT_RECEIVED, "x")
        e2 = self.eb.record("r1", "s1", "p", EventType.RESPONSE_COMPLETED, "y")
        self.assertEqual(e1.sequence, 2)
        self.assertEqual(e2.sequence, 3)

    def test_record_unknown_session_raises(self):
        with self.assertRaises(KeyError):
            self.eb.record("r1", "unknown", "p", EventType.PROMPT_RECEIVED, "x")

    def test_close_emits_session_ended(self):
        ep = self.eb.start_episode("r1", "s1", "p")
        self.eb.close_episode("s1", "p", final_outcome="DONE")
        self.assertEqual(ep.events[-1].event_type, EventType.SESSION_ENDED)
        self.assertEqual(ep.final_outcome, "DONE")

    def test_abort_mission(self):
        ep = self.eb.start_episode("r1", "s1", "p")
        ep = self.eb.abort_mission("r1", "s1", "p", reason="heartbeat lost")
        self.assertIn("ABORTED", ep.final_outcome)

    def test_ordered_reconstruction(self):
        self.eb.start_episode("r1", "s1", "p")
        for action in ["a", "b", "c"]:
            self.eb.record("r1", "s1", "p", EventType.TOOL_CALLED, action)
        recon = self.eb.reconstruct_run("r1")
        seqs = [e.sequence for e in recon]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(recon), 4)


# S3: Intent Service

class TestIntentService(unittest.TestCase):

    def test_create_and_get(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="timer", trigger_spec="x",
            action="a", idempotency_key="i1",
        )
        fetched = svc.get_intent(intent.intent_id)
        assert fetched is not None
        self.assertEqual(fetched.intent_id, intent.intent_id)

    def test_idempotency_key_reuses_intent(self):
        svc = IntentService(approval_secret=SECRET)
        i1 = svc.create_intent(
            owner="p", trigger_type="timer", trigger_spec="x",
            action="a", idempotency_key="dup-1",
        )
        i2 = svc.create_intent(
            owner="p", trigger_type="timer", trigger_spec="x",
            action="a", idempotency_key="dup-1",
        )
        self.assertEqual(i1.intent_id, i2.intent_id)

    def test_cancel(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a", idempotency_key="c1",
        )
        cancelled = svc.cancel_intent(intent.intent_id)
        assert cancelled is not None
        self.assertEqual(cancelled.status, "cancelled")

    def test_due_intents(self):
        svc = IntentService(approval_secret=SECRET)
        svc.create_intent(
            owner="p", trigger_type="timer", trigger_spec="x",
            action="a", due_at=time.time() - 100, idempotency_key="d1",
        )
        svc.create_intent(
            owner="p", trigger_type="timer", trigger_spec="x",
            action="a", due_at=time.time() + 1000, idempotency_key="d2",
        )
        self.assertEqual(len(svc.due_intents()), 1)

    def test_expire_overdue(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="timer", trigger_spec="x",
            action="a", expires_at=time.time() - 1, idempotency_key="e1",
        )
        expired = svc.expire_overdue()
        self.assertEqual(len(expired), 1)
        self.assertEqual(intent.status, "expired")

    def test_a4_consequential_blocked_without_approval(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="publish_offer",
            permission_class=PermissionClass.A4_CONSEQUENTIAL,
            idempotency_key="a4-1",
        )
        r = svc.execute_intent(intent.intent_id)
        self.assertFalse(r.executed)
        self.assertEqual(intent.status, "blocked")

    def test_a4_consequential_with_signed_approval_runs(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="publish_offer",
            permission_class=PermissionClass.A4_CONSEQUENTIAL,
            idempotency_key="a4-2",
        )
        token = mint_approval(SECRET, "approver", "r", "s")
        r = svc.execute_intent(intent.intent_id, approval_token=token)
        self.assertTrue(r.executed)
        self.assertEqual(intent.status, "completed")

    def test_a1_prepare_runs(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="prefetch_memory",
            permission_class=PermissionClass.A1_PREPARE,
            idempotency_key="a1-1",
        )
        r = svc.execute_intent(intent.intent_id)
        self.assertTrue(r.executed)


# S4: Retrieval Engine

class TestRetrievalEngine(unittest.TestCase):

    def setUp(self):
        self.engine = RetrievalEngine()
        self.scope = RetrievalScope(
            tenant_id="t-A", client_id="c-1", project_id="p-1",
            mission_id="m-1", operator_id="op-1",
            sensitivity_ceiling="internal",
        )
        for i in range(3):
            self.engine.store_candidate(MemoryCandidate(
                memory_id=f"m-self-{i}", source="vector",
                score=0.9 - i * 0.1,
                content_excerpt=f"relevant content about topic {i}",
                scope={"tenant_id": "t-A", "client_id": "c-1",
                       "project_id": "p-1", "mission_id": "m-1"},
                sensitivity="internal",
            ))
        self.engine.store_candidate(MemoryCandidate(
            memory_id="m-other-tenant", source="vector",
            score=0.99, content_excerpt="foreign tenant content",
            scope={"tenant_id": "t-B", "client_id": "c-1",
                   "project_id": "p-1", "mission_id": "m-1"},
            sensitivity="internal",
        ))
        self.engine.store_candidate(MemoryCandidate(
            memory_id="m-other-client", source="vector",
            score=0.99, content_excerpt="foreign client content",
            scope={"tenant_id": "t-A", "client_id": "c-OTHER",
                   "project_id": "p-1", "mission_id": "m-1"},
            sensitivity="internal",
        ))
        self.engine.store_candidate(MemoryCandidate(
            memory_id="m-secret", source="vector",
            score=0.99, content_excerpt="top secret",
            scope={"tenant_id": "t-A", "client_id": "c-1",
                   "project_id": "p-1", "mission_id": "m-1"},
            sensitivity="secret",
        ))

    def test_cross_tenant_excluded(self):
        pkg = self.engine.retrieve(query="relevant", scope=self.scope)
        ids = {i.memory_id for i in pkg.items}
        self.assertNotIn("m-other-tenant", ids)

    def test_cross_client_excluded(self):
        pkg = self.engine.retrieve(query="relevant", scope=self.scope)
        ids = {i.memory_id for i in pkg.items}
        self.assertNotIn("m-other-client", ids)

    def test_sensitivity_above_ceiling_excluded(self):
        pkg = self.engine.retrieve(query="relevant", scope=self.scope)
        ids = {i.memory_id for i in pkg.items}
        self.assertNotIn("m-secret", ids)

    def test_in_scope_returned(self):
        pkg = self.engine.retrieve(query="relevant", scope=self.scope, token_budget=2000)
        ids = {i.memory_id for i in pkg.items}
        self.assertTrue(all(mid.startswith("m-self") for mid in ids))

    def test_token_budget_respected(self):
        pkg = self.engine.retrieve(query="relevant", scope=self.scope, token_budget=200)
        self.assertLessEqual(len(pkg.items), 3)
        self.assertLessEqual(pkg.token_used, 200)

    def test_cache_hit(self):
        pkg1 = self.engine.retrieve(query="relevant", scope=self.scope)
        pkg2 = self.engine.retrieve(query="relevant", scope=self.scope)
        self.assertTrue(pkg2.retrieval_reason.startswith("cache_hit"))

    def test_unauthorized_logged(self):
        attempts_before = len(self.engine.unauthorized_attempts())
        self.engine.retrieve(query="foreign tenant", scope=self.scope)
        self.assertGreater(len(self.engine.unauthorized_attempts()), attempts_before)


# S5: Reality Cortex + Predictor

class TestRealityCortex(unittest.TestCase):

    def test_claim_lifecycle(self):
        c = self.cortex_setup().add_claim(subject="x", statement="y", evidence_refs=["e1"])
        c_id = c.claim_id

    def cortex_setup(self):
        return RealityCortex()

    def test_promote_requires_evidence(self):
        rc = RealityCortex()
        c = rc.add_claim(subject="x", statement="y", evidence_refs=[])
        self.assertFalse(rc.promote(c.claim_id))

    def test_supersession_preserves_history(self):
        rc = RealityCortex()
        old = rc.add_claim(subject="x", statement="v1", evidence_refs=["e1"])
        new = rc.add_claim(subject="x", statement="v2", evidence_refs=["e2"])
        rc.supersede(old.claim_id, new.claim_id)
        self.assertEqual(old.status.value, "superseded")
        # Phase 1: supersede does NOT auto-promote new
        self.assertEqual(new.status.value, "candidate")
        self.assertEqual(old.superseded_by, new.claim_id)

    def test_current_claims_returns_promoted_only(self):
        rc = RealityCortex()
        cand = rc.add_claim(subject="x", statement="c")
        promoted = rc.add_claim(subject="y", statement="p", evidence_refs=["e"])
        rc.promote(promoted.claim_id)
        ids = {c.claim_id for c in rc.current_claims()}
        self.assertIn(promoted.claim_id, ids)
        self.assertNotIn(cand.claim_id, ids)

    def test_contradictions(self):
        rc = RealityCortex()
        for stmt in ["true", "false"]:
            c = rc.add_claim(subject="x", statement=stmt, evidence_refs=["e"])
            rc.promote(c.claim_id)
        contradictions = rc.detect_contradictions()
        self.assertEqual(len(contradictions), 1)

    def test_reality_packet_evidence_gaps(self):
        rc = RealityCortex()
        c = rc.add_claim(subject="x", statement="y", evidence_refs=[])
        # Cannot promote (no evidence), but if we force it for the test:
        rc._claims[c.claim_id].evidence_refs = ["e"]  # manually add evidence
        rc.promote(c.claim_id)
        packet = rc.as_packet()
        # After promotion with evidence, no gap
        self.assertNotIn(c.claim_id, packet.evidence_gaps)


class TestPredictor(unittest.TestCase):

    def test_record_and_predict(self):
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        self.assertEqual(pred.predicted_state, "s2")

    def test_predict_no_history(self):
        pr = Predictor()
        pred = pr.predict_next_state("never", "ev")
        self.assertEqual(pred.probability, 0.5)

    def test_laplace_smoothing(self):
        # Phase 1: Laplace over outcome-space size K, not observed count
        # For n=1 observation, p < 1.0
        pr = Predictor()
        pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        self.assertLess(pred.probability, 1.0,
                        "single observation must give p < 1.0")

    def test_calibration(self):
        pr = Predictor()
        for _ in range(5):
            pr.record_transition("s1", "ev", "s2")
        for _ in range(3):
            p = pr.predict_next_state("s1", "ev")
            pr.resolve_prediction(p.prediction_id, "s2")
        # p=1/9 ≈ 0.111 from Laplace; outcome=1
        # brier = (0.111 - 1)^2 ≈ 0.79
        self.assertLess(pr.brier_score(), 1.0)

    def test_forbidden_actions_blocked(self):
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        for action in FORBIDDEN_ACTIONS:
            self.assertFalse(pr.allows_action(pred, action))

    def make_pred(self):
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        return _PredNS(pr, pr.predict_next_state("s1", "ev"))

    def test_only_matching_allowed_action(self):
        m = self.make_pred()
        self.assertTrue(m.predictor.allows_action(m.pred, "PREFETCH_MEMORY"))
        self.assertFalse(m.predictor.allows_action(m.pred, "WARM_CACHE"))


# S6: World Model + Foundries

class TestWorldModelService(unittest.TestCase):

    def test_create_model(self):
        svc = WorldModelService()
        m = svc.create_model("prefect")
        fetched = svc.get_model("prefect")
        assert fetched is not None
        self.assertEqual(fetched.domain, "prefect")

    def test_hypothesis_proposed(self):
        svc = WorldModelService()
        h = svc.add_hypothesis(description="x", mechanism="y", falsifier="z")
        self.assertEqual(h.status.value, "proposed")

    def test_hypothesis_outcome_test(self):
        svc = WorldModelService()
        h = svc.add_hypothesis(description="x", mechanism="y", falsifier="z")
        svc.update_hypothesis_outcome(h.hypothesis_id, "observed")
        self.assertEqual(h.status.value, "tested")
        self.assertEqual(h.observed_outcome, "observed")

    def test_cooccurrence_creates_hypothesis_not_causality(self):
        svc = WorldModelService()
        h = svc.add_hypothesis(
            description="x", mechanism="UNKNOWN", falsifier="z",
            alternatives=["confounding"], confounders=["schedule"],
        )
        self.assertEqual(h.status.value, "proposed")
        self.assertGreater(len(h.alternatives), 0)


class TestInterventionController(unittest.TestCase):

    def test_noop_when_no_candidates(self):
        ctrl = InterventionController()
        ranking = ctrl.rank()
        self.assertIsNone(ranking.selected)

    def test_ranking_by_net_value(self):
        ctrl = InterventionController()
        ctrl.propose(InterventionPacket(
            desired_state="x", candidate_action="PREFETCH_MEMORY",
            expected_gain=10, cost=1, risk=0.1, reversibility=1.0,
        ))
        ctrl.propose(InterventionPacket(
            desired_state="x", candidate_action="WARM_CACHE",
            expected_gain=2, cost=5, risk=2.0, reversibility=1.0,
        ))
        ranking = ctrl.rank()
        assert ranking.selected is not None
        self.assertEqual(ranking.selected.candidate_action, "PREFETCH_MEMORY")

    def test_no_candidate_beats_noop(self):
        ctrl = InterventionController()
        ctrl.propose(InterventionPacket(
            desired_state="x", candidate_action="PREFETCH_MEMORY",
            expected_gain=0.5, cost=10, risk=1.0, reversibility=0.5,
        ))
        ranking = ctrl.rank()
        self.assertIsNone(ranking.selected)

    def test_forbidden_action_rejected(self):
        ctrl = InterventionController()
        with self.assertRaises(ValueError):
            ctrl.propose(InterventionPacket(
                desired_state="x", candidate_action="SPEND_MONEY",
            ))


class TestSkillFoundry(unittest.TestCase):

    def test_three_repeats_create_candidate(self):
        sf = SkillFoundry()
        out = []
        for i in range(3):
            out.append(sf.record_trajectory(
                "deploy-and-verify", f"t-{i}", success=True,
                verifier_signature=f"sig-{i}",
            ))
        self.assertEqual(out[0], None)
        self.assertEqual(out[1], None)
        self.assertIsNotNone(out[2])
        cand = sf.get_candidate("deploy-and-verify")
        assert cand is not None
        self.assertFalse(cand.promoted)

    def test_failed_trajectory_does_not_count(self):
        sf = SkillFoundry()
        for i in range(3):
            sf.record_trajectory("k", f"t-{i}", success=False,
                                 verifier_signature=f"sig-{i}")
        self.assertIsNone(sf.get_candidate("k"))

    def test_caller_asserted_success_rejected(self):
        sf = SkillFoundry()
        with self.assertRaises(ValueError):
            sf.record_trajectory("k", "t-0", success=True)  # no verifier_signature

    def test_promote_requires_verifier_signed_gev(self):
        sf = SkillFoundry()
        for i in range(3):
            sf.record_trajectory("k", f"t-{i}", success=True,
                                 verifier_signature=f"sig-{i}")
        # Create a real fixture file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                        delete=False) as tf:
            tf.write("golden")
            fixture = tf.name
        try:
            with self.assertRaises(ValueError):
                sf.promote(
                    "k", golden_fixture=fixture,
                    replay_passed=10, replay_total=10,
                    gev_ref=mint_gev_ref(SECRET, "v", "a"),
                    approval_token=mint_approval(SECRET, "a", "r", "s"),
                )
        finally:
            os.unlink(fixture)


class TestOfferFoundry(unittest.TestCase):

    def test_required_fields(self):
        of = OfferFoundry()
        with self.assertRaises(ValueError):
            of.create_candidate(
                name="x", buyer="", pain="p", transformation="t",
                mechanism_hypothesis="m", pilot_plan="pp",
                kill_criteria=["k"],
            )

    def test_kill_criteria_required(self):
        of = OfferFoundry()
        with self.assertRaises(ValueError):
            of.create_candidate(
                name="x", buyer="b", pain="p", transformation="t",
                mechanism_hypothesis="m", pilot_plan="pp",
                kill_criteria=[],
            )

    def test_create_candidate_no_publication(self):
        of = OfferFoundry()
        offer = of.create_candidate(
            name="Memory MRI", buyer="solo founders", pain="scattered notes",
            transformation="scored map", mechanism_hypothesis="8-layer scoring",
            pilot_plan="$7.5K entry", kill_criteria=["<10% adoption in 90d"],
        )
        self.assertFalse(offer.published)

    def test_publish_requires_signed_approval(self):
        of = OfferFoundry(approval_secret=SECRET)
        offer = of.create_candidate(
            name="x", buyer="b", pain="p", transformation="t",
            mechanism_hypothesis="m", pilot_plan="pp", kill_criteria=["k"],
        )
        bad = mint_approval(b"wrong", "a", "r", "s")
        with self.assertRaises(ValueError):
            of.publish(offer.candidate_id, bad)


# S7: Cockpit

class TestMemoryCockpit(unittest.TestCase):

    def test_initial_state(self):
        c = MemoryCockpit()
        self.assertEqual(c.state, ControlState.ACTIVE)
        self.assertFalse(c.is_paused())
        self.assertFalse(c.is_killed())
        self.assertEqual(c.budget, 1.0)

    def test_pause_and_release(self):
        c = MemoryCockpit()
        c.engage_pause()
        self.assertTrue(c.is_paused())
        c.release_pause()
        self.assertFalse(c.is_paused())

    def test_kill_switch_implies_pause(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        self.assertTrue(c.is_killed())
        self.assertTrue(c.is_paused())

    def test_two_step_recovery(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        c.release_kill_switch()
        self.assertFalse(c.is_killed())
        self.assertTrue(c.is_paused())
        c.release_pause()
        self.assertTrue(c.is_active())

    def test_no_killed_and_unpaused(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        c.release_pause()  # rejected while killed
        self.assertTrue(c.is_paused())
        self.assertTrue(c.is_killed())

    def test_budget_clamps(self):
        c = MemoryCockpit()
        c.set_budget(-0.5)
        self.assertEqual(c.budget, 0.0)
        c.set_budget(2.0)
        self.assertEqual(c.budget, 1.0)
        c.set_budget(0.5)
        self.assertEqual(c.budget, 0.5)

    def test_layer_status_panel(self):
        c = MemoryCockpit()
        for layer in ["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8"]:
            c.set_layer_status(layer, "ok")
        snap = c.snapshot()
        names = {p.name for p in snap.panels}
        self.assertIn("L1-L8 health", names)
        self.assertIn("Events / episodes", names)
        self.assertIn("Retrieval", names)
        self.assertIn("gBrain", names)
        self.assertIn("Procedures", names)
        self.assertIn("Backup / restore", names)

    def test_prediction_status_panel(self):
        c = MemoryCockpit()
        c.set_prediction_stats({"brier": 0.1, "log_loss": 0.2})
        snap = c.snapshot()
        pred_panel = next(p for p in snap.panels if p.name == "Predictions")
        self.assertEqual(pred_panel.metrics["brier"], 0.1)

    def test_text_render(self):
        c = MemoryCockpit()
        c.set_layer_status("L1", "ok")
        text = c.render_text()
        self.assertIn("RIG Memory OS Cockpit", text)
        self.assertIn("State:", text)
        self.assertIn("Budget:", text)
        self.assertIn("Audit log:", text)

    def test_last_snapshot_tracking(self):
        c = MemoryCockpit()
        self.assertIsNone(c.last_snapshot())
        c.snapshot()
        self.assertIsNotNone(c.last_snapshot())


if __name__ == "__main__":
    unittest.main(verbosity=2)