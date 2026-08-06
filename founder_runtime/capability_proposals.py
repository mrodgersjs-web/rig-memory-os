#!/usr/bin/env python3
"""Convert stigmergy survival candidates into mutation-gate proposals (capability 18).

Pipeline:
  stigmergy-candidates.json
    -> filter survival_streak >= 12
    -> MutationProposal(new_detector_rule)
    -> mutation_gate.judge(...)
    -> capability-18-pending.json  (admitted only)

CLI:
  python -m founder_runtime.capability_proposals
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from founder_runtime.mutation_gate import MutationProposal, judge

STATE = Path.home() / ".rig" / "state"
CANDIDATES_PATH = STATE / "stigmergy-candidates.json"
BRIDGE_PATH = STATE / "prediction-bridge.json"
PENDING_PATH = STATE / "capability-18-pending.json"
STUDIO_DB = Path(
    "/Users/rig128gb/Developer/RIGForge/repos/rig-prediction-studio-pro"
    "/data/brier_calibration.db"
)

SURVIVAL_THRESHOLD = 12
HELD_OUT_N = 60
DEFAULT_BRIER = 0.25


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically via a per-PID tmp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + f"-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def slug(edge: str) -> str:
    """Stable filesystem-/id-safe slug for an edge identifier."""
    s = str(edge).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "unknown"


def load_candidates() -> list[dict]:
    """Read stigmergy candidates; tolerate missing/corrupt file."""
    if not CANDIDATES_PATH.exists():
        return []
    try:
        raw = json.loads(CANDIDATES_PATH.read_text())
    except Exception:
        return []
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict)]
    if isinstance(raw, dict):
        # tolerate {candidates: [...]} wrappers
        inner = raw.get("candidates")
        if isinstance(inner, list):
            return [c for c in inner if isinstance(c, dict)]
    return []


def current_system_brier() -> float:
    """Brier from prediction-bridge.json stats if present, else DEFAULT_BRIER."""
    if BRIDGE_PATH.exists():
        try:
            bridge = json.loads(BRIDGE_PATH.read_text())
            stats = bridge.get("stats") or {}
            for key in ("mean_brier", "brier", "brier_score", "avg_brier"):
                if key in stats and stats[key] is not None:
                    return float(stats[key])
            # some writers put brier at top level
            for key in ("mean_brier", "brier", "brier_score"):
                if key in bridge and bridge[key] is not None:
                    return float(bridge[key])
        except Exception:
            pass
    # Fall back to studio-db mean Brier when bridge has no stats yet.
    try:
        if STUDIO_DB.exists():
            conn = sqlite3.connect(str(STUDIO_DB))
            try:
                row = conn.execute(
                    """
                    SELECT AVG((p.p_true - b.actual_outcome) * (p.p_true - b.actual_outcome))
                      FROM predictions p
                      JOIN (
                        SELECT DISTINCT prediction_id, actual_outcome
                          FROM brier_scores
                         WHERE actual_outcome IS NOT NULL
                      ) b ON p.prediction_id = b.prediction_id
                    """
                ).fetchone()
                if row and row[0] is not None:
                    return float(row[0])
            finally:
                conn.close()
    except Exception:
        pass
    return DEFAULT_BRIER


def load_held_out(n: int = HELD_OUT_N) -> list[dict]:
    """Last N resolutions as {predicted, actual} for the mutation gate.

    Prefer prediction-bridge.json resolutions if present; else studio db.
    """
    # 1. bridge file
    if BRIDGE_PATH.exists():
        try:
            bridge = json.loads(BRIDGE_PATH.read_text())
            res = bridge.get("resolutions") or bridge.get("held_out") or []
            if isinstance(res, list) and res:
                out: list[dict] = []
                for r in res:
                    if not isinstance(r, dict):
                        continue
                    pred = r.get("predicted", r.get("p_true", r.get("probability")))
                    actual = r.get("actual", r.get("actual_outcome", r.get("outcome")))
                    if pred is None or actual is None:
                        continue
                    out.append({"predicted": float(pred), "actual": int(actual)})
                if out:
                    return out[-n:] if len(out) > n else out
        except Exception:
            pass

    # 2. studio db
    if STUDIO_DB.exists():
        try:
            conn = sqlite3.connect(str(STUDIO_DB))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT p.p_true, b.actual_outcome
                      FROM predictions p
                      JOIN (
                        SELECT DISTINCT prediction_id, actual_outcome
                          FROM brier_scores
                         WHERE actual_outcome IS NOT NULL
                      ) b ON p.prediction_id = b.prediction_id
                     ORDER BY p.created_at DESC
                     LIMIT ?
                    """,
                    (n,),
                ).fetchall()
                return [
                    {"predicted": float(r["p_true"]), "actual": int(r["actual_outcome"])}
                    for r in rows
                ]
            finally:
                conn.close()
        except Exception:
            pass

    return []


