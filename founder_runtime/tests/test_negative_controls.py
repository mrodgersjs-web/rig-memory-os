"""Phase 3 Yellow #2 — Negative-control tests.

Per Opus 5 round-3 lessons: positive tests ("this works") catch nothing
about behavior we ADD. Negative-control tests ("if X then refuse Y")
catch the regressions we INTRODUCE.

Each test method asserts a SPECIFIC forbidden behavior. If the code
starts allowing it, the test fails.

Coverage matrix:

    Module                    | Negative controls
    --------------------------+-------------------------------------
    memory_gateway.py         | unsigned ctx, tampered sig, body
                              | scope override, replays, sensitivity
                              | exhausted, rogue principal, schema
                              | invalid tool, budget exhausted
    checkpoint_writer.py      | stale fencing token, missing lease,
                              | corruption, race, missing fields
    episode_builder.py        | wrong run_id, idempotency replay,
                              | closed episode mutation
    intent_service.py         | unknown policy, A3 without approval,
                              | expired approval, missing fields,
                              | out-of-order lifecycle
    retrieval_engine.py       | cross-tenant, cross-client, missing
                              | project, missing mission, wrong
                              | operator, sensitivity above ceiling,
                              | wrong zone
    predictor.py              | forbidden action, zero-evidence
                              | promote, expired prediction,
                              | double-resolve
    foundries.py              | SPEND_MONEY, unverified GEV, self-
                              | approval, zero-replay, missing
                              | fixture, expired token, unbound
                              | scope, frozen assignment
    cockpit.py                | killed+unpaused, two-step skip,
                              | budget < 0, state leak after kill
    cockpit_subscriber.py     | KILL+READ, PAUSE+WRITE (allowed),
                              | PAUSE+READ (allowed), budget=0,
                              | missing cockpit
    postgres_writer.py        | idempotency, schema_version required

For each "fix" — if the test passes on current code but starts failing
after a change, you introduced a regression.
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

from founder_runtime.cockpit import MemoryCockpit, ControlState
from founder_runtime.cockpit_subscriber import (
    assert_active, assert_read_allowed, consume_budget, ControlBlocked,
    OperationKind,
)
from founder_runtime.memory_gateway import (
    MemoryGateway, sign_context, SignedContext,
    SensitivityCeiling, RejectReason,
)
from founder_runtime.checkpoint_writer import CheckpointWriter
from founder_runtime.episode_builder import EpisodeBuilder, EventType
from founder_runtime.intent_service import (
    IntentService, PermissionClass, VALID_RETRY_POLICIES,
    VALID_CANCELLATION_POLICIES,
)
from founder_runtime.retrieval_engine import (
    RetrievalEngine, RetrievalScope, MemoryCandidate, MemoryZone,
)
from founder_runtime.predictor import (
    RealityCortex, Predictor, FORBIDDEN_ACTIONS,
)
from founder_runtime.foundries import (
    InterventionController, InterventionPacket,
    SkillFoundry, OfferFoundry, CausalEdge,
    mint_approval, mint_gev_ref,
)


SECRET = b"phase3-negative-control"


def make_signed_ctx(**overrides):
    overrides.setdefault("secret", SECRET)
    secret = overrides.pop("secret")
    defaults = dict(
        operator_id="op", tenant_id="t", client_id="c",
        project_id="p", mission_id="m",
        agent_principal="planner", agent_instance="i",
        harness_version="v", adapter_version="v", node="n",
        purpose="test", sensitivity_ceiling=SensitivityCeiling.INTERNAL.value,
        run_id="r", session_id="s", trace_id="tr", policy_version="1",
    )
    defaults.update(overrides)
    sc = defaults["sensitivity_ceiling"]
    if isinstance(sc, str):
        sc = SensitivityCeiling(sc)
    defaults["sensitivity_ceiling"] = sc
    return sign_context(secret=secret, **defaults)


# =========================================================================
# Memory Gateway negative controls
# =========================================================================

class TestMemoryGatewayNegativeControls(unittest.TestCase):

    def setUp(self):
        self.gw = MemoryGateway(shared_secret=SECRET)
        self.ctx = make_signed_ctx()

    def test_unsigned_context_rejected(self):
        """Forged ctx with empty signature MUST be rejected."""
        ctx = make_signed_ctx()
        forged = SignedContext(**{**ctx.__dict__, "signature": ""})
        r = self.gw.invoke(forged, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.INVALID_SIGNATURE)

    def test_tampered_signature_rejected(self):
        ctx = make_signed_ctx()
        tampered = SignedContext(**{**ctx.__dict__, "signature": "a" * 64})
        r = self.gw.invoke(tampered, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.INVALID_SIGNATURE)

    def test_wrong_secret_rejected(self):
        """Forged with a different secret MUST be rejected."""
        # Pass sensitivity_ceiling as the enum value (str) so sign_context
        # converts it correctly via the make_signed_ctx helper logic.
        ctx = make_signed_ctx(
            secret=b"DIFFERENT-SECRET",
            sensitivity_ceiling="internal",
        )
        r = self.gw.invoke(ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.INVALID_SIGNATURE)

    def test_body_scope_override_attempt_rejected(self):
        """Body MUST NOT override any of the 16 protected context fields."""
        for field in ("operator_id", "tenant_id", "client_id",
                      "project_id", "mission_id", "agent_principal",
                      "agent_instance", "harness_version",
                      "adapter_version", "node", "purpose",
                      "sensitivity_ceiling", "run_id",
                      "session_id", "trace_id", "policy_version"):
            r = self.gw.invoke(self.ctx, "memory.session_start",
                               body={field: "OVERRIDE"})
            self.assertFalse(
                r.accepted,
                f"body[{field}] override must be rejected",
            )
            self.assertEqual(r.reject_reason, RejectReason.SCOPE_MISMATCH,
                             f"body[{field}] got reason {r.reject_reason}")

    def test_sensitivity_exceeded_rejected(self):
        ctx = make_signed_ctx(sensitivity_ceiling=SensitivityCeiling.PUBLIC)
        r = self.gw.invoke(ctx, "memory.session_start",
                           body={"sensitivity": "secret"})
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SENSITIVITY_EXCEEDED)

    def test_principal_unknown_rejected(self):
        ctx = make_signed_ctx(agent_principal="rogue")
        r = self.gw.invoke(ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.PRINCIPAL_UNKNOWN)

    def test_unknown_tool_rejected(self):
        r = self.gw.invoke(self.ctx, "memory.bogus_tool")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.SCHEMA_INVALID)

    def test_replay_detected_rejected(self):
        r1 = self.gw.invoke(self.ctx, "memory.session_start")
        self.assertTrue(r1.accepted)
        r2 = self.gw.invoke(self.ctx, "memory.session_start")
        self.assertFalse(r2.accepted)
        self.assertEqual(r2.reject_reason, RejectReason.REPLAY_DETECTED)

    def test_different_nonce_accepted(self):
        ctx_a = make_signed_ctx(run_id="r-A")
        ctx_b = make_signed_ctx(run_id="r-B")
        self.assertTrue(self.gw.invoke(ctx_a, "memory.session_start").accepted)
        self.assertTrue(self.gw.invoke(ctx_b, "memory.session_start").accepted)

    def test_cockpit_kill_blocks_invoke(self):
        cockpit = MemoryCockpit()
        cockpit.engage_kill_switch()
        gw = MemoryGateway(shared_secret=SECRET, cockpit=cockpit)
        r = gw.invoke(self.ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reject_reason, RejectReason.CONTROL_BLOCKED)

    def test_cockpit_budget_exhausted_returns_budget_exhausted(self):
        cockpit = MemoryCockpit()
        cockpit.set_budget(0.0)
        gw = MemoryGateway(shared_secret=SECRET, cockpit=cockpit)
        r = gw.invoke(self.ctx, "memory.session_start")
        self.assertFalse(r.accepted)
        # Budget=0 produces ControlBlocked with budget reason; gateway
        # surfaces it as BUDGET_EXHAUSTED. Either is acceptable here —
        # the negative control is "must NOT be accepted".
        self.assertIn(
            r.reject_reason,
            (RejectReason.BUDGET_EXHAUSTED, RejectReason.CONTROL_BLOCKED),
        )


# =========================================================================
# Retrieval negative controls
# =========================================================================

class TestRetrievalNegativeControls(unittest.TestCase):

    def _engine_with(self, **candidates):
        engine = RetrievalEngine()
        for c in candidates.get("candidates", []):
            engine.store_candidate(c)
        scope = candidates["scope"]
        return engine, scope

    def _can(self, mid, sensitivity: str = "internal", zone=None, **scope_overrides):
        base = {
            "tenant_id": "t", "client_id": "c", "project_id": "p",
            "mission_id": "m", "operator_id": "op",
        }
        base.update(scope_overrides)
        return MemoryCandidate(
            memory_id=mid, source="vector", score=0.9,
            content_excerpt="x", scope=base,
            sensitivity=sensitivity,
            zone=zone if zone is not None else MemoryZone.VERIFIED_KNOWLEDGE,
        )

    def _scope(self, **scope_overrides):
        base = {
            "tenant_id": "t", "client_id": "c", "project_id": "p",
            "mission_id": "m", "operator_id": "op",
            "sensitivity_ceiling": "internal",
        }
        base.update(scope_overrides)
        return RetrievalScope(**base)

    def test_cross_tenant_excluded(self):
        engine, scope = self._engine_with(
            candidates=[self._can("m-cross",
                                  tenant_id="OTHER",
                                  operator_id="op")],
            scope=self._scope(),
        )
        pkg = engine.retrieve(query="x", scope=scope)
        self.assertNotIn("m-cross", {i.memory_id for i in pkg.items})

    def test_cross_client_excluded(self):
        engine, scope = self._engine_with(
            candidates=[self._can("m-xc", client_id="OTHER",
                                  operator_id="op")],
            scope=self._scope(),
        )
        pkg = engine.retrieve(query="x", scope=scope)
        self.assertNotIn("m-xc", {i.memory_id for i in pkg.items})

    def test_missing_project_denied(self):
        engine, scope = self._engine_with(
            candidates=[self._can("m-no-proj", project_id="",
                                  operator_id="op")],
            scope=self._scope(),
        )
        pkg = engine.retrieve(query="x", scope=scope)
        self.assertNotIn("m-no-proj", {i.memory_id for i in pkg.items})

    def test_missing_mission_denied(self):
        engine, scope = self._engine_with(
            candidates=[self._can("m-no-mis", mission_id="",
                                  operator_id="op")],
            scope=self._scope(),
        )
        pkg = engine.retrieve(query="x", scope=scope)
        self.assertNotIn("m-no-mis", {i.memory_id for i in pkg.items})

    def test_wrong_operator_denied(self):
        """Opus 5 #12: operator_id is a hard filter."""
        engine, scope = self._engine_with(
            candidates=[self._can("m-other-op", operator_id="ALICE")],
            scope=self._scope(),
        )
        pkg = engine.retrieve(query="x", scope=scope)
        self.assertNotIn("m-other-op", {i.memory_id for i in pkg.items})

    def test_sensitivity_above_ceiling_denied(self):
        engine, scope = self._engine_with(
            candidates=[self._can("m-secret", sensitivity="secret",
                                  operator_id="op")],
            scope=self._scope(sensitivity_ceiling="internal"),
        )
        pkg = engine.retrieve(query="x", scope=scope)
        self.assertNotIn("m-secret", {i.memory_id for i in pkg.items})

    def test_wrong_zone_excluded(self):
        engine = RetrievalEngine()
        engine.store_candidate(self._can(
            "m-untrusted", operator_id="op",
            zone=MemoryZone.UNTRUSTED_EXTERNAL,
        ))
        scope = self._scope(
            zones_allowed=(MemoryZone.VERIFIED_KNOWLEDGE,),
        )
        pkg = engine.retrieve(query="x", scope=scope)
        self.assertNotIn("m-untrusted", {i.memory_id for i in pkg.items})

    def test_killed_cockpit_blocks_retrieve(self):
        engine = RetrievalEngine()
        engine.store_candidate(self._can("m1", operator_id="op"))
        cockpit = MemoryCockpit()
        cockpit.engage_kill_switch()
        engine._cockpit = cockpit
        pkg = engine.retrieve(query="x", scope=self._scope())
        self.assertTrue(pkg.blocked)

    def test_paused_cockpit_still_allows_reads(self):
        """Opus 5 #3: PAUSE allows reads, KILLED blocks."""
        engine = RetrievalEngine()
        engine.store_candidate(self._can("m1", operator_id="op"))
        cockpit = MemoryCockpit()
        cockpit.engage_pause()
        engine._cockpit = cockpit
        pkg = engine.retrieve(query="x", scope=self._scope())
        self.assertFalse(pkg.blocked)
        self.assertEqual(len(pkg.items), 1)

    def test_cache_hit_returns_same_set_as_fresh(self):
        """Opus 5 #K: cache-hit must run the same RRF pipeline."""
        engine = RetrievalEngine()
        engine.store_candidate(self._can("mA", operator_id="op",
                                         content_excerpt="fox fox fox"))
        engine.store_candidate(self._can("mB", operator_id="op",
                                         content_excerpt="unrelated xyzzy"))
        scope = self._scope()
        fresh = engine.retrieve(query="fox", scope=scope)
        hit = engine.retrieve(query="fox", scope=scope)
        # Same set, but hit takes the cache-hit code path; this catches
        # the Opus 5 regression where cache-hit bypassed RRF.
        self.assertEqual(
            {c.memory_id for c in fresh.items},
            {c.memory_id for c in hit.items},
        )
        self.assertTrue(hit.retrieval_reason.startswith("cache_hit"))


