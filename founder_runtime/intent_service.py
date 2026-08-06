"""RIG Memory OS v10 — Intent Service (S3) — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- retry_policy = "next_admission" is now READ and applied
- BLOCKED intents can transition back to PENDING on approval
- A4 consequential blocked unless signed ApprovalToken provided
- A3 also gated by ApprovalToken (Opus 5 fix #11)
- Execution fencing token prevents double-execution
- Effect receipts persisted to JSONL file (durable, not in-memory only)
- Module docstring honest: in-memory stub for execution; persistence only
"""

from __future__ import annotations

import hmac
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from founder_runtime.foundries import ApprovalToken, mint_approval
from founder_runtime.cockpit_subscriber import (
    assert_active, consume_budget, ControlBlocked,
    OperationKind,
)


if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit
    from founder_runtime.postgres_writer import PostgresWriter


class IntentStatus(str, Enum):
    PENDING = "pending"
    DUE = "due"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PermissionClass(str, Enum):
    A0_OBSERVE = "A0_observe"
    A1_PREPARE = "A1_prepare"
    A2_REVERSIBLE_INTERNAL = "A2_reversible_internal"
    A3_CONTROLLED_OPERATIONAL = "A3_controlled_operational"
    A4_CONSEQUENTIAL = "A4_consequential"


# Permission classes that REQUIRE a signed ApprovalToken
GATED_PERMISSION_CLASSES = frozenset({
    PermissionClass.A3_CONTROLLED_OPERATIONAL,
    PermissionClass.A4_CONSEQUENTIAL,
})

# Phase 1 fix #17 (Opus 5): explicit policy vocabularies.
VALID_RETRY_POLICIES = frozenset({
    "next_admission",
    "exponential_backoff",
    "fixed_delay",
    "manual_only",
})
VALID_CANCELLATION_POLICIES = frozenset({
    "manual",
    "ttl_expires",
    "auto_complete",
})


@dataclass
class Intent:
    intent_id: str
    owner: str
    trigger_type: str
    trigger_spec: str
    preconditions: list[str] = field(default_factory=list)
    action: str = ""
    required_context: list[str] = field(default_factory=list)
    permission_class: PermissionClass = PermissionClass.A1_PREPARE
    retry_policy: str = "next_admission"
    cancellation_policy: str = "manual"
    idempotency_key: str = ""
    due_at: Optional[float] = None
    expires_at: Optional[float] = None
    status: IntentStatus = IntentStatus.PENDING
    proof_obligations: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    attempts: int = 0
    approval_token: Optional[ApprovalToken] = None
    execution_fencing_token: int = 0


@dataclass
class IntentExecutionResult:
    intent_id: str
    executed: bool
    result: str = ""
    effect_receipt_id: Optional[str] = None
    duration_seconds: float = 0.0
    attempts: int = 1
    blocked_reason: str = ""


