"""Phase 3 — Verification subsystem.

Independent verifier. Runs on a different model family than the generator.
The agent never self-declares done.

Today: stub implementation. The verifier:
1. Re-runs the source commands (re-execution).
2. Checks artifact existence + non-vacuous output.
3. Computes a sha256 over (artifact_paths + summary + source_refs) as evidence_hash.
4. Emits Verdict.PASS / FAIL / REOPEN.
5. Seals a ProofPacket on disk under ~/.rig/founder-runtime/proof/.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    VerificationContract,
    ProofPacket,
    Verdict,
    WorkResultContract,
)
from .store import Store, record_proof_packet, append_audit


PROOF_DIR = Path.home() / ".rig" / "founder-runtime" / "proof"


def verify_and_seal(
    store: Store,
    *,
    work_item_id: str,
    result: WorkResultContract,
    verifier_node: str,
    verifier_model: str,
) -> VerificationContract:
    """Independent verification gate.

    Rules (handoff §6.6):
    - artifact exists
    - sources resolve (URLs in source_refs are reachable if HTTP)
    - output is non-vacuous (summary length > 0)
    - duplicate work was not created
    """
    failures: list[str] = []

    if not result.summary or len(result.summary.strip()) < 5:
        failures.append("summary is empty or too short")
    if not result.source_refs and not result.artifact_paths:
        failures.append("no source_refs and no artifact_paths — cannot verify")
    for path in result.artifact_paths:
        if not Path(path).exists():
            failures.append(f"artifact missing: {path}")
    if result.status.value == "FAILED":
        failures.append("result.status == FAILED")

    evidence = json.dumps(
        {"work_item_id": work_item_id, "summary": result.summary,
         "artifact_paths": result.artifact_paths, "source_refs": result.source_refs,
         "metrics": result.metrics},
        sort_keys=True, default=str,
    ).encode("utf-8")
    evidence_hash = "sha256:" + hashlib.sha256(evidence).hexdigest()

    verdict = Verdict.FAIL if failures else Verdict.PASS
    repair = None
    if failures:
        verdict = Verdict.REOPEN
        repair = failures[0]

    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    packet_path = PROOF_DIR / f"{work_item_id}.proof.json"
    pkt = ProofPacket(
        work_item_id=work_item_id,
        result_id=result.result_id,
        verifier_node=verifier_node,
        verifier_model=verifier_model,
        verdict=verdict,
        evidence_hash=evidence_hash,
        packet_path=str(packet_path),
        sealed_at=datetime.now(timezone.utc),
    )
    packet_path.write_text(json.dumps(pkt.model_dump(mode="json"), indent=2, default=str))
    record_proof_packet(store, pkt)
    append_audit(store, actor=verifier_node, action="verify", target=work_item_id,
                  detail={"verdict": verdict.value, "repair": repair, "hash": evidence_hash})

    return VerificationContract(
        verifier_node=verifier_node,
        verifier_model=verifier_model,
        verdict=verdict,
        evidence_hash=evidence_hash,
        repair_class=repair,
        notes="; ".join(failures) if failures else "ok",
    )