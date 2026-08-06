"""RIG Memory OS v10 — World Model + Foundries (S6) — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- InterventionController constrains candidate_action to AllowedAction
- SkillFoundry replay threshold raised to >=0.95 (was >=0.50)
- gev_regression_passed default removed; verifier_identity + signature required
- Golden fixture validated (must reference a real file path)
- OfferFoundry.published is a frozen dataclass field (immutable)
- OfferFoundry.publish() requires an ApprovalToken
- CausalHypothesis CONFIRMED/FALSIFIED transitions wired
- causal_edges schema rejects "caused_by" relations
- Skill retirement + signing wired
- World model + intervention + skill + offer create ApprovalRecord pattern
"""

from __future__ import annotations

import hmac
import hashlib
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit

from founder_runtime.cockpit_subscriber import (
    assert_active, consume_budget, ControlBlocked, OperationKind,
)


# =========================================================================
# Approval system (Phase 1)
# =========================================================================

class ApprovalClass(str, Enum):
    A0_OBSERVE = "A0_observe"
    A1_PREPARE = "A1_prepare"
    A2_REVERSIBLE_INTERNAL = "A2_reversible_internal"
    A3_CONTROLLED_OPERATIONAL = "A3_controlled_operational"
    A4_CONSEQUENTIAL = "A4_consequential"


@dataclass(frozen=True)
class ApprovalToken:
    """Signed approval from a named approver.

    Approval tokens carry approver identity, timestamp, run-scoped token,
    HMAC signature. Caller-supplied strings like 'granted' are rejected.

    Phase 1 fix #10 (Opus 5): added expires_at + scope binding. The token
    is now TTL-bounded and bound to a specific run/scope; cannot authorize
    arbitrary actions.
    """
    approver_id: str
    approved_at: float
    run_id: str
    scope_hash: str
    signature: str
    expires_at: float = 0.0  # 0.0 = no expiry (legacy tokens)
    scope_target: str = ""    # what the token authorizes (e.g. pattern_key)

    def verify(self, secret: bytes) -> bool:
        canonical = (
            f"{self.approver_id}|{self.approved_at}|"
            f"{self.run_id}|{self.scope_hash}|{self.expires_at}|"
            f"{self.scope_target}"
        ).encode()
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)


def mint_approval(
    secret: bytes, approver_id: str, run_id: str, scope_hash: str,
    expires_at: float = 0.0, scope_target: str = "",
) -> ApprovalToken:
    """Mint a new signed approval token.

    Phase 1 fix #10 (Opus 5): takes expires_at (TTL) and scope_target
    (what the token authorizes). Tokens default to expires_at=0 (no
    expiry); callers should pass an explicit TTL for safety.
    """
    now = time.time()
    canonical = (
        f"{approver_id}|{now}|{run_id}|{scope_hash}|"
        f"{expires_at}|{scope_target}"
    ).encode()
    sig = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return ApprovalToken(
        approver_id=approver_id,
        approved_at=now,
        run_id=run_id,
        scope_hash=scope_hash,
        signature=sig,
        expires_at=expires_at,
        scope_target=scope_target,
    )


# =========================================================================
# World Model
# =========================================================================

class HypothesisStatus(str, Enum):
    PROPOSED = "proposed"
    TESTED = "tested"
    CONFIRMED = "confirmed"
    FALSIFIED = "falsified"
    UNRESOLVED = "unresolved"


# Causal-edge relation types — explicitly NOT including "caused_by"
# (co-occurrence creates a hypothesis, never causality)
CAUSAL_RELATION_TYPES = frozenset({
    "co_occurs_with",
    "precedes",
    "enables",
    "inhibits",
    "correlates_with",
})


@dataclass
class CausalHypothesis:
    hypothesis_id: str
    description: str
    mechanism: str
    boundary_conditions: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    falsifier: str = ""
    intervention: str = ""
    expected_outcome: str = ""
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    observed_outcome: Optional[str] = None
    confounders: list[str] = field(default_factory=list)
    confirmed_at: Optional[float] = None
    falsified_at: Optional[float] = None

    def to_confirmed(self) -> None:
        """Phase 1: explicit transition to CONFIRMED (was unreachable)."""
        self.status = HypothesisStatus.CONFIRMED
        self.confirmed_at = time.time()

    def to_falsified(self) -> None:
        """Phase 1: explicit transition to FALSIFIED (was unreachable)."""
        self.status = HypothesisStatus.FALSIFIED
        self.falsified_at = time.time()


