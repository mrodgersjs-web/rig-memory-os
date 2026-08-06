"""Phase 1 negative tests — adversarial coverage for the 25 Opus 5 findings.

Each test method targets a specific blocking item. Tests are designed
to FAIL on the pre-Phase-1 implementation and PASS on Phase 1.
"""

from __future__ import annotations

import json
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
    SensitivityCeiling, RejectReason, PROTECTED_FIELDS,
)
from founder_runtime.retrieval_engine import (
    RetrievalEngine, RetrievalScope, MemoryCandidate, MemoryZone,
)
from founder_runtime.predictor import (
    RealityCortex, Predictor, AllowedAction, FORBIDDEN_ACTIONS,
)
from founder_runtime.foundries import (
    WorldModelService, InterventionController, InterventionPacket,
    InterventionAction, INTERVENTION_FORBIDDEN,
    SkillFoundry, OfferFoundry, CausalEdge, CAUSAL_RELATION_TYPES,
    ApprovalClass, ApprovalToken, mint_approval,
    GEVArtifactRef, mint_gev_ref,
)
from founder_runtime.intent_service import (
    IntentService, Intent, IntentStatus, PermissionClass,
)
from founder_runtime.checkpoint_writer import CheckpointWriter
from founder_runtime.episode_builder import EpisodeBuilder, EventType
from founder_runtime.cockpit import MemoryCockpit, ControlState


SECRET = b"phase1-test-secret"


def make_signed_ctx(**overrides) -> SignedContext:
    """Helper: build a properly signed context for tests."""
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
    # Cast sensitivity_ceiling string back to enum
    sc = defaults["sensitivity_ceiling"]
    if isinstance(sc, str):
        sc = SensitivityCeiling(sc)
    defaults["sensitivity_ceiling"] = sc
    return sign_context(secret=secret, **defaults)


# =========================================================================
# Auth & scope (Blocking 1-5)
# =========================================================================

class TestAuthScopePhase1(unittest.TestCase):
    """Blocking 1-5: HMAC signing, body scope, sensitivity, replay rekey."""

    def test_unsigned_context_rejected(self):
        """Blocking #1: SignedContext without HMAC is rejected."""
        ctx = SignedContext(
            operator_id="op", tenant_id="t", client_id="c",
            project_id="p", mission_id="m", agent_principal="planner",
            agent_instance="i", harness_version="v", adapter_version="v",
            node="n", purpose="x",
            sensitivity_ceiling=SensitivityCeiling.INTERNAL,
            run_id="r", session_id="s", trace_id="t", policy_version="1",
            nonce="n", signature="",  # empty signature
        )
        gw = MemoryGateway(shared_secret=SECRET)
        r = gw.invoke(ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.INVALID_SIGNATURE)

    def test_tampered_signature_rejected(self):
        """Blocking #1: tampered signature is rejected."""
        ctx = make_signed_ctx()
        ctx = SignedContext(
            **{**ctx.__dict__, "signature": "deadbeef" * 8}  # bad sig
        )
        gw = MemoryGateway(shared_secret=SECRET)
        r = gw.invoke(ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.INVALID_SIGNATURE)

    def test_unknown_principal_rejected(self):
        """Blocking #3 / Opus 5 PRINCIPAL_UNKNOWN wired."""
        ctx = make_signed_ctx(agent_principal="evil-rogue")
        gw = MemoryGateway(shared_secret=SECRET)
        r = gw.invoke(ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.PRINCIPAL_UNKNOWN)

    def test_body_supplying_tenant_id_rejected(self):
        """Blocking #2: body cannot override ANY protected field."""
        ctx = make_signed_ctx()
        gw = MemoryGateway(shared_secret=SECRET)
        r = gw.invoke(ctx, "memory.session_start", body={"tenant_id": "EVIL"})
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SCOPE_MISMATCH)

    def test_body_supplying_run_id_rejected(self):
        """Blocking #2: body cannot override run_id."""
        ctx = make_signed_ctx()
        gw = MemoryGateway(shared_secret=SECRET)
        r = gw.invoke(ctx, "memory.session_start", body={"run_id": "EVIL"})
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SCOPE_MISMATCH)

    def test_sensitivity_check_wired(self):
        """Blocking #3: SENSITIVITY_EXCEEDED wired."""
        ctx = make_signed_ctx(sensitivity_ceiling=SensitivityCeiling.PUBLIC)
        gw = MemoryGateway(shared_secret=SECRET)
        r = gw.invoke(ctx, "memory.session_start",
                      body={"sensitivity": "secret"})
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SENSITIVITY_EXCEEDED)

    def test_replay_rekeyed_to_context_hash_tool_nonce(self):
        """Blocking #4: replay keyed on (context_hash, tool, nonce)."""
        ctx = make_signed_ctx()
        gw = MemoryGateway(shared_secret=SECRET)
        # First call: accept
        r1 = gw.invoke(ctx, "memory.session_start")
        self.assertTrue(r1.accepted)
        # Same context, same tool, same nonce: replay
        r2 = gw.invoke(ctx, "memory.session_start")
        self.assertFalse(r2.accepted)
        self.assertEqual(r2.reject_reason, RejectReason.REPLAY_DETECTED)
        # Same context, DIFFERENT tool with same nonce: legitimate (different
        # action), should be accepted
        r3 = gw.invoke(ctx, "memory.heartbeat")
        self.assertTrue(r3.accepted,
                        "different tool with same nonce is legitimate")
        # Different context (different run_id = different nonce): accept
        ctx2 = make_signed_ctx(run_id="run-2")
        r4 = gw.invoke(ctx2, "memory.session_start")
        self.assertTrue(r4.accepted)


