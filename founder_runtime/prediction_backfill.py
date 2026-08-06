#!/usr/bin/env python3
"""
Prediction Studio Backfill — bootstrap the Brier calibration loop with
historical sessions whose outcomes are already known.

For each historical session (oldest -> newest), generate the testless
question, run the 12-persona swarm, then immediately resolve with the
known outcome. Personas accumulate real Brier history; the ensemble's
calibration report becomes real data instead of an empty room.

Honesty note: deterministic persona votes cluster near base rates, so
weights barely separate on question type alone. What this backfill really
buys: (1) a real resolved-outcome store, (2) a real calibration table,
(3) the resolution loop proven end-to-end at scale.

Usage:
  PYTHONPATH=. .venv/bin/python -m founder_runtime.prediction_backfill [--n 300]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/Users/rig128gb/Developer/RIGForge/repos/rig-prediction-studio-pro")

from runner.studio_v2 import PredictionStudioV2
from founder_runtime.jake_live_report import parse_session, CLAUDE_PROJECTS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300, help="max sessions to backfill")
    args = ap.parse_args()

    studio = PredictionStudioV2()

    sessions = []
    for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            sig = parse_session(p)
        except Exception:
            continue
        if len(sig["phases"]) >= 4 and sig["duration_min"] >= 2:
            sessions.append(sig)

    # chronological so persona weights evolve in time order
    sessions.sort(key=lambda s: s["last_ts"] or "")
    sessions = sessions[: args.n]

    made = resolved = 0
    t0 = time.time()
    correct = 0

    for sig in sessions:
        sid = sig["session_id"][:12]
        tests = sig["test_runs"]
        files = len(sig["files_modified"])
        edits = sum(1 for ph, _ in sig["phases"] if ph == "edit")
        q = (f"Session {sid} ends without a test run. Context: coding session, "
             f"{sig['duration_min']}min, {len(sig['phases'])} tool calls, "
             f"{files} files, {edits} edits, tests so far at mid-session unknown.")

        result = studio.predict(question=q, horizon_days=1)
        outcome = 1 if tests == 0 else 0
        try:
            studio.record_outcome(
                prediction_id=result["prediction_id"],
                outcome=outcome,
                notes="historical backfill (known outcome)",
            )
        except TypeError:
            studio.record_outcome(result["prediction_id"], outcome)

        made += 1
        resolved += 1
        if (result["p_true"] > 0.5) == bool(outcome):
            correct += 1
        if made % 50 == 0:
            print(f"  {made}/{len(sessions)} backfilled ({time.time()-t0:.0f}s), "
                  f"directional accuracy so far: {correct/made:.2f}")

    print(f"\nBackfilled {made} predictions, resolved {resolved} in {time.time()-t0:.0f}s")
    print(f"Directional accuracy (p>0.5 == outcome): {correct/max(1,made):.3f}")

    # The payoff: real calibration report
    try:
        cal = studio.calibration_report()
        print("=== CALIBRATION REPORT (post-backfill) ===")
        print(json.dumps(cal, indent=2, default=str)[:3000])
    except Exception as e:
        print(f"calibration_report error: {e}")
        try:
            from runner.cli import cli  # noqa
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
