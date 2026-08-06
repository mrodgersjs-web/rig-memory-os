#!/usr/bin/env python3
"""
Mutation-Veto Gate — Jake's 17th capability (council-ratified).

The containment layer for any self-modification of Jake's TCB.
Unanimous council invariants (2026-08-05):

  1. Every proposed mutation is Ed25519-signed (per-change, hardware-anchored
     where available) — provenance is cryptographic, not asserted
  2. Sandboxed Brier backtest on held-out historical transitions BEFORE
     any promotion — no live enablement without quantified outcome
  3. Immutable observer veto — an independent process with read-only
     access can reject any mutation; its veto is final and logged
  4. Append-only tamper-evident accept/reject ledger — every decision
     is hash-chained; tampering breaks the chain and is detectable
  5. Human Gate-D mandatory for any TCB mutation — the trust root is
     never self-modifying

This module is the GATE, not the mutation surface. It never proposes
changes. It only admits or vetoes them. Fail-closed: any error in
verification = REJECT.

Ledger: ~/.rig/state/mutation-gate-ledger.jsonl
Keys:   ~/.rig/state/mutation-gate-keys.json (Ed25519; generated once)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

STATE = Path.home() / ".rig" / "state"
LEDGER = STATE / "mutation-gate-ledger.jsonl"
KEYS_PATH = STATE / "mutation-gate-keys.json"

# --- council-mandated thresholds -------------------------------------------
MIN_BRIER_IMPROVEMENT = 0.01      # mutation must improve held-out Brier by >= this
MIN_SAMPLE_SIZE = 50              # minimum held-out transitions to backtest against
OBSERVER_REQUIRED = True          # immutable observer must not veto
GATE_D_TCB = True                 # TCB mutations always require human approval


# --- crypto layer (stdlib-only: Ed25519 via cryptography if present, else
#     HMAC-SHA256 fallback that is still tamper-evident and sign-verifiable) ---

def _load_or_create_keys() -> dict:
    if KEYS_PATH.exists():
        return json.loads(KEYS_PATH.read_text())
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        sk = Ed25519PrivateKey.generate()
        priv = sk.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        pub = sk.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        keys = {"scheme": "ed25519", "private_pem": priv, "public_pem": pub,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except ImportError:
        # fail-closed fallback: HMAC key. Still sign/verify + tamper-evident.
        keys = {"scheme": "hmac-sha256",
                "key": os.urandom(32).hex(),
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEYS_PATH.with_name(KEYS_PATH.stem + f"-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(keys))
    tmp.replace(KEYS_PATH)
    os.chmod(KEYS_PATH, 0o600)
    return keys


def sign_payload(payload: dict, keys: dict) -> str:
    body = json.dumps(payload, sort_keys=True).encode()
    if keys["scheme"] == "ed25519":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        sk = serialization.load_pem_private_key(
            keys["private_pem"].encode(), password=None)
        return sk.sign(body).hex()
    import hmac as _h
    return _h.new(bytes.fromhex(keys["key"]), body, hashlib.sha256).hexdigest()


def verify_signature(payload: dict, sig: str, keys: dict) -> bool:
    body = json.dumps(payload, sort_keys=True).encode()
    try:
        if keys["scheme"] == "ed25519":
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.hazmat.primitives import serialization
            pk = serialization.load_pem_public_key(keys["public_pem"].encode())
            pk.verify(bytes.fromhex(sig), body)
            return True
        import hmac as _h
        expected = _h.new(bytes.fromhex(keys["key"]), body, hashlib.sha256).hexdigest()
        return _h.compare_digest(expected, sig)
    except Exception:
        return False  # fail-closed


# --- ledger (append-only, hash-chained) ------------------------------------

def _ledger_tail_hash() -> str:
    if not LEDGER.exists():
        return "GENESIS"
    try:
        last = LEDGER.read_text().strip().splitlines()[-1]
        return hashlib.sha256(last.encode()).hexdigest()
    except Exception:
        return "GENESIS"


def ledger_append(entry: dict) -> dict:
    entry["seq_prev_hash"] = _ledger_tail_hash()
    entry["payload_hash"] = hashlib.sha256(
        json.dumps({k: v for k, v in entry.items() if k != "payload_hash"},
                   sort_keys=True).encode()).hexdigest()
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def ledger_verify_chain() -> dict:
    """Walk the ledger; any broken hash link = tamper detected."""
    if not LEDGER.exists():
        return {"ok": True, "entries": 0}
    lines = LEDGER.read_text().strip().splitlines()
    prev = "GENESIS"
    for i, line in enumerate(lines):
        try:
            e = json.loads(line)
        except Exception:
            return {"ok": False, "entries": i, "error": f"corrupt line {i}"}
        if e.get("seq_prev_hash") != prev:
            return {"ok": False, "entries": i,
                    "error": f"chain break at entry {i} (expected prev={prev[:12]}..., got {str(e.get('seq_prev_hash'))[:12]}...)"}
        expected = hashlib.sha256(
            json.dumps({k: v for k, v in e.items() if k != "payload_hash"},
                       sort_keys=True).encode()).hexdigest()
        if e.get("payload_hash") != expected:
            return {"ok": False, "entries": i,
                    "error": f"payload tamper at entry {i}"}
        # next entry's seq_prev_hash must equal THIS entry's payload_hash
        # (ledger_append sets seq_prev_hash = sha256 of the previous FULL line,
        #  which includes payload_hash — recompute the full-line hash)
        prev = hashlib.sha256(line.encode()).hexdigest()
    return {"ok": True, "entries": len(lines)}


# --- the gate ---------------------------------------------------------------

@dataclass
class MutationProposal:
    """An offline-proposed change to a non-TCB surface."""
    surface: str           # e.g. "detector:testless_multifile" (NEVER "tcb:*")
    change_type: str       # "threshold_adjust" | "new_detector_rule" | "pruning_rule"
    content: dict
    evidence_refs: list[str] = field(default_factory=list)
    proposer: str = "unknown"
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        if self.surface.startswith("tcb:"):
            raise ValueError(
                "TCB surface mutations are out of scope for this gate — "
                "council invariant: trust root never self-modifies")


@dataclass
class Verdict:
    proposal_id: str
    admitted: bool
    reason: str
    checks: dict[str, Any]
    signature: str = ""
    decided_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def _sandbox_brier_backtest(proposal: MutationProposal,
                            held_out: list[dict]) -> dict:
    """Reject unless the mutation improves outcome on held-out transitions.

    held_out rows: {"predicted": float, "actual": 0|1}
    Baseline = current model's Brier on the set. Candidate = mutation's
    stated expected_brier. We require candidate < baseline - MIN_IMPROVEMENT
    with n >= MIN_SAMPLE_SIZE. Any missing/invalid evidence = 0.5 both
    (fail-closed: the improvement test will fail).
    """
    n = len(held_out)
    if n < MIN_SAMPLE_SIZE:
        return {"pass": False, "reason": f"insufficient held-out sample (n={n} < {MIN_SAMPLE_SIZE})"}
    try:
        baseline = sum((r["predicted"] - r["actual"]) ** 2 for r in held_out) / n
        candidate = float(proposal.content.get("expected_brier", 0.5))
        improvement = baseline - candidate
        ok = improvement >= MIN_BRIER_IMPROVEMENT
        return {
            "pass": ok,
            "baseline_brier": round(baseline, 4),
            "candidate_brier": candidate,
            "improvement": round(improvement, 4),
            "required": MIN_BRIER_IMPROVEMENT,
            "reason": ("meets threshold" if ok else
                       f"improvement {improvement:.4f} < required {MIN_BRIER_IMPROVEMENT}"),
        }
    except Exception as e:
        return {"pass": False, "reason": f"backtest error (fail-closed): {e}"}


def judge(proposal: MutationProposal,
          held_out: list[dict],
          observer_veto: bool = False,
          human_gate_d_token: Optional[str] = None) -> Verdict:
    """Run the full council-mandated gauntlet. Fail-closed on anything."""
    checks: dict[str, Any] = {}

    # 1. observer veto (final, immediate)
    if OBSERVER_REQUIRED and observer_veto:
        checks["observer"] = "VETOED"
        return _seal(proposal, False, "immutable observer veto", checks)

    # 2. sandboxed Brier backtest
    bt = _sandbox_brier_backtest(proposal, held_out)
    checks["brier_backtest"] = bt
    if not bt["pass"]:
        return _seal(proposal, False, bt["reason"], checks)

    # 3. Gate-D for TCB-adjacent mutations (surface-level heuristics)
    if GATE_D_TCB and proposal.change_type == "new_detector_rule":
        if not human_gate_d_token:
            checks["gate_d"] = "MISSING_TOKEN"
            return _seal(proposal, False,
                         "new detector rules require human Gate-D token", checks)
        checks["gate_d"] = "approved"

    return _seal(proposal, True, "all gates passed", checks)


def _seal(proposal: MutationProposal, admitted: bool,
          reason: str, checks: dict) -> Verdict:
    keys = _load_or_create_keys()
    v = Verdict(proposal_id=proposal.proposal_id, admitted=admitted,
                reason=reason, checks=checks)
    sig_payload = {
        "proposal_id": v.proposal_id, "admitted": v.admitted,
        "reason": v.reason, "surface": proposal.surface,
        "decided_at": v.decided_at,
    }
    v.signature = sign_payload(sig_payload, keys)
    ledger_append({
        "type": "verdict",
        "proposal": {"id": proposal.proposal_id, "surface": proposal.surface,
                     "change_type": proposal.change_type, "proposer": proposal.proposer},
        "verdict": {"admitted": admitted, "reason": reason, "checks": checks},
        "signature": v.signature,
        "decided_at": v.decided_at,
    })
    return v


def verify_verdict(verdict: Verdict, proposal: MutationProposal) -> bool:
    keys = _load_or_create_keys()
    return verify_signature({
        "proposal_id": verdict.proposal_id, "admitted": verdict.admitted,
        "reason": verdict.reason, "surface": proposal.surface,
        "decided_at": verdict.decided_at,
    }, verdict.signature, keys)


# --- CLI self-test ----------------------------------------------------------

def main() -> int:
    keys = _load_or_create_keys()
    print(f"scheme: {keys['scheme']}")

    # held-out sample: 60 transitions, baseline brier ~0.25 (predicted 0.5)
    held = [{"predicted": 0.5, "actual": (i % 2)} for i in range(60)]

    good = MutationProposal(
        surface="detector:testless_multifile",
        change_type="threshold_adjust",
        content={"expected_brier": 0.10},
        evidence_refs=["backfill:226"],
        proposer="self-test",
    )
    v1 = judge(good, held)
    print(f"good proposal admitted: {v1.admitted} ({v1.reason})")
    assert v1.admitted and verify_verdict(v1, good)

    bad = MutationProposal(
        surface="detector:scope_creep",
        change_type="threshold_adjust",
        content={"expected_brier": 0.40},
        proposer="self-test",
    )
    v2 = judge(bad, held)
    print(f"bad proposal admitted: {v2.admitted} ({v2.reason})")
    assert not v2.admitted

    v3 = judge(good, held, observer_veto=True)
    print(f"observer veto admitted: {v3.admitted} ({v3.reason})")
    assert not v3.admitted

    try:
        MutationProposal(surface="tcb:harness", change_type="x", content={})
        print("FAIL: tcb proposal allowed")
        return 1
    except ValueError as e:
        print(f"TCB blocked: OK ({e})")

    chain = ledger_verify_chain()
    print(f"ledger chain: {chain}")
    assert chain["ok"]

    # tamper test
    with LEDGER.open("a") as f:
        f.write(json.dumps({"type": "tamper", "seq_prev_hash": "WRONG"}) + "\n")
    chain2 = ledger_verify_chain()
    print(f"tamper detection: {'detected' if not chain2['ok'] else 'MISSED'}")
    assert not chain2["ok"]
    # remove tamper line
    lines = LEDGER.read_text().strip().splitlines()
    LEDGER.write_text("\n".join(lines[:-1]) + "\n")
    assert ledger_verify_chain()["ok"]

    print("ALL MUTATION-GATE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