class IntentService:
    """L8 Prospective memory service.

    Phase 1 honest scope: in-process executor for tests + decisions;
    effect receipts persisted to JSONL file at `storage_path` (durable
    across restart). Production deployment requires wiring real
    Temporal per the v10 spec — see residual_risks in ProofPacket.
    """

    def __init__(
        self,
        executor: Optional[Callable] = None,
        storage_path: Optional[Path] = None,
        approval_secret: Optional[bytes] = None,
        cockpit: Optional["MemoryCockpit"] = None,
        postgres_writer: Optional["PostgresWriter"] = None,
        intents_path: Optional[Path] = None,
    ) -> None:
        self._intents: dict[str, Intent] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self._effect_receipts: list[dict] = []
        self._executor = executor
        self._storage_path = storage_path
        self._approval_secret = approval_secret or b"rig-intent-approval-secret"
        self._cockpit = cockpit
        # Phase 4: optional Postgres sink for intents / effect_receipts.
        # Best-effort: sink failure never blocks the service, but is
        # recorded visibly in _persistence_failures.
        self._postgres_writer = postgres_writer
        self._persistence_failures: list[dict] = []
        # Phase 5: durable intents log (JSONL). effect_receipts has a
        # foreign key to intents, so reconciliation is impossible unless
        # intents themselves survive an outage. Append-only:
        # {"kind": "create", ...full fields} then
        # {"kind": "status", "intent_id", "status"} per transition.
        self._intents_path = intents_path

        # Load persisted effect receipts on init
        if self._storage_path is not None and self._storage_path.exists():
            self._load_persisted_receipts()
        # Load persisted intents on init (Phase 5 restart recovery)
        if self._intents_path is not None and self._intents_path.exists():
            self._load_persisted_intents()

    def _append_intent_line(self, line: dict) -> None:
        if self._intents_path is None:
            return
        self._intents_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._intents_path, "a") as f:
            f.write(json.dumps(line) + "\n")

    def _persist_intent_create(self, intent: Intent) -> None:
        self._append_intent_line({
            "kind": "create",
            "intent_id": intent.intent_id,
            "owner": intent.owner,
            "trigger_type": intent.trigger_type,
            "trigger_spec": intent.trigger_spec,
            "action": intent.action,
            "permission_class": intent.permission_class.value,
            "retry_policy": intent.retry_policy,
            "idempotency_key": intent.idempotency_key,
            "due_at": intent.due_at,
            "expires_at": intent.expires_at,
            "status": intent.status.value,
        })

    def _persist_intent_status(self, intent: Intent) -> None:
        self._append_intent_line({
            "kind": "status",
            "intent_id": intent.intent_id,
            "status": intent.status.value,
        })

    def _load_persisted_intents(self) -> None:
        try:
            with open(self._intents_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("kind") == "create":
                        intent = Intent(
                            intent_id=data["intent_id"],
                            owner=data["owner"],
                            trigger_type=data["trigger_type"],
                            trigger_spec=data["trigger_spec"],
                            action=data.get("action", ""),
                            permission_class=PermissionClass(
                                data.get("permission_class", "A1_prepare")
                            ),
                            retry_policy=data.get("retry_policy", "next_admission"),
                            idempotency_key=data.get("idempotency_key", ""),
                            due_at=data.get("due_at"),
                            expires_at=data.get("expires_at"),
                            status=IntentStatus(data.get("status", "pending")),
                        )
                        self._intents[intent.intent_id] = intent
                        if intent.idempotency_key:
                            self._by_idempotency_key[intent.idempotency_key] = intent.intent_id
                    elif data.get("kind") == "status":
                        intent = self._intents.get(data["intent_id"])
                        if intent is not None:
                            intent.status = IntentStatus(data["status"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass

    def persistence_failures(self) -> list[dict]:
        """Phase 4: Postgres sink failures, in order. Empty when healthy."""
        return list(self._persistence_failures)

    def _sink(self, sink: str, fn) -> None:
        """Best-effort Postgres write-through with visible failure."""
        if self._postgres_writer is None:
            return
        try:
            fn(self._postgres_writer)
        except Exception as exc:
            self._persistence_failures.append({
                "sink": sink,
                "error": f"{type(exc).__name__}: {exc}",
                "timestamp": time.time(),
            })

    def _load_persisted_receipts(self) -> None:
        if self._storage_path is None:
            return
        try:
            with open(self._storage_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._effect_receipts.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            pass

    def _persist_receipt(self, receipt: dict) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._storage_path, "a") as f:
            f.write(json.dumps(receipt) + "\n")

    def create_intent(
        self,
        owner: str,
        trigger_type: str,
        trigger_spec: str,
        action: str,
        due_at: Optional[float] = None,
        expires_at: Optional[float] = None,
        preconditions: Optional[list[str]] = None,
        required_context: Optional[list[str]] = None,
        permission_class: PermissionClass = PermissionClass.A1_PREPARE,
        retry_policy: str = "next_admission",
        cancellation_policy: str = "manual",
        idempotency_key: Optional[str] = None,
        proof_obligations: Optional[list[str]] = None,
    ) -> Intent:
        # Phase 1 fix #17 (Opus 5): validate retry_policy at create time
        # so a typo here raises immediately, not from inside the except
        # handler in execute_intent.
        if retry_policy not in VALID_RETRY_POLICIES:
            raise ValueError(
                f"retry_policy must be one of {sorted(VALID_RETRY_POLICIES)}; "
                f"got {retry_policy!r}"
            )
        if cancellation_policy not in VALID_CANCELLATION_POLICIES:
            raise ValueError(
                f"cancellation_policy must be one of "
                f"{sorted(VALID_CANCELLATION_POLICIES)}; "
                f"got {cancellation_policy!r}"
            )
        if idempotency_key and idempotency_key in self._by_idempotency_key:
            existing_id = self._by_idempotency_key[idempotency_key]
            return self._intents[existing_id]

        intent_id = str(uuid.uuid4())
        effective_idempotency_key = idempotency_key or intent_id

        intent = Intent(
            intent_id=intent_id,
            owner=owner,
            trigger_type=trigger_type,
            trigger_spec=trigger_spec,
            preconditions=list(preconditions or []),
            action=action,
            required_context=list(required_context or []),
            permission_class=permission_class,
            retry_policy=retry_policy,
            cancellation_policy=cancellation_policy,
            idempotency_key=effective_idempotency_key,
            due_at=due_at,
            expires_at=expires_at,
            proof_obligations=list(proof_obligations or []),
        )
        self._intents[intent_id] = intent
        if idempotency_key:
            self._by_idempotency_key[idempotency_key] = intent_id
        # Phase 5: durable intents log (before the Postgres sink — JSONL is
        # canonical during an outage, and effect_receipts FK to intents).
        self._persist_intent_create(intent)
        # Phase 4: durable sink (idempotent on intent_id).
        self._sink("intents", lambda w: w.write_intent(
            intent_id=intent.intent_id,
            owner=intent.owner,
            trigger_type=intent.trigger_type,
            trigger_spec=intent.trigger_spec,
            action=intent.action,
            permission_class=intent.permission_class.value,
            retry_policy=intent.retry_policy,
            idempotency_key=intent.idempotency_key,
            due_at=intent.due_at,
            expires_at=intent.expires_at,
            status=intent.status.value,
        ))
        return intent

    def cancel_intent(self, intent_id: str, reason: str = "") -> Optional[Intent]:
        intent = self._intents.get(intent_id)
        if intent is None:
            return None
        if intent.status in {IntentStatus.COMPLETED, IntentStatus.CANCELLED}:
            return intent
        intent.status = IntentStatus.CANCELLED
        intent.completed_at = time.time()
        if reason:
            new_obligations = list(intent.proof_obligations) + [f"CANCELLED: {reason}"]
            intent.proof_obligations = new_obligations
        # Phase 5: durable intents log. Phase 4: durable status sink.
        self._persist_intent_status(intent)
        self._sink("intents.status", lambda w: w.update_intent_status(
            intent_id, IntentStatus.CANCELLED.value,
        ))
        return intent

    def get_intent(self, intent_id: str) -> Optional[Intent]:
        return self._intents.get(intent_id)

    def due_intents(self, now: Optional[float] = None) -> list[Intent]:
        now = now if now is not None else time.time()
        return [
            i for i in self._intents.values()
            if i.status == IntentStatus.PENDING
            and i.due_at is not None
            and i.due_at <= now
        ]

    def expire_overdue(self, now: Optional[float] = None) -> list[Intent]:
        now = now if now is not None else time.time()
        expired: list[Intent] = []
        for intent in self._intents.values():
            if (
                intent.status in {IntentStatus.PENDING, IntentStatus.DUE}
                and intent.expires_at is not None
                and intent.expires_at <= now
            ):
                intent.status = IntentStatus.EXPIRED
                intent.completed_at = now
                expired.append(intent)
        # Phase 4 re-review fix (F2): expire transitions are terminal and
        # must sink like every other transition. Phase 5: intents log first.
        for intent in expired:
            self._persist_intent_status(intent)
            self._sink("intents.status", lambda w, iid=intent.intent_id:
                       w.update_intent_status(iid, IntentStatus.EXPIRED.value))
        return expired

    def _apply_retry_policy(self, intent: Intent) -> bool:
        """Phase 1: retry_policy is now READ and applied.

        For "next_admission", a FAILED intent transitions back to PENDING
        for re-execution at the next admission. For other policies, this
        function returns False WITHOUT raising (Opus 5 #17: retry policy
        was previously raised inside the except handler, destroying the
        IntentExecutionResult).
        """
        if intent.retry_policy not in VALID_RETRY_POLICIES:
            # Invalid policy (defense in depth: create_intent also validates).
            # Return False so caller can decide what to do.
            return False
        if intent.retry_policy == "next_admission":
            intent.status = IntentStatus.PENDING
            intent.attempts = 0
            # Phase 4 re-review fix (F1): a re-pend is not a completion —
            # clear the in-memory completed_at so it cannot diverge from
            # Postgres (where the CASE keeps it NULL on non-terminal rows).
            intent.completed_at = None
            return True
        # exponential_backoff / fixed_delay / manual_only — not yet wired
        # to a scheduler; leave intent in FAILED state.
        return False

    def approve_blocked_intent(
        self, intent_id: str, approval_token: ApprovalToken,
    ) -> Intent:
        """Phase 1: BLOCKED can transition back to PENDING on approval."""
        intent = self._intents.get(intent_id)
        if intent is None:
            raise KeyError(f"unknown intent: {intent_id}")
        if intent.status != IntentStatus.BLOCKED:
            raise ValueError(
                f"intent is not BLOCKED (status={intent.status.value})"
            )
        if not approval_token.verify(self._approval_secret):
            raise ValueError("approval token signature verification failed")
        intent.approval_token = approval_token
        intent.status = IntentStatus.PENDING
        # Phase 5: durable intents log. Phase 4: durable status sink.
        self._persist_intent_status(intent)
        self._sink("intents.status", lambda w: w.update_intent_status(
            intent_id, IntentStatus.PENDING.value,
        ))
        return intent

    def execute_intent(
        self,
        intent_id: str,
        executor_fn: Optional[Callable] = None,
        approval_token: Optional[ApprovalToken] = None,
    ) -> IntentExecutionResult:
        """Phase 1: A3/A4 gated by signed ApprovalToken; execution fencing."""
        intent = self._intents.get(intent_id)
        if intent is None:
            return IntentExecutionResult(
                intent_id=intent_id,
                executed=False,
                blocked_reason="unknown intent_id",
            )
        if intent.status in {IntentStatus.COMPLETED, IntentStatus.CANCELLED, IntentStatus.EXPIRED}:
            return IntentExecutionResult(
                intent_id=intent_id,
                executed=False,
                blocked_reason=f"intent already in terminal state: {intent.status.value}",
            )

        # Phase 1: cockpit gate — kill/pause/budget enforcement
        # Opus 5 #6: every intent execute consumes budget.
        try:
            assert_active(
                self._cockpit,
                f"intent.execute({intent.action})",
                kind=OperationKind.WRITE,
            )
            if not consume_budget(self._cockpit, 0.05):
                intent.status = IntentStatus.BLOCKED
                return IntentExecutionResult(
                    intent_id=intent_id,
                    executed=False,
                    blocked_reason="budget exhausted",
                )
        except ControlBlocked as e:
            return IntentExecutionResult(
                intent_id=intent_id,
                executed=False,
                blocked_reason=f"cockpit blocked: {e.reason}",
            )

        # Phase 1: A3/A4 require signed approval token
        if intent.permission_class in GATED_PERMISSION_CLASSES:
            token = approval_token or intent.approval_token
            if token is None:
                intent.status = IntentStatus.BLOCKED
                return IntentExecutionResult(
                    intent_id=intent_id,
                    executed=False,
                    blocked_reason=(
                        f"{intent.permission_class.value} requires "
                        f"signed ApprovalToken"
                    ),
                )
            if not token.verify(self._approval_secret):
                intent.status = IntentStatus.BLOCKED
                return IntentExecutionResult(
                    intent_id=intent_id,
                    executed=False,
                    blocked_reason="approval token signature invalid",
                )

        # Phase 1: execution fencing token prevents double-execution
        if intent.execution_fencing_token > 0:
            return IntentExecutionResult(
                intent_id=intent_id,
                executed=False,
                blocked_reason="already executing (fencing token held)",
            )

        # Mark as running with fencing token
        intent.status = IntentStatus.RUNNING
        intent.execution_fencing_token = 1
        intent.attempts += 1
        started = time.time()

        fn = executor_fn if executor_fn is not None else self._executor
        effect_receipt_id: Optional[str] = None
        result_text = ""
        try:
            if fn is None:
                result_text = f"executed: {intent.action}"
            else:
                result_text = fn(intent)
            effect_receipt_id = str(uuid.uuid4())

            # Phase 1: persist effect receipt (durable)
            receipt = {
                "receipt_id": effect_receipt_id,
                "intent_id": intent_id,
                "result": result_text,
                "permission_class": intent.permission_class.value,
                "approver_id": (
                    approval_token.approver_id
                    if approval_token else
                    (intent.approval_token.approver_id
                     if intent.approval_token else None)
                ),
                "timestamp": time.time(),
            }
            self._effect_receipts.append(receipt)
            self._persist_receipt(receipt)
            # Phase 4: durable sinks (idempotent on receipt_id / intent_id).
            self._sink("effect_receipts", lambda w: w.write_effect_receipt(
                receipt_id=effect_receipt_id,
                intent_id=intent_id,
                approver_id=receipt["approver_id"],
                permission_class=intent.permission_class.value,
                result=result_text,
            ))

            intent.status = IntentStatus.COMPLETED
            intent.completed_at = time.time()
            intent.execution_fencing_token = 0
            self._persist_intent_status(intent)
            self._sink("intents.status", lambda w: w.update_intent_status(
                intent_id, IntentStatus.COMPLETED.value,
            ))
            return IntentExecutionResult(
                intent_id=intent_id,
                executed=True,
                result=result_text,
                effect_receipt_id=effect_receipt_id,
                duration_seconds=time.time() - started,
                attempts=intent.attempts,
            )
        except Exception as e:
            attempts_so_far = intent.attempts
            intent.execution_fencing_token = 0
            intent.status = IntentStatus.FAILED
            intent.completed_at = time.time()
            # Phase 1: retry policy application on failure
            if attempts_so_far < 3:
                self._apply_retry_policy(intent)
            # Phase 4: durable status transition (FAILED, or re-pended by
            # the retry policy — sink the effective status). Phase 5: the
            # durable intents log gets the same line first.
            self._persist_intent_status(intent)
            self._sink("intents.status", lambda w: w.update_intent_status(
                intent_id, intent.status.value,
            ))
            return IntentExecutionResult(
                intent_id=intent_id,
                executed=False,
                result=str(e),
                duration_seconds=time.time() - started,
                attempts=attempts_so_far,
                blocked_reason="executor raised; re-admitted",
            )

    def all_intents(self) -> list[Intent]:
        return list(self._intents.values())

    def effect_receipts(self) -> list[dict]:
        return list(self._effect_receipts)