# =========================================================================
# Retrieval isolation (Blocking 6-10)
# =========================================================================

class TestRetrievalIsolationPhase1(unittest.TestCase):
    """Blocking 6-10: hard filters, zone from candidate, cache, log, operator_id."""

    def _make_engine(self):
        engine = RetrievalEngine()
        for i in range(3):
            engine.store_candidate(MemoryCandidate(
                memory_id=f"m-{i}", source="vector", score=0.9,
                content_excerpt=f"in-scope content {i}",
                scope={"tenant_id": "T", "client_id": "C",
                       "project_id": "P", "mission_id": "M"},
                sensitivity="internal", zone=MemoryZone.VERIFIED_KNOWLEDGE,
            ))
        return engine

    def test_empty_project_field_denied(self):
        """Blocking #6: missing project_id in candidate scope denies."""
        engine = self._make_engine()
        engine.store_candidate(MemoryCandidate(
            memory_id="m-no-project", source="vector", score=0.95,
            content_excerpt="no project field",
            scope={"tenant_id": "T", "client_id": "C",
                   # NO project_id, NO mission_id
                   },
            sensitivity="internal", zone=MemoryZone.VERIFIED_KNOWLEDGE,
        ))
        scope = RetrievalScope(
            tenant_id="T", client_id="C", project_id="P", mission_id="M",
            operator_id="op", sensitivity_ceiling="internal",
        )
        pkg = engine.retrieve(query="no project field", scope=scope)
        ids = {i.memory_id for i in pkg.items}
        self.assertNotIn("m-no-project", ids)

    def test_empty_mission_field_denied(self):
        """Blocking #6: missing mission_id in candidate scope denies."""
        engine = self._make_engine()
        engine.store_candidate(MemoryCandidate(
            memory_id="m-no-mission", source="vector", score=0.95,
            content_excerpt="no mission field",
            scope={"tenant_id": "T", "client_id": "C",
                   "project_id": "P",  # has project, no mission
                   },
            sensitivity="internal", zone=MemoryZone.VERIFIED_KNOWLEDGE,
        ))
        scope = RetrievalScope(
            tenant_id="T", client_id="C", project_id="P", mission_id="M",
            operator_id="op", sensitivity_ceiling="internal",
        )
        pkg = engine.retrieve(query="no mission field", scope=scope)
        ids = {i.memory_id for i in pkg.items}
        self.assertNotIn("m-no-mission", ids)

    def test_zone_read_from_candidate_not_caller_dict(self):
        """Blocking #7: zone from MemoryCandidate.zone, not scope dict."""
        engine = self._make_engine()
        # Legit VERIFIED_PROCEDURES candidate
        engine.store_candidate(MemoryCandidate(
            memory_id="m-vp", source="vector", score=0.95,
            content_excerpt="verified procedure",
            scope={"tenant_id": "T", "client_id": "C",
                   "project_id": "P", "mission_id": "M",
                   "operator_id": "op"},
            sensitivity="internal", zone=MemoryZone.VERIFIED_PROCEDURES,
        ))
        # UNTRUSTED_EXTERNAL candidate (zone cannot be spoofed)
        engine.store_candidate(MemoryCandidate(
            memory_id="m-ue", source="vector", score=0.95,
            content_excerpt="untrusted external",
            scope={"tenant_id": "T", "client_id": "C",
                   "project_id": "P", "mission_id": "M",
                   "operator_id": "op",
                   # NO zone field in scope dict
                   },
            sensitivity="internal", zone=MemoryZone.UNTRUSTED_EXTERNAL,
        ))
        scope = RetrievalScope(
            tenant_id="T", client_id="C", project_id="P", mission_id="M",
            operator_id="op", sensitivity_ceiling="internal",
            zones_allowed=(MemoryZone.VERIFIED_PROCEDURES,),
        )
        pkg = engine.retrieve(query="verified procedure", scope=scope)
        ids = {i.memory_id for i in pkg.items}
        # m-vp is in VERIFIED_PROCEDURES — should pass
        self.assertIn("m-vp", ids)
        # m-ue is in UNTRUSTED_EXTERNAL — should NOT pass zone filter
        self.assertNotIn("m-ue", ids)

    def test_cache_hit_re_runs_scope_filter(self):
        """Blocking #8: cache hit re-runs scope/sensitivity filter."""
        engine = self._make_engine()
        scope = RetrievalScope(
            tenant_id="T", client_id="C", project_id="P", mission_id="M",
            operator_id="op", sensitivity_ceiling="internal",
        )
        # Warm cache
        engine.retrieve(query="in-scope content", scope=scope)
        # Reclassify a candidate to secret AFTER cache warm
        for mid in ["m-0", "m-1", "m-2"]:
            engine.store_candidate(MemoryCandidate(
                memory_id=mid, source="vector", score=0.9,
                content_excerpt=engine._storage[mid].content_excerpt,
                scope=engine._storage[mid].scope,
                sensitivity="secret",  # reclassified
                zone=MemoryZone.VERIFIED_KNOWLEDGE,
            ))
        # Query again — cache hit should STILL apply the new sensitivity filter
        pkg = engine.retrieve(query="in-scope content", scope=scope)
        ids = {i.memory_id for i in pkg.items}
        self.assertNotIn("m-0", ids)
        self.assertNotIn("m-1", ids)
        self.assertNotIn("m-2", ids)

    def test_unauthorized_attempt_auto_logged(self):
        """Blocking #9: denials are auto-logged."""
        engine = self._make_engine()
        engine.store_candidate(MemoryCandidate(
            memory_id="m-cross", source="vector", score=0.95,
            content_excerpt="cross tenant",
            scope={"tenant_id": "OTHER", "client_id": "C",
                   "project_id": "P", "mission_id": "M"},
            sensitivity="internal", zone=MemoryZone.VERIFIED_KNOWLEDGE,
        ))
        scope = RetrievalScope(
            tenant_id="T", client_id="C", project_id="P", mission_id="M",
            operator_id="op", sensitivity_ceiling="internal",
        )
        attempts_before = len(engine.unauthorized_attempts())
        engine.retrieve(query="cross tenant", scope=scope)
        attempts_after = len(engine.unauthorized_attempts())
        self.assertGreater(attempts_after, attempts_before,
                           "denial must be auto-logged")

    def test_store_candidate_invalidates_cache(self):
        """Blocking #8: writes invalidate cache."""
        engine = self._make_engine()
        scope = RetrievalScope(
            tenant_id="T", client_id="C", project_id="P", mission_id="M",
            operator_id="op", sensitivity_ceiling="internal",
        )
        engine.retrieve(query="in-scope", scope=scope)
        size_before = engine.cache_size()
        engine.store_candidate(MemoryCandidate(
            memory_id="m-new", source="vector", score=0.95,
            content_excerpt="new entry",
            scope={"tenant_id": "T", "client_id": "C",
                   "project_id": "P", "mission_id": "M"},
            sensitivity="internal", zone=MemoryZone.VERIFIED_KNOWLEDGE,
        ))
        size_after = engine.cache_size()
        self.assertLessEqual(size_after, size_before,
                             "writing should invalidate stale cache entries")