def build_proposal(candidate: dict, system_brier: float) -> MutationProposal:
    """Build a MutationProposal for one stigmergy candidate."""
    edge = candidate.get("edge") or candidate.get("edge_id") or "unknown"
    streak = int(candidate.get("survival_streak") or 0)
    expected = max(0.05, float(system_brier) - 0.02)
    return MutationProposal(
        surface=f"detector:stigmergy-{slug(edge)}",
        change_type="new_detector_rule",
        content={
            "expected_brier": expected,
            "trigger_edge": edge,
            "survival_streak": streak,
        },
        proposer="stigmergy",
        evidence_refs=[f"stigmergy:{edge}:streak={streak}"],
    )


def proposal_record(proposal: MutationProposal, verdict: Any) -> dict:
    """Serializable admitted-proposal record for the pending queue."""
    return {
        "proposal_id": proposal.proposal_id,
        "surface": proposal.surface,
        "change_type": proposal.change_type,
        "content": proposal.content,
        "proposer": proposal.proposer,
        "evidence_refs": list(proposal.evidence_refs),
        "admitted": bool(getattr(verdict, "admitted", False)),
        "reason": getattr(verdict, "reason", ""),
        "checks": getattr(verdict, "checks", {}),
        "signature": getattr(verdict, "signature", ""),
        "decided_at": getattr(verdict, "decided_at", ""),
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def run() -> dict:
    """Convert ready stigmergy candidates into gate-admitted pending proposals."""
    candidates = load_candidates()
    ready = [
        c for c in candidates
        if int(c.get("survival_streak") or 0) >= SURVIVAL_THRESHOLD
    ]
    system_brier = current_system_brier()
    held_out = load_held_out(HELD_OUT_N)
    token = os.environ.get("JAKE_GATE_D_TOKEN")

    admitted: list[dict] = []
    rejected: list[dict] = []

    for cand in ready:
        proposal = build_proposal(cand, system_brier)
        try:
            verdict = judge(
                proposal,
                held_out=held_out,
                human_gate_d_token=token,
            )
        except Exception as e:
            rejected.append({
                "edge": cand.get("edge"),
                "error": str(e),
            })
            continue
        rec = proposal_record(proposal, verdict)
        if verdict.admitted:
            admitted.append(rec)
        else:
            rejected.append({
                "edge": cand.get("edge"),
                "proposal_id": proposal.proposal_id,
                "reason": verdict.reason,
                "checks": verdict.checks,
            })

    # Always write the pending file (empty list is a valid state).
    _atomic_write_json(PENDING_PATH, admitted)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "candidates_total": len(candidates),
        "candidates_ready": len(ready),
        "system_brier": round(system_brier, 4),
        "held_out_n": len(held_out),
        "admitted": len(admitted),
        "rejected": len(rejected),
        "pending_path": str(PENDING_PATH),
        "admitted_ids": [a["proposal_id"] for a in admitted],
        "rejected_detail": rejected[:20],
    }
    return summary


def main() -> int:
    summary = run()
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
