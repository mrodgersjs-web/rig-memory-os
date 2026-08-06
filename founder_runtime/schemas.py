"""RIG Memory OS v10 — Phase 0 schemas (S0 Contracts).

Defines the JSON Schemas for the canonical wire formats used across the
Memory OS at the contract layer. These are the authoritative envelope
shapes that all eight memory layers and the predictive control plane share.

Following the v10 spec:
- Raw events are append-only
- Each envelope carries provenance, scope, sensitivity, and a content hash
- Predictions live in a speculative namespace and never become facts
- Generators, evaluators, and verifiers are separate identities
"""

from __future__ import annotations

# =====================================================================
# S0.1 MemoryEnvelope — canonical event wrapper for all eight layers
# =====================================================================

MEMORY_ENVELOPE_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "MemoryEnvelope",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "envelope_version",
        "schema_id",
        "event_id",
        "timestamp",
        "destination_agent",
        "destination_agent_id",
        "origin_agent",
        "origin_agent_id",
        "correlation_id",
        "scope",
        "provenance",
        "sensitivity",
        "retention_policy",
        "writer_id",
        "content_hash",
        "content",
    ],
    "properties": {
        "envelope_version": {"type": "string", "const": "1"},
        "schema_id": {"type": "string", "const": "memory-envelope"},
        "event_id": {"type": "string", "format": "uuid"},
        "timestamp": {"type": "string", "format": "date-time"},
        "destination_agent": {"type": "string"},
        "destination_agent_id": {"type": "string", "format": "uuid"},
        "origin_agent": {"type": "string"},
        "origin_agent_id": {"type": "string", "format": "uuid"},
        "correlation_id": {"type": "string", "format": "uuid"},
        "run_id": {"type": ["string", "null"], "format": "uuid"},
        "scope": {
            "type": "string",
            "enum": ["local", "client", "control-plane", "fleet"],
        },
        "provenance": {"type": "string", "description": "source_uri / tool name"},
        "sensitivity": {
            "type": "string",
            "enum": ["public", "internal", "credential", "secret"],
        },
        "retention_policy": {
            "type": "string",
            "enum": ["30d", "90d", "365d", "permanent", "session"],
        },
        "writer_id": {"type": "string", "description": "principal that wrote the event"},
        "content_hash": {
            "type": "string",
            "description": "SHA-256 hex of canonical content field",
        },
        "content": {"type": "object"},
        "signature": {
            "type": "string",
            "description": "ed25519 signature over all required fields (excluding signature)",
        },
    },
}


# =====================================================================
# S0.2 ProofPacket — verifiable Phase 0 evidence
# =====================================================================

PROOF_PACKET_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ProofPacket",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "envelope_version",
        "schema_id",
        "proof_id",
        "scope",
        "verifier_node",
        "verifier_model",
        "verdict",
        "proven_at",
        "commands",
        "results",
        "artifact_hashes",
        "signature",
    ],
    "properties": {
        "envelope_version": {"type": "string", "const": "1"},
        "schema_id": {"type": "string", "const": "proof-packet"},
        "proof_id": {"type": "string", "format": "uuid"},
        "scope": {
            "type": "string",
            "enum": ["local", "client", "control-plane", "fleet"],
        },
        "verifier_node": {"type": "string"},
        "verifier_model": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "FAIL", "REOPEN"]},
        "proven_at": {"type": "string", "format": "date-time"},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["cmd", "args", "exit_code"],
                "properties": {
                    "cmd": {"type": "string"},
                    "args": {"type": "object"},
                    "exit_code": {"type": "integer"},
                    "stdout_sha256": {"type": "string"},
                    "stderr_sha256": {"type": "string"},
                    "duration_ms": {"type": "integer"},
                },
            },
        },
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["check_name", "passed"],
                "properties": {
                    "check_name": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
            },
        },
        "artifact_hashes": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "source_lineage": {"type": "array", "items": {"type": "string"}},
        "residual_risks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["risk_id", "description"],
                "properties": {
                    "risk_id": {"type": "string"},
                    "description": {"type": "string"},
                    "mitigation_status": {"type": "string"},
                },
            },
        },
        "signature": {"type": "string"},
    },
}


# =====================================================================
# Required source types for L2 episodic memory
# =====================================================================

REQUIRED_SOURCE_TYPES = frozenset(
    {
        "user_supplied",
        "agent_observed",
        "tool_observed",
        "imported_reference",
        "model_extracted",
        "model_synthesized",
        "human_approved",
        "verifier_approved",
        "rejected",
        "archived",
    }
)


def validate_memory_envelope(payload: dict) -> tuple[bool, str]:
    """Lightweight validator for MemoryEnvelope payloads.

    Returns (is_valid, error_message). For full JSON-Schema validation,
    use jsonschema (out of scope for Phase 0 — kept local-only).
    """
    required = MEMORY_ENVELOPE_SCHEMA["required"]
    missing = [f for f in required if f not in payload]
    if missing:
        return False, f"missing required fields: {missing}"

    if payload.get("envelope_version") != "1":
        return False, "envelope_version must be '1'"
    if payload.get("schema_id") != "memory-envelope":
        return False, "schema_id must be 'memory-envelope'"
    if payload.get("scope") not in {"local", "client", "control-plane", "fleet"}:
        return False, "scope invalid"
    if payload.get("sensitivity") not in {"public", "internal", "credential", "secret"}:
        return False, "sensitivity invalid"

    return True, ""


def validate_proof_packet(payload: dict) -> tuple[bool, str]:
    """Lightweight validator for ProofPacket payloads."""
    required = PROOF_PACKET_SCHEMA["required"]
    missing = [f for f in required if f not in payload]
    if missing:
        return False, f"missing required fields: {missing}"
    if payload.get("verdict") not in {"PASS", "FAIL", "REOPEN"}:
        return False, "verdict must be PASS, FAIL, or REOPEN"
    return True, ""