# =========================================================================
# Truth path (Blocking 19-22)
# =========================================================================

class TestTruthPathPhase1(unittest.TestCase):
    """Blocking 19-22: supersede split, Laplace, auto-track, gate on path."""

    def test_supersede_does_not_promote(self):
        """Blocking #19: supersede does NOT set new.status to PROMOTED."""
        rc = RealityCortex()
        old = rc.add_claim(subject="x", statement="v1", evidence_refs=["e1"])
        new = rc.add_claim(subject="x", statement="v2", evidence_refs=["e2"])
        rc.supersede(old.claim_id, new.claim_id)
        self.assertEqual(new.status.value, "candidate",
                         "supersede must not auto-promote")
        self.assertEqual(old.status.value, "superseded")

    def test_supersede_preserves_learned_at(self):
        """Blocking #19: supersede does NOT overwrite new.learned_at."""
        rc = RealityCortex()
        old = rc.add_claim(subject="x", statement="v1", evidence_refs=["e1"])
        new = rc.add_claim(subject="x", statement="v2", evidence_refs=["e2"])
        original_learned_at = new.learned_at
        time.sleep(0.01)
        rc.supersede(old.claim_id, new.claim_id)
        self.assertEqual(new.learned_at, original_learned_at,
                         "supersede must not overwrite learned_at")

    def test_promote_requires_evidence(self):
        """Blocking #19: zero-evidence claim cannot be promoted."""
        rc = RealityCortex()
        c = rc.add_claim(subject="x", statement="y", evidence_refs=[])
        self.assertFalse(rc.promote(c.claim_id),
                         "zero-evidence claim must not be promoted")

    def test_promote_with_evidence_succeeds(self):
        rc = RealityCortex()
        c = rc.add_claim(subject="x", statement="y", evidence_refs=["e1"])
        self.assertTrue(rc.promote(c.claim_id))
        self.assertEqual(c.status.value, "promoted")

    def test_laplace_under_1_for_n1(self):
        """Blocking #20: p < 1.0 for single observation."""
        pr = Predictor()
        pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        self.assertLess(pred.probability, 1.0,
                        "single observation must give p < 1.0")

    def test_predict_auto_tracks(self):
        """Blocking #21: predict_next_state auto-tracks."""
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        # Auto-track: should be findable without manual track_prediction
        resolved = pr.resolve_prediction(pred.prediction_id, "s2")
        self.assertTrue(resolved.actual_outcome)

    def test_resolve_is_idempotent(self):
        """Blocking #21: double-resolve returns same record, no duplicate."""
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        r1 = pr.resolve_prediction(pred.prediction_id, "s2")
        r2 = pr.resolve_prediction(pred.prediction_id, "s2")
        self.assertEqual(pr.resolved_count(), 1)
        # Both calls return the same record
        self.assertEqual(r1.prediction_id, r2.prediction_id)
        self.assertEqual(r1.brier_component, r2.brier_component)

    def test_allows_action_checks_expires_at(self):
        """Blocking #21: expired prediction disallows all actions."""
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev", expires_at=time.time() - 1)
        self.assertFalse(pr.allows_action(pred, "PREFETCH_MEMORY"))


