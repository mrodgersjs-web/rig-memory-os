"""Phase 0 — Typed contracts for the Founder Runtime.

These Pydantic models are the authoritative shape of every record that crosses
the runtime boundary. Database rows, queue payloads, lease packets, ProofPackets,
and approval requests must all validate against these schemas.

Design rules:
- Every state transition is a model field; free-form JSON only inside `payload`.
- No secret values live in these models.
- Idempotency is mandatory on WorkItem.
- ApprovalLane is closed-set: `autonomous_local` or `mike_approval`.
- Stage transitions on Opportunity follow the lifecycle in section 8.1 of the handoff.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return str(uuid4())


# ------------------------------------------------------------------ Lifecycle


class OpportunityStage(str, Enum):
    SIGNAL = "SIGNAL"
    CANDIDATE = "CANDIDATE"
    VALIDATING = "VALIDATING"
    QUALIFIED = "QUALIFIED"
    EXPERIMENT_READY = "EXPERIMENT_READY"
    EXPERIMENTING = "EXPERIMENTING"
    SELL_READY = "SELL_READY"
    BUILD_READY = "BUILD_READY"
    WON = "WON"
    LOST = "LOST"
    PARKED = "PARKED"
    KILLED = "KILLED"


class WorkItemStatus(str, Enum):
    READY = "READY"
    LEASED = "LEASED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"
    REOPENED = "REOPENED"


class WorkResultStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


class ApprovalLane(str, Enum):
    autonomous_local = "autonomous_local"
    mike_approval = "mike_approval"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    REOPEN = "REOPEN"


class NodeStatus(str, Enum):
    ONLINE = "ONLINE"
    DRAINING = "DRAINING"
    OFFLINE_UNVERIFIED = "OFFLINE_UNVERIFIED"
    OFFLINE = "OFFLINE"


# ------------------------------------------------------------------ Contracts


class FounderRuntimeContract(BaseModel):
    """Top-level runtime identity and version pin."""

    schema_version: Literal[1] = 1
    runtime_id: str = Field(default_factory=_uid)
    started_at: datetime = Field(default_factory=_utcnow)
    direction_loaded: bool = False
    one_scheduler_only: bool = True
    one_worker_per_node: bool = True


class NodeCapabilityContract(BaseModel):
    """Persistent worker on a single host. One per node, no fanout."""

    node_id: str
    hostname: str
    status: NodeStatus = NodeStatus.ONLINE
    capabilities: list[str] = Field(default_factory=list)
    model_routes: list[str] = Field(default_factory=list)
    max_concurrency: int = 2
    current_load: int = 0
    last_heartbeat: Optional[datetime] = None
    lan_address: Optional[str] = None
    tailnet_address: Optional[str] = None
    worker_version: str = "0.1.0"
    health_details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("max_concurrency")
    @classmethod
    def _cap_concurrency(cls, v: int) -> int:
        if v < 1 or v > 8:
            raise ValueError("max_concurrency must be 1..8")
        return v


class WorkItemContract(BaseModel):
    """Single bounded unit of work, leased by one worker at a time."""

    work_item_id: str = Field(default_factory=_uid)
    opportunity_id: Optional[str] = None
    work_type: str
    objective: str
    payload: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    status: WorkItemStatus = WorkItemStatus.READY
    priority: int = 50
    idempotency_key: str
    approval_lane: ApprovalLane = ApprovalLane.autonomous_local
    max_attempts: int = 2
    attempt_count: int = 0
    available_at: datetime = Field(default_factory=_utcnow)
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("priority")
    @classmethod
    def _priority_range(cls, v: int) -> int:
        if v < 0 or v > 100:
            raise ValueError("priority must be 0..100")
        return v

    @field_validator("max_attempts")
    @classmethod
    def _attempts_bounded(cls, v: int) -> int:
        if v < 1 or v > 5:
            raise ValueError("max_attempts must be 1..5")
        return v


class WorkResultContract(BaseModel):
    """What a worker produced for a leased WorkItem."""

    result_id: str = Field(default_factory=_uid)
    work_item_id: str
    worker_id: str
    status: WorkResultStatus
    summary: str
    artifact_paths: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    proofpacket_path: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: datetime = Field(default_factory=_utcnow)
    error_class: Optional[str] = None
    retryable: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class OpportunityContract(BaseModel):
    """One row in the company portfolio. Scored on ten transparent fields."""

    opportunity_id: str = Field(default_factory=_uid)
    title: str
    vertical: Optional[str] = None
    company_id: Optional[str] = None
    stage: OpportunityStage = OpportunityStage.SIGNAL
    # 10 scoring fields, all 0..10, normalized by scoring code:
    direction_fit: float = 0
    pain_evidence: float = 0
    urgency_evidence: float = 0
    buyer_access: float = 0
    proof_advantage: float = 0
    speed_to_test: float = 0
    delivery_burden: float = 0
    recurrence_potential: float = 0
    ip_reuse_potential: float = 0
    confidence: float = 0
    priority: float = 0
    owner: Optional[str] = None
    next_action: Optional[str] = None
    next_action_due_at: Optional[datetime] = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def composite(self, weights: Optional[dict[str, float]] = None) -> float:
        w = weights or {
            "direction_fit": 2.0,
            "pain_evidence": 1.5,
            "urgency_evidence": 1.0,
            "buyer_access": 1.0,
            "proof_advantage": 1.5,
            "speed_to_test": 0.5,
            "recurrence_potential": 1.0,
            "ip_reuse_potential": 0.5,
            "delivery_burden": -0.5,  # higher burden lowers score
            "confidence": 0.5,
        }
        s = 0.0
        for k, wt in w.items():
            s += wt * getattr(self, k)
        return round(s, 3)


class VerificationContract(BaseModel):
    """Independent verifier verdict. Different model family than generators."""

    verifier_node: str
    verifier_model: str
    verdict: Verdict
    evidence_hash: str
    repair_class: Optional[str] = None
    notes: str = ""
    sealed_at: datetime = Field(default_factory=_utcnow)


class ApprovalRequestContract(BaseModel):
    """A Mike-decision-lane action awaiting sign-off."""

    approval_id: str = Field(default_factory=_uid)
    action_type: str          # send_email | post | spend | pricing | commit | deploy | dns | credential | destructive | export
    target: str               # exact recipient, host, repo, billing key, etc.
    exact_content_or_diff: dict[str, Any] = Field(default_factory=dict)
    business_reason: str
    rollback_plan: Optional[str] = None
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"] = "PENDING"
    requested_at: datetime = Field(default_factory=_utcnow)
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None


class DoneContract(BaseModel):
    """Measurable exit criteria for a mission or work item."""

    acceptance: list[str] = Field(default_factory=list)
    evidence_required: list[str] = Field(default_factory=list)
    measurable_outcome: str
    forbidden_outputs: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ Packets


class SignalPacket(BaseModel):
    """Output of a signal_research work item."""

    signal_id: str = Field(default_factory=_uid)
    source_uri: str
    source_type: str
    observed_at: datetime = Field(default_factory=_utcnow)
    content_hash: str
    summary: str
    entities: dict[str, Any] = Field(default_factory=dict)
    freshness_until: Optional[datetime] = None
    evidence_strength: float = 0
    suggested_opportunity_id: Optional[str] = None


class ProofPacket(BaseModel):
    """Hash-chained proof a result was real and verifier-sealed."""

    schema_version: Literal[1] = 1
    proof_id: str = Field(default_factory=_uid)
    work_item_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    result_id: Optional[str] = None
    verifier_node: str
    verifier_model: str
    verdict: Verdict
    evidence_hash: str
    packet_path: str
    sealed_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "FounderRuntimeContract",
    "NodeCapabilityContract",
    "WorkItemContract",
    "WorkResultContract",
    "OpportunityContract",
    "VerificationContract",
    "ApprovalRequestContract",
    "DoneContract",
    "SignalPacket",
    "ProofPacket",
    "OpportunityStage",
    "WorkItemStatus",
    "WorkResultStatus",
    "ApprovalLane",
    "Verdict",
    "NodeStatus",
]