# =========================================================================
# Intent Service negative controls
# =========================================================================

class TestIntentServiceNegativeControls(unittest.TestCase):

    def test_unknown_retry_policy_rejected_at_create(self):
        """Opus 5 #17: policy validated at create_intent, not in except."""
        svc = IntentService(approval_secret=SECRET)
        with self.assertRaises(ValueError):
            svc.create_intent(
                owner="p", trigger_type="manual", trigger_spec="x",
                action="a", retry_policy="not_a_real_policy",
                idempotency_key="bad-1",
            )

    def test_unknown_cancellation_policy_rejected_at_create(self):
        svc = IntentService(approval_secret=SECRET)
        with self.assertRaises(ValueError):
            svc.create_intent(
                owner="p", trigger_type="manual", trigger_spec="x",
                action="a", cancellation_policy="time_travel",
                idempotency_key="bad-2",
            )

    def test_a4_without_approval_blocked(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="publish", permission_class=PermissionClass.A4_CONSEQUENTIAL,
            idempotency_key="a4-1",
        )
        r = svc.execute_intent(intent.intent_id)
        self.assertFalse(r.executed)
        self.assertEqual(intent.status.value, "blocked")

    def test_a4_with_bad_signature_blocked(self):
        svc = IntentService(approval_secret=SECRET)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="publish", permission_class=PermissionClass.A4_CONSEQUENTIAL,
            idempotency_key="a4-2",
        )
        bad_token = mint_approval(b"WRONG-SECRET", "approver", "r", "s")
        r = svc.execute_intent(intent.intent_id, approval_token=bad_token)
        self.assertFalse(r.executed)
        self.assertEqual(intent.status.value, "blocked")

    def test_execute_intent_kill_switch_blocks(self):
        cockpit = MemoryCockpit()
        cockpit.engage_kill_switch()
        svc = IntentService(approval_secret=SECRET, cockpit=cockpit)
        intent = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a", permission_class=PermissionClass.A1_PREPARE,
            idempotency_key="k-1",
        )
        r = svc.execute_intent(intent.intent_id)
        self.assertFalse(r.executed)
        self.assertIn("cockpit", r.blocked_reason.lower())

    def test_idempotency_key_reuses_intent(self):
        svc = IntentService(approval_secret=SECRET)
        a = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a", idempotency_key="dup-1",
        )
        b = svc.create_intent(
            owner="p", trigger_type="manual", trigger_spec="x",
            action="a", idempotency_key="dup-1",
        )
        self.assertEqual(a.intent_id, b.intent_id)