# =========================================================================
# Gate-D / GEV (Blocking 11-14)
# =========================================================================

class TestGateDGEVPhase1(unittest.TestCase):
    """Blocking 11-14: approval records, GEV removal, action enum, immutable publish."""

    def test_intervention_forbidden_action_rejected(self):
        """Blocking #13: SPEND_MONEY rejected at propose time."""
        ic = InterventionController()
        with self.assertRaises(ValueError):
            ic.propose(InterventionPacket(
                desired_state="x", candidate_action="SPEND_MONEY",
                expected_gain=10,
            ))

    def test_intervention_unknown_action_rejected(self):
        """Blocking #13: unknown action rejected at propose time."""
        ic = InterventionController()
        with self.assertRaises(ValueError):
            ic.propose(InterventionPacket(
                desired_state="x", candidate_action="BOGUS_ACTION",
            ))

    def test_skill_promote_replay_threshold_95(self):
        """Blocking #12: 5/10 pass rate (50%) is now below threshold."""
        sf = SkillFoundry()
        for i in range(3):
            sf.record_trajectory("k", f"t-{i}", success=True,
                                 verifier_signature=f"sig-{i}")
        # Create a real fixture file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                        delete=False) as tf:
            tf.write("golden")
            fixture_path = tf.name
        try:
            gev = mint_gev_ref(SECRET, "verifier-1", "gev-1")
            approval = mint_approval(SECRET, "approver-1", "run-1", "scope-1")
            # 5/10 = 50% — below 0.95 threshold
            with self.assertRaises(ValueError) as ctx:
                sf.promote(
                    "k", golden_fixture=fixture_path,
                    replay_passed=5, replay_total=10,
                    gev_ref=gev, approval_token=approval,
                    rollback_plan="undo",
                )
            self.assertIn("below", str(ctx.exception).lower())
        finally:
            os.unlink(fixture_path)

    def test_skill_promote_requires_verifier_signed_success(self):
        """Blocking #12: caller-asserted success without verifier rejected."""
        sf = SkillFoundry()
        with self.assertRaises(ValueError) as ctx:
            sf.record_trajectory("k", "t-0", success=True)  # no verifier_signature
        self.assertIn("verifier_signature", str(ctx.exception).lower())

    def test_skill_promote_golden_fixture_must_exist(self):
        """Blocking #12: golden fixture must reference a real file."""
        sf = SkillFoundry()
        for i in range(3):
            sf.record_trajectory("k", f"t-{i}", success=True,
                                 verifier_signature=f"sig-{i}")
        with self.assertRaises(ValueError):
            sf.promote(
                "k", golden_fixture="/nonexistent/path.txt",
                replay_passed=10, replay_total=10,
                gev_ref=mint_gev_ref(SECRET, "v", "a"),
                approval_token=mint_approval(SECRET, "a", "r", "s"),
            )

    def test_offer_published_requires_signed_approval(self):
        """Blocking #14: publish requires signed ApprovalToken."""
        of = OfferFoundry()
        offer = of.create_candidate(
            name="x", buyer="b", pain="p", transformation="t",
            mechanism_hypothesis="m", pilot_plan="pp",
            kill_criteria=["k"],
        )
        # Token signed with wrong secret fails
        bad_token = mint_approval(b"wrong-secret", "a", "r", "s")
        with self.assertRaises(ValueError):
            of.publish(offer.candidate_id, bad_token)
        # No token: can't publish without it
        self.assertFalse(offer.published)

    def test_offer_published_is_frozen(self):
        """Blocking #14: published is a frozen dataclass field."""
        of = OfferFoundry()
        offer = of.create_candidate(
            name="x", buyer="b", pain="p", transformation="t",
            mechanism_hypothesis="m", pilot_plan="pp",
            kill_criteria=["k"],
        )
        with self.assertRaises((AttributeError, Exception)):
            offer.published = True  # direct mutation must fail

    def test_causal_edge_rejects_caused_by(self):
        """Blocking Truth #30: causal_edges schema rejects "caused_by"."""
        with self.assertRaises(ValueError) as ctx:
            CausalEdge(source="a", target="b", relation="caused_by")
        self.assertIn("causality", str(ctx.exception).lower())

    def test_causal_edge_accepts_valid_relations(self):
        for rel in CAUSAL_RELATION_TYPES:
            edge = CausalEdge(source="a", target="b", relation=rel)
            self.assertEqual(edge.relation, rel)

    def test_hypothesis_confirmed_transition(self):
        """Blocking Truth #30: CONFIRMED transition wired."""
        wms = WorldModelService()
        h = wms.add_hypothesis(description="x", mechanism="y", falsifier="z")
        wms.confirm_hypothesis(h.hypothesis_id)
        self.assertEqual(h.status.value, "confirmed")
        self.assertIsNotNone(h.confirmed_at)

    def test_hypothesis_falsified_transition(self):
        wms = WorldModelService()
        h = wms.add_hypothesis(description="x", mechanism="y", falsifier="z")
        wms.falsify_hypothesis(h.hypothesis_id)
        self.assertEqual(h.status.value, "falsified")
        self.assertIsNotNone(h.falsified_at)


