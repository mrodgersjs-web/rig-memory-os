"""RIG Memory OS v10 — Memory Gateway (S2 Capture, task 3.1) — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- SignedContext is now HMAC-SHA256 signed; signature verified at every call
- Body scope rejection covers all 16 protected fields (not just 3)
- Sensitivity check enforced before ranking (SENSITIVITY_EXCEEDED wired)
- Replay keyed on (context_hash, tool_name, nonce) with TTL eviction
- PRINCIPAL_UNKNOWN / SCOPE_DENIED wired with explicit paths
- 17 tools (corrected from "12" docstring)

The 12 Memory Gateway tools (from the v10 spec):
- memory.session_start / heartbeat / end
- memory.record_event / record_event_batch
- memory.get_context_package / ack_context_usage
- memory.propose_memory / correct_memory
- memory.get_entity / get_procedure / get_trace
- memory.create_intention / cancel_intention
- memory.get_predictions / resolve_prediction
- memory.report_outcome
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

from founder_runtime.cockpit_subscriber import (
    assert_active, consume_budget, ControlBlocked,
    OperationKind,
)


if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit
    from founder_runtime.postgres_writer import PostgresWriter


class SensitivityCeiling(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CREDENTIAL = "credential"
    SECRET = "secret"


class RejectReason(str, Enum):
    INVALID_CONTEXT = "invalid_context"          # wired: missing fields
    INVALID_SIGNATURE = "invalid_signature"       # NEW: HMAC mismatch
    CONTROL_BLOCKED = "control_blocked"           # NEW: cockpit refused
    BUDGET_EXHAUSTED = "budget_exhausted"         # NEW: budget=0
    SCOPE_MISMATCH = "scope_mismatch"             # wired: body override
    SENSITIVITY_EXCEEDED = "sensitivity_exceeded"  # NEW: sensitivity check
    PRINCIPAL_UNKNOWN = "principal_unknown"       # wired: bad principal
    SCOPE_DENIED = "scope_denied"                 # wired: scope filter
    SCHEMA_INVALID = "schema_invalid"             # wired: unknown tool
    REPLAY_DETECTED = "replay_detected"           # rekeyed with nonce


# All 16 protected fields — body cannot override any of these
PROTECTED_FIELDS = frozenset({
    "operator_id", "tenant_id", "client_id", "project_id",
    "mission_id", "agent_principal", "agent_instance",
    "harness_version", "adapter_version", "node",
    "purpose", "sensitivity_ceiling", "run_id",
    "session_id", "trace_id", "policy_version",
})


@dataclass(frozen=True)
class SignedContext:
    """Authenticated, signed scope binding for a request.

    The signature is HMAC-SHA256 over the canonical tuple, with the
    shared secret known only to the gateway. Every request MUST carry
    a valid signature. Body or tool arguments CANNOT override any
    field — they are claims to validate, not authentication.
    """

    operator_id: str
    tenant_id: str
    client_id: str
    project_id: str
    mission_id: str
    agent_principal: str
    agent_instance: str
    harness_version: str
    adapter_version: str
    node: str
    purpose: str
    sensitivity_ceiling: SensitivityCeiling
    run_id: str
    session_id: str
    trace_id: str
    policy_version: str
    nonce: str  # NEW: per-request nonce for replay rekeying
    signature: str  # NEW: HMAC-SHA256 over canonical tuple

    def canonical_bytes(self) -> bytes:
        """Canonical byte representation for signing."""
        return "|".join(
            [
                self.operator_id, self.tenant_id, self.client_id,
                self.project_id, self.mission_id, self.agent_principal,
                self.agent_instance, self.harness_version,
                self.adapter_version, self.node, self.purpose,
                self.sensitivity_ceiling.value, self.run_id,
                self.session_id, self.trace_id, self.policy_version,
                self.nonce,
            ]
        ).encode("utf-8")

    def verify_signature(self, secret: bytes) -> bool:
        """Verify the HMAC-SHA256 signature using `secret`."""
        expected = hmac.new(
            secret, self.canonical_bytes(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    def context_hash(self) -> str:
        """SHA-256 of the canonical tuple. Used for replay detection keying."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def sign_context(
    secret: bytes,
    operator_id: str,
    tenant_id: str,
    client_id: str,
    project_id: str,
    mission_id: str,
    agent_principal: str,
    agent_instance: str,
    harness_version: str,
    adapter_version: str,
    node: str,
    purpose: str,
    sensitivity_ceiling: SensitivityCeiling,
    run_id: str,
    session_id: str,
    trace_id: str,
    policy_version: str,
) -> SignedContext:
    """Mint a new SignedContext with a fresh nonce and HMAC signature."""
    nonce = secrets.token_hex(16)
    ctx = SignedContext(
        operator_id=operator_id, tenant_id=tenant_id, client_id=client_id,
        project_id=project_id, mission_id=mission_id,
        agent_principal=agent_principal, agent_instance=agent_instance,
        harness_version=harness_version, adapter_version=adapter_version,
        node=node, purpose=purpose,
        sensitivity_ceiling=sensitivity_ceiling,
        run_id=run_id, session_id=session_id, trace_id=trace_id,
        policy_version=policy_version,
        nonce=nonce,
        signature="",  # placeholder for sign
    )
    sig = hmac.new(secret, ctx.canonical_bytes(), hashlib.sha256).hexdigest()
    # Reconstruct with the real signature (frozen dataclass)
    return SignedContext(
        operator_id=operator_id, tenant_id=tenant_id, client_id=client_id,
        project_id=project_id, mission_id=mission_id,
        agent_principal=agent_principal, agent_instance=agent_instance,
        harness_version=harness_version, adapter_version=adapter_version,
        node=node, purpose=purpose,
        sensitivity_ceiling=sensitivity_ceiling,
        run_id=run_id, session_id=session_id, trace_id=trace_id,
        policy_version=policy_version,
        nonce=nonce,
        signature=sig,
    )