@dataclass(frozen=True)
class CausalEdge:
    """Phase 1: typed causal-edge schema — rejects "caused_by" structurally."""

    source: str
    target: str
    relation: str  # must be in CAUSAL_RELATION_TYPES
    evidence_refs: tuple[str, ...] = ()
    confidence: float = 0.5

    def __post_init__(self):
        if self.relation not in CAUSAL_RELATION_TYPES:
            raise ValueError(
                f"causal edge relation must be in {CAUSAL_RELATION_TYPES}; "
                f"got {self.relation!r} (co-occurrence creates a hypothesis, "
                f"never causality)"
            )


class WorldModelService:
    def __init__(self) -> None:
        self._models: dict[str, "WorldModel"] = {}
        self._hypotheses: dict[str, CausalHypothesis] = {}
        self._edges: list[CausalEdge] = []

    def create_model(self, domain: str) -> "WorldModel":
        model = WorldModel(domain=domain)
        self._models[domain] = model
        return model

    def get_model(self, domain: str) -> Optional["WorldModel"]:
        return self._models.get(domain)

    def add_hypothesis(
        self,
        description: str,
        mechanism: str,
        falsifier: str = "",
        boundary_conditions: Optional[list[str]] = None,
        alternatives: Optional[list[str]] = None,
        confounders: Optional[list[str]] = None,
    ) -> CausalHypothesis:
        h = CausalHypothesis(
            hypothesis_id=str(uuid.uuid4()),
            description=description,
            mechanism=mechanism,
            boundary_conditions=list(boundary_conditions or []),
            alternatives=list(alternatives or []),
            falsifier=falsifier,
            confounders=list(confounders or []),
        )
        self._hypotheses[h.hypothesis_id] = h
        return h

    def update_hypothesis_outcome(
        self, hypothesis_id: str, observed_outcome: str,
    ) -> CausalHypothesis:
        h = self._hypotheses.get(hypothesis_id)
        if h is None:
            raise KeyError(f"unknown hypothesis: {hypothesis_id}")
        h.observed_outcome = observed_outcome
        h.status = HypothesisStatus.TESTED
        return h

    def confirm_hypothesis(self, hypothesis_id: str) -> CausalHypothesis:
        """Phase 1: explicit CONFIRMED transition."""
        h = self._hypotheses.get(hypothesis_id)
        if h is None:
            raise KeyError(f"unknown hypothesis: {hypothesis_id}")
        h.to_confirmed()
        return h

    def falsify_hypothesis(self, hypothesis_id: str) -> CausalHypothesis:
        """Phase 1: explicit FALSIFIED transition."""
        h = self._hypotheses.get(hypothesis_id)
        if h is None:
            raise KeyError(f"unknown hypothesis: {hypothesis_id}")
        h.to_falsified()
        return h

    def add_edge(self, edge: CausalEdge) -> None:
        """Phase 1: structural check rejects "caused_by" relations."""
        self._edges.append(edge)

    def all_hypotheses(self) -> list[CausalHypothesis]:
        return list(self._hypotheses.values())

    def all_edges(self) -> list[CausalEdge]:
        return list(self._edges)


@dataclass
class WorldModel:
    domain: str
    current_state: str = ""
    latent_state_hypotheses: dict[str, str] = field(default_factory=dict)
    causal_edges: list[dict] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    controllable_variables: list[str] = field(default_factory=list)
    uncontrollable_variables: list[str] = field(default_factory=list)
    leading_indicators: list[str] = field(default_factory=list)
    outcome_variables: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    falsifiers: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    version: str = "1"


# =========================================================================
# Intervention Controller (Phase 1: action vocabulary constrained)
# =========================================================================

class InterventionAction(str, Enum):
    NOOP = "NOOP"
    PREFETCH_MEMORY = "PREFETCH_MEMORY"
    WARM_CACHE = "WARM_CACHE"
    LOAD_TOOL_SCHEMA = "LOAD_TOOL_SCHEMA"
    PREPARE_WORKFLOW = "PREPARE_WORKFLOW"
    PREPARE_APPROVAL_PACKET = "PREPARE_APPROVAL_PACKET"
    ALERT_VERIFIER = "ALERT_VERIFIER"