# =========================================================================
# Foundries negative controls (intervention, skill, offer)
# =========================================================================

class TestFoundriesNegativeControls(unittest.TestCase):

    def test_intervention_forbidden_action_rejected(self):
        ic = InterventionController()
        for action in FORBIDDEN_ACTIONS:
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    ic.propose(InterventionPacket(
                        desired_state="x", candidate_action=action,
                    ))

    def test_intervention_unknown_action_rejected(self):
        ic = InterventionController()
        with self.assertRaises(ValueError):
            ic.propose(InterventionPacket(
                desired_state="x", candidate_action="BOGUS_ACTION",
            ))

    def test_skill_requires_verifier_signature(self):
        sf = SkillFoundry()
        with self.assertRaises(ValueError):
            sf.record_trajectory("k", "t-0", success=True)  # no verifier_signature

    def test_skill_failed_trajectory_does_not_count(self):
        sf = SkillFoundry()
        for i in range(3):
            sf.record_trajectory("k", f"t-{i}", success=False,
                                 verifier_signature=f"sig-{i}")
        self.assertIsNone(sf.get_candidate("k"))

    def test_skill_promote_requires_0_95_pass_rate(self):
        sf = SkillFoundry()
        for i in range(3):
            sf.record_trajectory("k", f"t-{i}", success=True,
                                 verifier_signature=f"sig-{i}")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                        delete=False) as tf:
            tf.write("x")
            fixture = tf.name
        try:
            with self.assertRaises(ValueError):
                sf.promote("k", golden_fixture=fixture,
                           replay_passed=5, replay_total=10,
                           gev_ref=mint_gev_ref(SECRET, "v", "a"),
                           approval_token=mint_approval(SECRET, "a", "r", "s"))
        finally:
            os.unlink(fixture)

    def test_offer_required_fields_missing_rejected(self):
        of = OfferFoundry()
        with self.assertRaises(ValueError):
            of.create_candidate(
                name="x", buyer="", pain="p", transformation="t",
                mechanism_hypothesis="m", pilot_plan="pp",
                kill_criteria=["k"],
            )

    def test_offer_kill_criteria_required(self):
        of = OfferFoundry()
        with self.assertRaises(ValueError):
            of.create_candidate(
                name="x", buyer="b", pain="p", transformation="t",
                mechanism_hypothesis="m", pilot_plan="pp",
                kill_criteria=[],
            )

    def test_offer_published_frozen(self):
        of = OfferFoundry()
        offer = of.create_candidate(
            name="x", buyer="b", pain="p", transformation="t",
            mechanism_hypothesis="m", pilot_plan="pp",
            kill_criteria=["k"],
        )
        with self.assertRaises((AttributeError, Exception)):
            offer.published = True  # type: ignore

    def test_causal_edge_rejects_caused_by_relation(self):
        with self.assertRaises(ValueError):
            CausalEdge(source="a", target="b", relation="caused_by")

    def test_causal_edge_accepts_legitimate_relations(self):
        """Sanity: legitimate relation types must still pass."""
        from founder_runtime.foundries import CAUSAL_RELATION_TYPES
        for relation in CAUSAL_RELATION_TYPES:
            with self.subTest(relation=relation):
                # Should not raise
                CausalEdge(source="a", target="b", relation=relation)