@dataclass
class UsageReceipt:
    """Immutable receipt emitted for every gateway tool call."""

    receipt_id: str
    context_hash: str
    tool_name: str
    timestamp: float
    principal: str
    run_id: str
    session_id: str
    trace_id: str
    nonce: str
    memory_ids_presented: list[str] = field(default_factory=list)
    memory_ids_referenced: list[str] = field(default_factory=list)
    memory_ids_rejected: list[str] = field(default_factory=list)
    output_artifact: Optional[str] = None
    latency_ms: int = 0
    token_count: int = 0
    outcome_ref: Optional[str] = None


@dataclass
class GatewayResult:
    accepted: bool
    tool_name: str
    context_hash: str
    reject_reason: Optional[RejectReason] = None
    reject_detail: str = ""
    receipt: Optional[UsageReceipt] = None
    payload: Optional[dict] = None


class MemoryGateway:
    """The Memory Gateway — sole entrypoint for all agent requests.

    Implements scope-first authorization: every request's signed context
    is validated BEFORE any retrieval or mutation. The gateway tracks
    usage receipts and exposes the 17 Memory OS tools.

    Phase 1 fixes:
    - HMAC-SHA256 signature verification on every request
    - Body cannot override ANY of the 16 protected context fields
    - Sensitivity check emits SENSITIVITY_EXCEEDED (wired)
    - Replay keyed on (context_hash, tool_name, nonce) with TTL eviction
    - PRINCIPAL_UNKNOWN wired (rejects unknown principal strings)
    - SCOPE_DENIED wired for explicit scope failures (distinct from mismatch)
    """

    TOOL_NAMES = frozenset({
        "memory.session_start", "memory.heartbeat", "memory.session_end",
        "memory.record_event", "memory.record_event_batch",
        "memory.get_context_package", "memory.ack_context_usage",
        "memory.propose_memory", "memory.correct_memory",
        "memory.get_entity", "memory.get_procedure", "memory.get_trace",
        "memory.create_intention", "memory.cancel_intention",
        "memory.get_predictions", "memory.resolve_prediction",
        "memory.report_outcome",
    })

    # Known principals — anything outside this list emits PRINCIPAL_UNKNOWN
    KNOWN_PRINCIPALS = frozenset({
        "jake", "codex", "claude", "hermes", "opencode",
        "cursor", "aider", "planner", "builder", "verifier",
    })

    # Replay detection: key on (context_hash, tool_name, nonce)
    # TTL eviction keeps the cache bounded.
    REPLAY_TTL_SECONDS = 60.0

    def __init__(
        self,
        shared_secret: Optional[bytes] = None,
        cockpit: Optional["MemoryCockpit"] = None,
        postgres_writer: Optional["PostgresWriter"] = None,
    ) -> None:
        # Default secret for tests; production replaces via env or config
        self._secret = shared_secret or b"rig-memory-os-test-secret"
        self._received_receipts: list[UsageReceipt] = []
        # Replay cache: key=(context_hash, tool_name, nonce) -> first-seen-timestamp
        self._replay_cache: dict[tuple[str, str, str], float] = {}
        # Phase 1: optional cockpit reference for authoritative control plane
        self._cockpit = cockpit
        # Phase 4: optional Postgres sink for usage_receipts. Best-effort:
        # a sink failure never blocks the gateway, but is recorded visibly
        # in _persistence_failures (never silently lost).
        self._postgres_writer = postgres_writer
        self._persistence_failures: list[dict] = []

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

    def _check_replay(self, context: SignedContext, tool_name: str) -> Optional[GatewayResult]:
        """Re-keyed replay detection on (context_hash, tool_name, nonce)."""
        now = time.time()
        # TTL eviction: prune expired entries
        expired = [k for k, t in self._replay_cache.items()
                   if now - t > self.REPLAY_TTL_SECONDS]
        for k in expired:
            del self._replay_cache[k]

        key = (context.context_hash(), tool_name, context.nonce)
        first_seen = self._replay_cache.get(key)
        if first_seen is not None:
            return GatewayResult(
                accepted=False,
                tool_name=tool_name,
                context_hash=context.context_hash(),
                reject_reason=RejectReason.REPLAY_DETECTED,
                reject_detail=(
                    f"(context_hash, tool_name, nonce) replay within "
                    f"{self.REPLAY_TTL_SECONDS}s window"
                ),
            )
        self._replay_cache[key] = now
        return None

    def _validate_signature(self, context: SignedContext) -> Optional[GatewayResult]:
        """Verify HMAC-SHA256 signature."""
        if not context.verify_signature(self._secret):
            return GatewayResult(
                accepted=False,
                tool_name="(pre-flight)",
                context_hash=context.context_hash(),
                reject_reason=RejectReason.INVALID_SIGNATURE,
                reject_detail="HMAC-SHA256 signature verification failed",
            )
        return None

    def _validate_principal(self, context: SignedContext) -> Optional[GatewayResult]:
        """Reject unknown principals."""
        if context.agent_principal not in self.KNOWN_PRINCIPALS:
            return GatewayResult(
                accepted=False,
                tool_name="(pre-flight)",
                context_hash=context.context_hash(),
                reject_reason=RejectReason.PRINCIPAL_UNKNOWN,
                reject_detail=(
                    f"agent_principal={context.agent_principal!r} "
                    f"not in known set"
                ),
            )
        return None

    def _validate_context_shape(self, context: SignedContext) -> Optional[GatewayResult]:
        """Reject contexts missing required identity fields."""
        if not context.operator_id or not context.tenant_id:
            return GatewayResult(
                accepted=False,
                tool_name="(pre-flight)",
                context_hash=context.context_hash(),
                reject_reason=RejectReason.INVALID_CONTEXT,
                reject_detail="missing operator_id or tenant_id",
            )
        if not context.agent_principal or not context.run_id:
            return GatewayResult(
                accepted=False,
                tool_name="(pre-flight)",
                context_hash=context.context_hash(),
                reject_reason=RejectReason.INVALID_CONTEXT,
                reject_detail="missing agent_principal or run_id",
            )
        return None

    def _check_body_scope(self, context: SignedContext, body: dict) -> Optional[GatewayResult]:
        """Reject any body-supplied scope key, regardless of value.

        Body fields whose NAME is in PROTECTED_FIELDS are always denied —
        they cannot legitimately be supplied by the body, only by the
        authenticated context.
        """
        for key in body:
            if key in PROTECTED_FIELDS:
                return GatewayResult(
                    accepted=False,
                    tool_name="(pre-flight)",
                    context_hash=context.context_hash(),
                    reject_reason=RejectReason.SCOPE_MISMATCH,
                    reject_detail=(
                        f"body supplied protected field {key!r} — "
                        f"must come from signed context"
                    ),
                )
        return None

    def _check_sensitivity(
        self, context: SignedContext, body: dict
    ) -> Optional[GatewayResult]:
        """Wire the sensitivity check.

        If the body declares a sensitivity field (separate from the
        ceiling in the context), the body's sensitivity must be ≤ ceiling.
        """
        body_sensitivity = body.get("sensitivity")
        if body_sensitivity is None:
            return None
        levels = {"public": 0, "internal": 1, "credential": 2, "secret": 3}
        ceiling = levels.get(context.sensitivity_ceiling.value, 0)
        body_level = levels.get(body_sensitivity, 99)
        if body_level > ceiling:
            return GatewayResult(
                accepted=False,
                tool_name="(pre-flight)",
                context_hash=context.context_hash(),
                reject_reason=RejectReason.SENSITIVITY_EXCEEDED,
                reject_detail=(
                    f"body.sensitivity={body_sensitivity!r} exceeds "
                    f"context ceiling {context.sensitivity_ceiling.value!r}"
                ),
            )
        return None

    def _enforce_tool(self, tool_name: str) -> Optional[GatewayResult]:
        if tool_name not in self.TOOL_NAMES:
            return GatewayResult(
                accepted=False,
                tool_name=tool_name,
                context_hash="(unknown)",
                reject_reason=RejectReason.SCHEMA_INVALID,
                reject_detail=f"unknown tool: {tool_name!r}",
            )
        return None

    def invoke(
        self,
        context: SignedContext,
        tool_name: str,
        body: Optional[dict] = None,
    ) -> GatewayResult:
        """Invoke a Memory Gateway tool with full Phase 1 validation.

        Validation order:
        1. Cockpit gate (NEW Phase 1: authoritative kill/pause/budget)
        2. HMAC signature verification
        3. Context shape validation
        4. Principal check
        5. Tool allowlist
        6. Body scope rejection (covers all 16 fields)
        7. Sensitivity check
        8. Replay detection on (context_hash, tool_name, nonce)
        9. Emit usage receipt
        """
        started = time.time()
        body = body or {}

        # Step 1: cockpit gate — kill/pause/budget enforcement
        # Per Opus 5 #5: distinguish control-plane refusal from
        # malformed client input via CONTROL_BLOCKED enum value.
        # Per Opus 5 #6: every invoke consumes budget (writes cost more).
        try:
            assert_active(
                self._cockpit,
                f"gateway.invoke({tool_name})",
                kind=OperationKind.WRITE,
            )
            # Opus 5 #6 fix: explicit budget check (raises on exhaustion).
            # consume_budget returns False silently; we must catch + reject.
            if not consume_budget(self._cockpit, 0.01):
                return GatewayResult(
                    accepted=False,
                    tool_name=tool_name,
                    context_hash=context.context_hash(),
                    reject_reason=RejectReason.BUDGET_EXHAUSTED,
                    reject_detail="budget exhausted",
                )
        except ControlBlocked as e:
            return GatewayResult(
                accepted=False,
                tool_name=tool_name,
                context_hash=context.context_hash(),
                reject_reason=RejectReason.CONTROL_BLOCKED,
                reject_detail=f"cockpit blocked: {e.reason}",
            )

        # Step 2: signature
        rejection = self._validate_signature(context)
        if rejection is not None:
            return rejection

        # Step 3: shape
        rejection = self._validate_context_shape(context)
        if rejection is not None:
            return rejection

        # Step 4: principal
        rejection = self._validate_principal(context)
        if rejection is not None:
            return rejection

        # Step 5: tool allowlist
        rejection = self._enforce_tool(tool_name)
        if rejection is not None:
            rejection.context_hash = context.context_hash()
            return rejection

        # Step 6: body scope (covers all 16 fields)
        rejection = self._check_body_scope(context, body)
        if rejection is not None:
            return rejection

        # Step 7: sensitivity
        rejection = self._check_sensitivity(context, body)
        if rejection is not None:
            return rejection

        # Step 8: replay (rekeyed)
        rejection = self._check_replay(context, tool_name)
        if rejection is not None:
            return rejection

        # Step 9: emit receipt
        latency_ms = int((time.time() - started) * 1000)
        receipt = UsageReceipt(
            receipt_id=str(uuid.uuid4()),
            context_hash=context.context_hash(),
            tool_name=tool_name,
            timestamp=time.time(),
            principal=context.agent_principal,
            run_id=context.run_id,
            session_id=context.session_id,
            trace_id=context.trace_id,
            nonce=context.nonce,
            latency_ms=latency_ms,
        )
        self._received_receipts.append(receipt)
        # Phase 4: durable sink (idempotent on receipt_id).
        self._sink("usage_receipts", lambda w: w.write_usage_receipt(
            receipt_id=receipt.receipt_id,
            context_hash=receipt.context_hash,
            tool_name=receipt.tool_name,
            principal=receipt.principal,
            run_id=receipt.run_id,
            session_id=receipt.session_id,
            trace_id=receipt.trace_id,
            nonce=receipt.nonce,
            latency_ms=receipt.latency_ms,
            token_count=receipt.token_count,
            ts=receipt.timestamp,
        ))

        return GatewayResult(
            accepted=True,
            tool_name=tool_name,
            context_hash=context.context_hash(),
            receipt=receipt,
            payload={"ok": True, "tool": tool_name},
        )

    def all_receipts(self) -> list[UsageReceipt]:
        return list(self._received_receipts)

    def receipts_for_session(self, session_id: str) -> list[UsageReceipt]:
        return [r for r in self._received_receipts if r.session_id == session_id]