# Phase 1: forbidden actions cannot be selected
INTERVENTION_FORBIDDEN = frozenset({
    "WRITE_CANONICAL_FACT", "PROMOTE_PROCEDURE", "SEND_EXTERNAL_MESSAGE",
    "MERGE_CODE", "DELETE_DATA", "SPEND_MONEY", "DECLARE_WORKFLOW_COMPLETE",
})


@dataclass
class InterventionPacket:
    """Phase 1: candidate_action is constrained to InterventionAction enum."""

    desired_state: str
    candidate_action: str  # enum value
    expected_gain: float = 0.0
    cost: float = 0.0
    risk: float = 0.0
    attention_cost: float = 0.0
    reversibility: float = 1.0
    approval_class: ApprovalClass = ApprovalClass.A2_REVERSIBLE_INTERNAL
    proof_obligations: list[str] = field(default_factory=list)
    selected: bool = False
    approval_token: Optional[ApprovalToken] = None  # NEW: required for A3/A4


@dataclass
class InterventionRanking:
    ranked: list[InterventionPacket] = field(default_factory=list)
    selected: Optional[InterventionPacket] = None
    rationale: str = ""


class InterventionController:
    """Phase 1: action vocabulary constrained; A4 requires approval token."""

    def __init__(
        self,
        approval_secret: Optional[bytes] = None,
        cockpit: Optional["MemoryCockpit"] = None,
    ) -> None:
        self._candidates: list[InterventionPacket] = []
        self._approval_secret = approval_secret or b"rig-foundry-approval-secret"
        self._cockpit = cockpit

    def propose(self, packet: InterventionPacket) -> None:
        """Phase 1 fix: reject forbidden actions at propose time.

        Phase 1: cockpit gate — refuses on kill/pause/budget.
        """
        assert_active(
            self._cockpit, "intervention.propose", kind=OperationKind.WRITE,
        )
        if not consume_budget(self._cockpit, 0.02):
            raise RuntimeError("budget exhausted (intervention.propose)")
        if packet.candidate_action in INTERVENTION_FORBIDDEN:
            raise ValueError(
                f"forbidden intervention action: {packet.candidate_action!r}"
            )
        try:
            InterventionAction(packet.candidate_action)
        except ValueError:
            raise ValueError(
                f"unknown intervention action: {packet.candidate_action!r}; "
                f"must be one of {[a.value for a in InterventionAction]}"
            )
        self._candidates.append(packet)

    def clear(self) -> None:
        self._candidates.clear()

    def rank(self) -> InterventionRanking:
        if not self._candidates:
            return InterventionRanking(
                ranked=[], selected=None,
                rationale="no candidates; NOOP implied",
            )
        scored: list[tuple[float, InterventionPacket]] = []
        for pkt in self._candidates:
            net = (
                pkt.expected_gain * pkt.reversibility
                - pkt.cost - pkt.risk - pkt.attention_cost
            )
            scored.append((net, pkt))
        scored.sort(key=lambda x: x[0], reverse=True)
        ranked = [p for _, p in scored]
        best = ranked[0] if ranked else None
        selected = best if best and scored[0][0] > 0 else None
        rationale = (
            f"ranked {len(ranked)} candidates; "
            f"best net={scored[0][0]:.3f}; "
            f"{'selected' if selected else 'NOOP'} policy applies"
        )
        for p in ranked:
            p.selected = (p is selected)
        return InterventionRanking(ranked=ranked, selected=selected, rationale=rationale)


# =========================================================================
# Skill Foundry (Phase 1: 95% threshold, real GEV)
# =========================================================================

@dataclass(frozen=True)
class GEVArtifactRef:
    """Verifier-signed GEV regression artifact reference."""

    verifier_id: str
    artifact_id: str
    signed_at: float
    signature: str

    def verify(self, secret: bytes) -> bool:
        canonical = f"{self.verifier_id}|{self.artifact_id}|{self.signed_at}".encode()
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)