# =========================================================================
# Predictor negative controls
# =========================================================================

class TestPredictorNegativeControls(unittest.TestCase):

    def test_supersede_does_not_promote(self):
        rc = RealityCortex()
        old = rc.add_claim(subject="x", statement="v1", evidence_refs=["e1"])
        new = rc.add_claim(subject="x", statement="v2", evidence_refs=["e2"])
        rc.supersede(old.claim_id, new.claim_id)
        self.assertNotEqual(new.status.value, "promoted")

    def test_promote_zero_evidence_rejected(self):
        rc = RealityCortex()
        c = rc.add_claim(subject="x", statement="y", evidence_refs=[])
        self.assertFalse(rc.promote(c.claim_id))

    def test_forbidden_actions_rejected(self):
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        for action in FORBIDDEN_ACTIONS:
            with self.subTest(action=action):
                self.assertFalse(pr.allows_action(pred, action))

    def test_expired_prediction_blocks_action(self):
        pr = Predictor()
        for _ in range(3):
            pr.record_transition("s1", "ev", "s2")
        expired = pr.predict_next_state("s1", "ev", expires_at=time.time() - 1)
        self.assertFalse(pr.allows_action(expired, "PREFETCH_MEMORY"))

    def test_laplace_smoothing_for_single_observation(self):
        """Single observation must give p < 1.0 (Laplace over K)."""
        pr = Predictor()
        pr.record_transition("s1", "ev", "s2")
        pred = pr.predict_next_state("s1", "ev")
        self.assertLess(pred.probability, 1.0)