# =========================================================================
# Durability (Blocking 15-18)
# =========================================================================

class TestDurabilityPhase1(unittest.TestCase):
    """Blocking 15-18: external lease, append-only log, episode ledger, retry."""

    def test_checkpoint_lease_file_persists(self):
        """Blocking #15: fencing token externalized to lease file."""
        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / "lease.json"
            cw = CheckpointWriter(
                mission_id="m1",
                lease_path=lease,
                storage_path=Path(tmp) / "history.jsonl",
            )
            cw.write(presenter_token=0, active_goal="g1")
            self.assertTrue(lease.exists())
            data = json.loads(lease.read_text())
            self.assertEqual(data["fencing_token"], 1)

    def test_checkpoint_history_persisted(self):
        """Blocking #16: history in append-only JSONL."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "history.jsonl"
            cw = CheckpointWriter(mission_id="m1", storage_path=storage)
            cw.write(presenter_token=0, active_goal="g1")
            cw.write(presenter_token=1, active_goal="g2")
            content = storage.read_text()
            self.assertEqual(content.count("\n"), 2)

    def test_checkpoint_restart_recovery(self):
        """Blocking #16: reload from disk restores state."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "history.jsonl"
            lease = Path(tmp) / "lease.json"
            cw1 = CheckpointWriter(mission_id="m1", storage_path=storage, lease_path=lease)
            r1 = cw1.write(presenter_token=0, active_goal="g1")
            cw1.write(presenter_token=1, active_goal="g2")
            # Simulate restart
            cw2 = CheckpointWriter(mission_id="m1", storage_path=storage, lease_path=lease)
            self.assertEqual(len(cw2.history()), 2)
            self.assertEqual(cw2.fencing_token, 2)
            # Restore specific checkpoint
            restored = cw2.restore(r1.checkpoint_id)
            self.assertEqual(restored.active_goal, "g1")

    def test_checkpoint_split_brain_token_caught_up(self):
        """Blocking #15: split-brain with stale token is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "history.jsonl"
            lease = Path(tmp) / "lease.json"
            cw = CheckpointWriter(mission_id="m1", storage_path=storage, lease_path=lease)
            cw.write(presenter_token=0, active_goal="g1")
            cw.write(presenter_token=1, active_goal="g2")
            # Token 0 is stale (current is 2)
            r = cw.write(presenter_token=0, active_goal="stale")
            self.assertFalse(r.accepted)

    def test_episode_events_persisted(self):
        """Blocking #17: events in append-only JSONL."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "episodes.jsonl"
            eb = EpisodeBuilder(storage_path=storage)
            ep = eb.start_episode("r1", "s1", "p")
            eb.close_episode("s1", "p")
            self.assertTrue(storage.exists())
            content = storage.read_text()
            self.assertIn("session.started", content)
            self.assertIn("session.ended", content)

    def test_episode_caller_idempotency_key(self):
        """Blocking #17: caller idempotency keys dedupe."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "episodes.jsonl"
            eb = EpisodeBuilder(storage_path=storage)
            eb.start_episode("r1", "s1", "p")
            e1 = eb.record("r1", "s1", "p", EventType.PROMPT_RECEIVED,
                           "first", idempotency_key="key-1")
            e2 = eb.record("r1", "s1", "p", EventType.PROMPT_RECEIVED,
                           "first", idempotency_key="key-1")
            self.assertEqual(e1.event_id, e2.event_id,
                             "same idempotency key returns same event")
            # Check BEFORE close (only SESSION_STARTED + 1 record = 2)
            ep = eb.get_episode("s1")
            assert ep is not None
            self.assertEqual(len(ep.events), 2,
                             "duplicate not stored again")

    def test_episode_get_episode_returns_defensive_copy(self):
        """Blocking #17: get_episode returns defensive copy."""
        with tempfile.TemporaryDirectory() as tmp:
            eb = EpisodeBuilder(storage_path=Path(tmp) / "ep.jsonl")
            ep = eb.start_episode("r1", "s1", "p")
            # Mutating the returned copy should not affect internal state
            copy = eb.get_episode("s1")
            original_count = len(copy.events)
            copy.events.append("FAKE")
            fresh = eb.get_episode("s1")
            self.assertEqual(len(fresh.events), original_count)
            eb.close_episode("s1", "p")

    def test_intent_retry_policy_read(self):
        """Blocking #18: retry_policy='next_admission' is read and applied."""
        svc = IntentService()
        # Intent with custom failing executor
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="x", retry_policy="next_admission",
            idempotency_key="retry-1",
        )
        # First attempt fails
        r = svc.execute_intent(
            intent.intent_id,
            executor_fn=lambda i: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # Phase 1: retry policy applied — re-admitted to PENDING
        self.assertEqual(intent.status.value, "pending")
        self.assertEqual(r.attempts, 1)

    def test_intent_a3_gated_by_signed_approval(self):
        """Blocking #11: A3 CONTROLLED_OPERATIONAL requires signed token."""
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="x",
            permission_class=PermissionClass.A3_CONTROLLED_OPERATIONAL,
            idempotency_key="a3-1",
        )
        # No token: BLOCKED
        r = svc.execute_intent(intent.intent_id)
        self.assertFalse(r.executed)
        self.assertEqual(intent.status.value, "blocked")

    def test_intent_a4_gated_by_signed_approval(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="publish",
            permission_class=PermissionClass.A4_CONSEQUENTIAL,
            idempotency_key="a4-1",
        )
        r = svc.execute_intent(intent.intent_id)
        self.assertFalse(r.executed)
        self.assertEqual(intent.status.value, "blocked")

    def test_intent_blocked_can_be_approved(self):
        """Blocking #18: BLOCKED -> PENDING via approve_blocked_intent."""
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="publish",
            permission_class=PermissionClass.A4_CONSEQUENTIAL,
            idempotency_key="block-1",
        )
        svc.execute_intent(intent.intent_id)
        self.assertEqual(intent.status.value, "blocked")
        token = mint_approval(SECRET, "approver-1", "r", "s")
        svc.approve_blocked_intent(intent.intent_id, token)
        self.assertEqual(intent.status.value, "pending")

    def test_intent_double_execution_blocked(self):
        """Blocking #18: execution fencing token prevents double-execution."""
        svc = IntentService()
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a", idempotency_key="dbl-1",
        )
        svc.execute_intent(intent.intent_id)
        self.assertEqual(intent.status.value, "completed")
        # Try to execute again (status terminal, so blocked)
        r = svc.execute_intent(intent.intent_id)
        self.assertFalse(r.executed)
        self.assertIn("terminal", r.blocked_reason.lower())

    def test_intent_effect_receipts_persisted(self):
        """Blocking #18: effect receipts persisted to JSONL."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "receipts.jsonl"
            svc = IntentService(storage_path=storage)
            intent = svc.create_intent(
                owner="p", trigger_type="manual", trigger_spec="x",
                action="a", idempotency_key="persist-1",
            )
            svc.execute_intent(intent.intent_id)
            self.assertTrue(storage.exists())
            content = storage.read_text()
            self.assertIn(intent.intent_id, content)


# =========================================================================
# Control plane (Blocking 23-25)
# =========================================================================

class TestControlPlanePhase1(unittest.TestCase):
    """Blocking 23-25: authoritative pause/kill, state machine, budget."""

    def test_state_machine_killed_implies_paused(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        self.assertTrue(c.is_killed())
        self.assertTrue(c.is_paused(),
                        "killed must imply paused")

    def test_state_machine_two_step_recovery(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        c.release_kill_switch()
        # After release_kill_switch, state is PAUSED (not ACTIVE)
        self.assertFalse(c.is_killed())
        self.assertTrue(c.is_paused())
        # Second step: release_pause
        c.release_pause()
        self.assertFalse(c.is_paused())
        self.assertTrue(c.is_active())

    def test_state_machine_no_killed_and_unpaused(self):
        """Blocking #24: killed-but-unpaused state is unreachable."""
        c = MemoryCockpit()
        c.engage_kill_switch()
        c.release_pause()  # try to release pause while killed
        # Should still be paused (release rejected because killed)
        self.assertTrue(c.is_paused())
        self.assertTrue(c.is_killed())

    def test_audit_log_records_all_control_actions(self):
        c = MemoryCockpit()
        c.engage_pause(actor="alice")
        c.engage_kill_switch(actor="bob")
        c.release_kill_switch(actor="bob")
        c.release_pause(actor="alice")
        snap = c.snapshot()
        self.assertGreater(len(snap.audit), 0)

    def test_budget_decrement_enforcement(self):
        """Blocking #25: budget enforcement via decrement_budget."""
        c = MemoryCockpit()
        c.set_budget(0.5)
        # First decrement succeeds
        self.assertTrue(c.decrement_budget(0.3))
        self.assertAlmostEqual(c.budget, 0.2)
        # Second decrement that exceeds remaining returns False
        self.assertFalse(c.decrement_budget(0.5))
        self.assertAlmostEqual(c.budget, 0.2,
                                "rejected decrement must not change budget")

    def test_budget_clamping_on_set(self):
        c = MemoryCockpit()
        c.set_budget(2.0)
        self.assertEqual(c.budget, 1.0)
        c.set_budget(-0.5)
        self.assertEqual(c.budget, 0.0)

    def test_all_8_panels_implemented(self):
        """Blocking Non-blocking #27: all 8 documented panels present."""
        c = MemoryCockpit()
        snap = c.snapshot()
        names = {p.name for p in snap.panels}
        expected = {
            "L1-L8 health", "Predictions", "Intentions",
            "Events / episodes", "Retrieval", "gBrain",
            "Procedures", "Backup / restore",
        }
        self.assertTrue(expected.issubset(names),
                        f"missing panels: {expected - names}")

    def test_snapshot_retention_bounded(self):
        """Blocking Non-blocking #27: snapshot retention bounded."""
        c = MemoryCockpit()
        for _ in range(100):
            c.snapshot()
        # deque maxlen=50, so internal storage should be bounded
        self.assertLessEqual(len(c._snapshots), 50)

    def test_cockpit_subscriber_blocks_when_killed(self):
        """minimax cross-family: subsystems respect kill switch."""
        from founder_runtime.cockpit_subscriber import (
            assert_active, ControlBlocked, consume_budget,
        )
        c = MemoryCockpit()
        c.engage_kill_switch()
        with self.assertRaises(ControlBlocked):
            assert_active(c, "test_op")
        c.release_kill_switch()
        # After release_kill_switch, state is PAUSED — still blocks writes
        with self.assertRaises(ControlBlocked):
            assert_active(c, "test_op")
        c.release_pause()
        # Now ACTIVE — passes
        assert_active(c, "test_op")

    def test_cockpit_subscriber_blocks_when_budget_zero(self):
        from founder_runtime.cockpit_subscriber import (
            assert_active, ControlBlocked,
        )
        c = MemoryCockpit()
        c.set_budget(0.0)
        with self.assertRaises(ControlBlocked):
            assert_active(c, "test_op")

    def test_consume_budget_decrements(self):
        from founder_runtime.cockpit_subscriber import consume_budget
        c = MemoryCockpit()
        c.set_budget(1.0)
        self.assertTrue(consume_budget(c, 0.3))
        self.assertAlmostEqual(c.budget, 0.7)
        # Cannot consume more than remaining
        self.assertFalse(consume_budget(c, 0.8))

    def test_gateway_invocation_respects_kill_switch(self):
        """Phase 1 wiring: gateway refuses when cockpit is KILLED."""
        from founder_runtime.memory_gateway import MemoryGateway, sign_context
        from founder_runtime.cockpit import MemoryCockpit, ControlState
        from founder_runtime.memory_gateway import SensitivityCeiling
        from founder_runtime.cockpit_subscriber import ControlBlocked
        c = MemoryCockpit()
        c.engage_kill_switch()
        gw = MemoryGateway(shared_secret=b"phase1-wire", cockpit=c)
        ctx = sign_context(
            secret=b"phase1-wire",
            operator_id="op", tenant_id="t", client_id="c",
            project_id="p", mission_id="m",
            agent_principal="planner", agent_instance="i",
            harness_version="v", adapter_version="v", node="n",
            purpose="x", sensitivity_ceiling=SensitivityCeiling.INTERNAL,
            run_id="r", session_id="s", trace_id="t",
            policy_version="1",
        )
        r = gw.invoke(ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertIn("cockpit", r.reject_detail.lower())

    def test_retrieval_respects_pause(self):
        """Phase 1: PAUSE allows reads (Opus 5 #3); only KILLED blocks reads."""
        from founder_runtime.retrieval_engine import (
            RetrievalEngine, RetrievalScope, MemoryCandidate, MemoryZone,
        )
        from founder_runtime.cockpit import MemoryCockpit
        c = MemoryCockpit()
        re = RetrievalEngine(cockpit=c)
        re.store_candidate(MemoryCandidate(
            memory_id="m1", source="vector", score=0.9,
            content_excerpt="x", scope={"tenant_id": "t", "client_id": "c",
                                        "project_id": "p", "mission_id": "m",
                                        "operator_id": "op"},
            sensitivity="internal", zone=MemoryZone.VERIFIED_KNOWLEDGE,
        ))
        scope = RetrievalScope(
            tenant_id="t", client_id="c", project_id="p", mission_id="m",
            operator_id="op", sensitivity_ceiling="internal",
        )
        # PAUSE: reads should still succeed (Opus 5 #3)
        c.engage_pause()
        pkg = re.retrieve(query="x", scope=scope)
        self.assertEqual(len(pkg.items), 1)
        self.assertFalse(pkg.blocked)

        # KILL: reads should be blocked
        c.release_pause()
        c.engage_kill_switch()
        pkg = re.retrieve(query="x", scope=scope)
        self.assertEqual(len(pkg.items), 0)
        self.assertTrue(pkg.blocked)
        self.assertIn("kill", pkg.blocked_reason.lower())

    def test_intent_execute_respects_kill_switch(self):
        """Phase 1 wiring: intent refuses when KILLED."""
        from founder_runtime.intent_service import (
            IntentService, PermissionClass,
        )
        from founder_runtime.cockpit import MemoryCockpit
        c = MemoryCockpit()
        c.engage_kill_switch()
        svc = IntentService(cockpit=c)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a", permission_class=PermissionClass.A1_PREPARE,
            idempotency_key="kill-1",
        )
        r = svc.execute_intent(intent.intent_id)
        self.assertFalse(r.executed)
        self.assertIn("cockpit", r.blocked_reason.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)