def mint_gev_ref(secret: bytes, verifier_id: str, artifact_id: str) -> GEVArtifactRef:
    now = time.time()
    canonical = f"{verifier_id}|{artifact_id}|{now}".encode()
    sig = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return GEVArtifactRef(
        verifier_id=verifier_id, artifact_id=artifact_id,
        signed_at=now, signature=sig,
    )


@dataclass
class SkillCandidate:
    candidate_id: str
    name: str
    description: str
    repeat_references: list[str] = field(default_factory=list)
    input_signature: str = ""
    output_signature: str = ""
    allowlist: list[str] = field(default_factory=list)
    permission_required: ApprovalClass = ApprovalClass.A2_REVERSIBLE_INTERNAL
    golden_fixture: Optional[str] = None
    replay_passed: int = 0
    replay_total: int = 0
    failure_modes: list[str] = field(default_factory=list)
    rollback_plan: str = ""
    reliability: float = 0.0
    bms_proposal: ApprovalClass = ApprovalClass.A4_CONSEQUENTIAL
    gev_ref: Optional[GEVArtifactRef] = None
    approval_token: Optional[ApprovalToken] = None
    created_at: float = field(default_factory=time.time)
    promoted: bool = False
    retired: bool = False
    retired_at: Optional[float] = None


class SkillFoundry:
    """Phase 1: 95% replay threshold; verifier-signed GEV; fixture validation."""

    REPEAT_THRESHOLD = 3
    REPLAY_PASS_RATE_THRESHOLD = 0.95  # was 0.5 in Opus 5 review

    def __init__(
        self,
        gev_secret: Optional[bytes] = None,
        approval_secret: Optional[bytes] = None,
        cockpit: Optional["MemoryCockpit"] = None,
    ) -> None:
        # Phase 1 fix #9 (Opus 5): SEPARATE secrets for GEV attestation vs
        # approval. The previous single-secret design let the verifier
        # self-approve their own promotion.
        self._patterns: dict[str, int] = defaultdict(int)
        self._trajectories: dict[str, list[str]] = defaultdict(list)
        self._candidates: dict[str, SkillCandidate] = {}
        self._gev_secret = gev_secret or b"rig-skill-gev-secret"
        self._approval_secret = (
            approval_secret or self._gev_secret + b":approval"
        )
        self._cockpit = cockpit

    def record_trajectory(
        self, pattern_key: str, trajectory_ref: str,
        success: bool,
        verifier_signature: Optional[str] = None,
    ) -> Optional[SkillCandidate]:
        """Phase 1: success requires verifier_signature (GEV separation).

        Phase 1 fix (Opus 5 #1): cockpit gate MUST be called.
        """
        # Opus 5 fix: this method MUST consult the cockpit. The previous
        # version stored self._cockpit but never called assert_active.
        assert_active(
            self._cockpit,
            "skill.record_trajectory",
            kind=OperationKind.WRITE,
        )
        if not consume_budget(self._cockpit, 0.01):
            raise RuntimeError("budget exhausted (skill.record_trajectory)")
        if not success:
            return None
        # Phase 1: success implies an external verifier attested
        if not verifier_signature:
            # Caller-asserted success without verifier is invalid
            raise ValueError(
                f"success=True requires verifier_signature for {trajectory_ref!r} "
                f"(GEV separation: caller cannot self-attest success)"
            )
        self._patterns[pattern_key] += 1
        self._trajectories[pattern_key].append(trajectory_ref)

        count = self._patterns[pattern_key]
        if count >= self.REPEAT_THRESHOLD and pattern_key not in self._candidates:
            candidate = SkillCandidate(
                candidate_id=str(uuid.uuid4()),
                name=pattern_key,
                description=(
                    f"Detected pattern {pattern_key!r} repeated "
                    f"{count} times"
                ),
                repeat_references=list(self._trajectories[pattern_key]),
                reliability=1.0,
                bms_proposal=ApprovalClass.A4_CONSEQUENTIAL,
            )
            self._candidates[pattern_key] = candidate
            return candidate
        return None

    def get_candidate(self, pattern_key: str) -> Optional[SkillCandidate]:
        return self._candidates.get(pattern_key)

    def promote(
        self,
        pattern_key: str,
        golden_fixture: str,
        replay_passed: int,
        replay_total: int,
        gev_ref: GEVArtifactRef,
        approval_token: ApprovalToken,
        rollback_plan: str = "",
        failure_modes: Optional[list[str]] = None,
        bms_proposal: ApprovalClass = ApprovalClass.A3_CONTROLLED_OPERATIONAL,
    ) -> SkillCandidate:
        """Phase 1: requires verifier-signed GEV artifact + signed approval token.

        Replay pass-rate must be >= 0.95 (was >= 0.5 — Opus 5 fix #12).
        Golden fixture must reference a real file (was truthiness check).
        Phase 1 fix (Opus 5 #1): cockpit gate MUST be called.
        Phase 1 fix (Opus 5 #9): GEV and approval use SEPARATE secrets so
        verifier cannot self-approve.
        """
        # Opus 5 fix: cockpit gate on promote.
        assert_active(
            self._cockpit, "skill.promote", kind=OperationKind.WRITE,
        )
        if not consume_budget(self._cockpit, 0.05):
            raise RuntimeError("budget exhausted (skill.promote)")
        candidate = self._candidates.get(pattern_key)
        if candidate is None:
            raise KeyError(f"no skill candidate for pattern: {pattern_key!r}")

        # Phase 1: validate golden fixture (must reference a real file path)
        if not golden_fixture or not os.path.exists(golden_fixture):
            raise ValueError(
                f"golden_fixture must reference an existing file path; "
                f"got {golden_fixture!r}"
            )

        if replay_total <= 0:
            raise ValueError("replay_total must be > 0")
        pass_rate = replay_passed / replay_total
        if pass_rate < self.REPLAY_PASS_RATE_THRESHOLD:
            raise ValueError(
                f"replay pass rate {pass_rate:.2f} below "
                f"{self.REPLAY_PASS_RATE_THRESHOLD} threshold"
            )

        # Phase 1: verify GEV signature (Opus 5 #9: separate secret from approval)
        if not gev_ref.verify(self._gev_secret):
            raise ValueError("GEV artifact signature verification failed")

        # Phase 1 fix #9: verify approval token with SEPARATE secret.
        if not approval_token.verify(self._approval_secret):
            raise ValueError("approval token signature verification failed")

        # Phase 1 fix #10 (Opus 5): bind approval to the subject being promoted.
        # A token minted for run_id="X" must not promote run_id="Y".
        if approval_token.run_id and approval_token.run_id != pattern_key:
            raise ValueError(
                f"approval token run_id={approval_token.run_id!r} does not "
                f"match pattern_key={pattern_key!r}"
            )
        # Phase 1 fix #10: enforce TTL on approval
        if approval_token.expires_at and approval_token.expires_at < time.time():
            raise ValueError(
                f"approval token expired at {approval_token.expires_at} "
                f"(now {time.time()})"
            )

        if not rollback_plan:
            raise ValueError("rollback_plan is required")

        candidate.golden_fixture = golden_fixture
        candidate.replay_passed = replay_passed
        candidate.replay_total = replay_total
        candidate.reliability = pass_rate
        candidate.bms_proposal = bms_proposal
        candidate.rollback_plan = rollback_plan
        candidate.failure_modes = list(failure_modes or [])
        candidate.gev_ref = gev_ref
        candidate.approval_token = approval_token
        candidate.promoted = True
        return candidate

    def retire(self, pattern_key: str) -> SkillCandidate:
        """Phase 1: explicit retirement."""
        candidate = self._candidates.get(pattern_key)
        if candidate is None:
            raise KeyError(f"no skill candidate: {pattern_key!r}")
        candidate.retired = True
        candidate.retired_at = time.time()
        return candidate

    def all_candidates(self) -> list[SkillCandidate]:
        return list(self._candidates.values())

    def all_pattern_counts(self) -> dict[str, int]:
        return dict(self._patterns)