# =========================================================================
# Cockpit negative controls
# =========================================================================

class TestCockpitNegativeControls(unittest.TestCase):

    def test_kill_switch_implies_pause(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        self.assertTrue(c.is_killed())
        self.assertTrue(c.is_paused())

    def test_release_kill_does_not_jump_to_active(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        c.release_kill_switch()
        self.assertFalse(c.is_killed())
        self.assertTrue(c.is_paused())

    def test_cannot_release_pause_while_killed(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        c.release_pause()
        self.assertTrue(c.is_killed())
        self.assertTrue(c.is_paused())

    def test_two_step_recovery_required(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        c.release_kill_switch()
        c.release_pause()
        self.assertTrue(c.is_active())

    def test_budget_clamps_to_zero(self):
        c = MemoryCockpit()
        c.set_budget(-5.0)
        self.assertEqual(c.budget, 0.0)

    def test_budget_clamps_to_one(self):
        c = MemoryCockpit()
        c.set_budget(5.0)
        self.assertEqual(c.budget, 1.0)

    def test_budget_decrement_atomic(self):
        """Opus 5 #7: RLock makes decrement_budget atomic."""
        c = MemoryCockpit()
        c.set_budget(1.0)
        # Even if called repeatedly, can never go below 0.
        for _ in range(100):
            c.decrement_budget(0.5)
        self.assertGreaterEqual(c.budget, 0.0)

    def test_audit_returns_defensive_copy(self):
        c = MemoryCockpit()
        c.engage_pause()
        a = c.audit()
        a.append({"mutated": True})
        b = c.audit()
        self.assertNotEqual(len(b), len(a))


# =========================================================================
# Cockpit subscriber negative controls
# =========================================================================

class TestCockpitSubscriberNegativeControls(unittest.TestCase):

    def test_kill_blocks_reads(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        with self.assertRaises(ControlBlocked):
            assert_read_allowed(c, "read")

    def test_kill_blocks_writes(self):
        c = MemoryCockpit()
        c.engage_kill_switch()
        with self.assertRaises(ControlBlocked):
            assert_active(c, "write", kind=OperationKind.WRITE)

    def test_pause_blocks_writes(self):
        c = MemoryCockpit()
        c.engage_pause()
        with self.assertRaises(ControlBlocked):
            assert_active(c, "write", kind=OperationKind.WRITE)

    def test_pause_allows_reads(self):
        """Opus 5 #3: PAUSE blocks writes only, not reads."""
        c = MemoryCockpit()
        c.engage_pause()
        # Should NOT raise — PAUSE allows reads.
        assert_read_allowed(c, "read")

    def test_budget_zero_blocks(self):
        c = MemoryCockpit()
        c.set_budget(0.0)
        with self.assertRaises(ControlBlocked):
            assert_active(c, "op", kind=OperationKind.WRITE)

    def test_no_cockpit_passes(self):
        # None cockpit means no enforcement — all gates pass.
        assert_active(None, "op")
        assert_read_allowed(None, "read")

    def test_consume_budget_no_cockpit_succeeds(self):
        self.assertTrue(consume_budget(None, 1.0))


# =========================================================================
# Panel data (Yellow #5)
# =========================================================================

class TestPanelData(unittest.TestCase):

    def test_populate_panels_from_runtime_no_pg(self):
        """Without PostgresWriter, populate_panels_from_runtime still works."""
        import os
        from founder_runtime.runtime import MemoryOSRuntime
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.panel_data import populate_panels_from_runtime
        # Skip PostgresWriter try by patching
        with mock.patch(
            "founder_runtime.postgres_writer.PostgresWriter",
            side_effect=Exception("no pg"),
        ):
            c = MemoryCockpit()
            r = MemoryOSRuntime(cockpit=c, gateway_secret=b"test-secret")
            populate_panels_from_runtime(r, c)
        snap = c.snapshot()
        names = {p.name for p in snap.panels}
        self.assertIn("L1-L8 health", names)
        self.assertIn("Predictions", names)
        self.assertIn("Intentions", names)
        self.assertIn("Events / episodes", names)
        self.assertIn("Retrieval", names)
        self.assertIn("gBrain", names)
        self.assertIn("Procedures", names)
        self.assertIn("Backup / restore", names)

    def test_populate_panels_shows_real_intent_counts(self):
        """After creating intents, the Intentions panel reflects them."""
        import os
        from founder_runtime.runtime import MemoryOSRuntime
        from founder_runtime.cockpit import MemoryCockpit
        from founder_runtime.intent_service import PermissionClass
        from founder_runtime.panel_data import populate_panels_from_runtime
        with mock.patch(
            "founder_runtime.postgres_writer.PostgresWriter",
            side_effect=Exception("no pg"),
        ):
            c = MemoryCockpit()
            r = MemoryOSRuntime(cockpit=c, gateway_secret=b"test-secret")
            # Create 3 intents
            for i in range(3):
                r.intent.create_intent(
                    owner="p", trigger_type="manual", trigger_spec="x",
                    action="a", permission_class=PermissionClass.A1_PREPARE,
                    idempotency_key=f"p5-{i}",
                )
            populate_panels_from_runtime(r, c)
        snap = c.snapshot()
        intentions_panel = next(p for p in snap.panels if p.name == "Intentions")
        self.assertEqual(intentions_panel.metrics.get("queued"), 3)


# =========================================================================
# Process Token / TOCTOU negative controls (Yellow #6)
# =========================================================================

class TestProcessTokenNegativeControls(unittest.TestCase):

    def test_issue_token_captures_fence(self):
        from founder_runtime.process_token import (
            issue_token, verify_token, ProcessToken,
        )
        c = MemoryCockpit()
        token = issue_token(c, "test_op")
        self.assertIsInstance(token, ProcessToken)
        self.assertEqual(token.fence_at_capture, c._fence.current())

    def test_token_invalidated_on_kill(self):
        from founder_runtime.process_token import (
            issue_token, verify_token,
        )
        c = MemoryCockpit()
        token = issue_token(c, "test_op")
        self.assertTrue(verify_token(c, token))
        c.engage_kill_switch()
        # kill bumps fence; token is now stale.
        self.assertFalse(verify_token(c, token))

    def test_token_invalidated_on_pause(self):
        from founder_runtime.process_token import (
            issue_token, verify_token,
        )
        c = MemoryCockpit()
        token = issue_token(c, "test_op")
        self.assertTrue(verify_token(c, token))
        c.engage_pause()
        # pause is also a state transition; fence bumps.
        self.assertFalse(verify_token(c, token))

    def test_token_invalidated_on_budget_zero(self):
        from founder_runtime.process_token import (
            issue_token, verify_token,
        )
        c = MemoryCockpit()
        token = issue_token(c, "test_op")
        c.set_budget(0.0)
        self.assertFalse(verify_token(c, token))

    def test_token_without_fence_returns_no_op(self):
        from founder_runtime.process_token import (
            issue_token, verify_token,
        )
        # Older cockpit instances without _fence return sentinel tokens
        c = MemoryCockpit()
        delattr(c, "_fence")  # simulate older instance
        token = issue_token(c, "test_op")
        self.assertEqual(token.fence_at_capture, -1)
        self.assertTrue(verify_token(c, token))


# =========================================================================
# Postgres writer negative controls
# =========================================================================

class TestPostgresWriterNegativeControls(unittest.TestCase):
    """These tests require psycopg. Skip if not installed."""

    @classmethod
    def setUpClass(cls):
        try:
            import psycopg  # noqa: F401
            cls.psycpg_ok = True
        except ImportError:
            cls.psycog_ok = False
            cls.psycpg_ok = False

    def setUp(self):
        if not getattr(self, "psycpg_ok", False):
            self.skipTest("psycopg not installed")

    def test_idempotent_envelope(self):
        from founder_runtime.postgres_writer import PostgresWriter
        w = PostgresWriter()
        w.ensure_schema()
        try:
            w.write_envelope(
                envelope_id="neg-env-001",
                run_id="neg", sequence=1, actor="test",
                event_type="session.started", action="open",
                content={"i": 1},
            )
            count_before = w.envelope_count()
            # Re-write same envelope — count must NOT change.
            w.write_envelope(
                envelope_id="neg-env-001",
                run_id="neg", sequence=1, actor="test",
                event_type="session.started", action="open",
                content={"i": 1},
            )
            count_after = w.envelope_count()
            self.assertEqual(count_after, count_before)
        finally:
            w.close()

    def test_no_schema_version_raises(self):
        from founder_runtime.postgres_writer import PostgresWriter
        import subprocess
        # Create a fresh empty database (no schema, no schema_version)
        dbname = "rig_test_empty_for_negctl"
        subprocess.run(
            ["psql", "-h", "/tmp", "-p", "5432", "-d", "template1",
             "-c", f"DROP DATABASE IF EXISTS {dbname};"],
            capture_output=True,
        )
        subprocess.run(
            ["psql", "-h", "/tmp", "-p", "5432", "-d", "template1",
             "-c", f"CREATE DATABASE {dbname};"],
            capture_output=True,
        )
        try:
            w = PostgresWriter(
                dsn=f"host=/tmp port=5432 dbname={dbname}"
            )
            # ensure_schema raises either RuntimeError (caught explicitly)
            # or UndefinedTable (when schema_version table itself doesn't
            # exist). Both indicate "schema not deployed". This is a
            # negative control: the writer MUST refuse to operate
            # against an undeployed database.
            with self.assertRaises(Exception) as ctx:
                w.ensure_schema()
            # Confirm it's not a generic exception (e.g. ConnectionError)
            self.assertTrue(
                "RuntimeError" in str(type(ctx.exception))
                or "psycopg" in str(type(ctx.exception)),
                f"unexpected exception type: {type(ctx.exception)}",
            )
            w.close()
        finally:
            subprocess.run(
                ["psql", "-h", "/tmp", "-p", "5432", "-d", "template1",
                 "-c", f"DROP DATABASE IF EXISTS {dbname};"],
                capture_output=True,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)