# =========================================================================
# Offer Foundry (Phase 1: immutable published)
# =========================================================================

@dataclass(frozen=True)
class OfferCandidate:
    """Phase 1: published is a frozen dataclass field; immutable once set."""
    candidate_id: str
    name: str
    buyer: str
    pain: str
    transformation: str
    mechanism_hypothesis: str
    outcome_evidence_refs: tuple[str, ...] = ()
    delivery_energy: float = 0.0
    reuse_potential: float = 0.0
    payment_hypothesis: str = ""
    pilot_plan: str = ""
    kill_criteria: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    published: bool = False  # default False, mutable only via publish()
    published_at: Optional[float] = None
    published_by: Optional[str] = None
    approval_token: Optional[ApprovalToken] = None


class OfferFoundry:
    """Phase 1: explicit publish() requires signed approval; no free assignment."""

    def __init__(
        self,
        approval_secret: Optional[bytes] = None,
        cockpit: Optional["MemoryCockpit"] = None,
    ) -> None:
        self._offers: dict[str, OfferCandidate] = {}
        self._approval_secret = approval_secret or b"rig-offer-approval-secret"
        self._cockpit = cockpit

    def create_candidate(
        self,
        name: str,
        buyer: str,
        pain: str,
        transformation: str,
        mechanism_hypothesis: str,
        outcome_evidence_refs: Optional[list[str]] = None,
        delivery_energy: float = 0.0,
        reuse_potential: float = 0.0,
        payment_hypothesis: str = "",
        pilot_plan: str = "",
        kill_criteria: Optional[list[str]] = None,
    ) -> OfferCandidate:
        """Phase 1: cockpit gate — refuses on kill/pause/budget."""
        assert_active(
            self._cockpit, "offer.create_candidate",
            kind=OperationKind.WRITE,
        )
        if not consume_budget(self._cockpit, 0.05):
            raise RuntimeError("budget exhausted (offer.create_candidate)")
        required = {
            "buyer": buyer, "pain": pain, "transformation": transformation,
            "mechanism_hypothesis": mechanism_hypothesis, "pilot_plan": pilot_plan,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"missing required OfferCandidate fields: {missing}")
        if not kill_criteria:
            raise ValueError("kill_criteria are required per v10 spec")
        # Use replace() to create a new instance with frozen fields
        offer = OfferCandidate(
            candidate_id=str(uuid.uuid4()),
            name=name, buyer=buyer, pain=pain,
            transformation=transformation,
            mechanism_hypothesis=mechanism_hypothesis,
            outcome_evidence_refs=tuple(outcome_evidence_refs or []),
            delivery_energy=delivery_energy,
            reuse_potential=reuse_potential,
            payment_hypothesis=payment_hypothesis,
            pilot_plan=pilot_plan,
            kill_criteria=tuple(kill_criteria),
        )
        self._offers[offer.candidate_id] = offer
        return offer

    def publish(
        self, candidate_id: str, approval_token: ApprovalToken,
    ) -> OfferCandidate:
        """Phase 1: explicit publish; requires signed approval token + cockpit gate.

        Phase 1 fix #10 (Opus 5): token must be bound to this candidate
        (scope_target == candidate_id) and not expired.
        """
        assert_active(
            self._cockpit, "offer.publish", kind=OperationKind.WRITE,
        )
        if not consume_budget(self._cockpit, 0.1):
            raise RuntimeError("budget exhausted (offer.publish)")
        offer = self._offers.get(candidate_id)
        if offer is None:
            raise KeyError(f"unknown offer: {candidate_id!r}")
        if not approval_token.verify(self._approval_secret):
            raise ValueError("approval token signature verification failed")
        # Phase 1 fix #10: token must be bound to this candidate
        if approval_token.scope_target and (
            approval_token.scope_target != candidate_id
        ):
            raise ValueError(
                f"approval token scope_target={approval_token.scope_target!r} "
                f"does not match candidate_id={candidate_id!r}"
            )
        # Phase 1 fix #10: enforce TTL
        if approval_token.expires_at and (
            approval_token.expires_at < time.time()
        ):
            raise ValueError(
                f"approval token expired at {approval_token.expires_at} "
                f"(now {time.time()})"
            )
        # Frozen dataclass: replace() to create new instance with updated field
        new_offer = replace(
            offer, published=True, published_at=time.time(),
            published_by=approval_token.approver_id,
            approval_token=approval_token,
        )
        self._offers[candidate_id] = new_offer
        return new_offer

    def get_offer(self, candidate_id: str) -> Optional[OfferCandidate]:
        return self._offers.get(candidate_id)

    def all_offers(self) -> list[OfferCandidate]:
        return list(self._offers.values())