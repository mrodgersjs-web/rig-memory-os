#!/usr/bin/env python3
"""
Jake Live Prediction Report — v2 (categorical states + history backfill).

Runs Jake's observer + predictor over currently-active OMP/Claude coding
sessions and produces predictions with REAL learned probabilities.

How it works:
1. BACKFILL: parse the last N days of chat transcripts, classify every
   tool call into a categorical phase (read/edit/test/bash/delegate/search),
   and record phase_i -> phase_{i+1} transitions into the Predictor.
2. PREDICT: for each session active in the last 6h, take its current
   (last) phase and predict the next phase using the learned model.
3. OBSERVE: run each active session through JakeObserver for pushbacks.
4. Persist the report + all prediction IDs for later resolution.

Usage:
  PYTHONPATH=. RIG_MEMORY_OS_SECRET=test-universal-secret \
    .venv/bin/python -m founder_runtime.jake_live_report [--days 7]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from founder_runtime.runtime import MemoryOSRuntime

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
ACTIVE_WINDOW_SEC = 6 * 3600

TEST_RE = re.compile(r"pytest|npm test|bun test|vitest|cargo test|go test|\bmake test\b")
WRITE_RE = re.compile(r"(^|[^<])>(?!>)|tee\s|cat\s*>|sed\s+-i|apply_patch|python3?\s+-c.*open\(.*['\"]w")


def classify_tool(name: str, inp: dict) -> str:
    """Map a tool call to a categorical phase."""
    if name in ("Read", "Grep", "Glob"):
        return "read"
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "edit"
    if name in ("Task", "Agent"):
        return "delegate"
    if name in ("WebSearch", "WebFetch"):
        return "search"
    if name == "Bash":
        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
        if TEST_RE.search(cmd):
            return "test"
        if WRITE_RE.search(cmd):
            return "edit"  # bash-driven file writes ARE edits
        return "bash"
    if name in ("TodoWrite", "TodoRead"):
        return "plan"
    return "other"


def parse_session(jsonl_path: Path) -> dict:
    """Parse a transcript into a phase timeline + signal counts."""
    phases = []          # ordered categorical phases
    files_modified = set()
    test_runs = 0
    abstractions = 0
    user_msgs = 0
    first_ts = last_ts = None

    try:
        for line in jsonl_path.open(errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            t = d.get("type")
            if t == "user":
                # skip tool_result-only user turns
                content = d.get("message", {}).get("content")
                if isinstance(content, str) or any(
                    isinstance(c, dict) and c.get("type") == "text" for c in (content or [])
                ):
                    user_msgs += 1
                    phases.append(("prompt", ts))
                continue
            if t != "assistant":
                continue
            for c in (d.get("message", {}).get("content") or []):
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name = c.get("name", "?")
                inp = c.get("input") or {}
                phase = classify_tool(name, inp)
                phases.append((phase, ts))
                if phase == "edit":
                    fp = inp.get("file_path") or inp.get("path") or ""
                    if fp:
                        files_modified.add(fp)
                    elif name == "Bash":
                        files_modified.add(f"bash:{inp.get('command','')[:40]}")
                    content = inp.get("content") or inp.get("new_string") or ""
                    abstractions += len(re.findall(r"\bclass\s+\w+|\binterface\s+\w+", content))
                if phase == "test":
                    test_runs += 1
    except Exception:
        pass

    duration_min = 0.0
    if first_ts and last_ts:
        try:
            from datetime import datetime
            f = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            l = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_min = max(0.0, (l - f).total_seconds() / 60)
        except Exception:
            pass

    return {
        "session_id": jsonl_path.stem,
        "project": jsonl_path.parent.name,
        "phases": phases,
        "files_modified": sorted(files_modified),
        "test_runs": test_runs,
        "abstractions": abstractions,
        "user_msgs": user_msgs,
        "duration_min": round(duration_min, 1),
        "last_ts": last_ts,
        "mtime": jsonl_path.stat().st_mtime if jsonl_path.exists() else 0,
    }


def backfill_transitions(rt, sessions: list[dict]) -> int:
    """Record phase transitions from all parsed sessions into the Predictor."""
    n = 0
    for s in sessions:
        seq = [p for p, _ in s["phases"]]
        for a, b in zip(seq, seq[1:]):
            rt.record_transition(
                current_state=a,
                event_type="phase_advance",
                next_state=b,
                harness="omp",
                stage="coding",
                project=s["project"],
            )
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="DEPRECATED: backfill now lives in ingest_history.py; "
                         "this flag only bounds which sessions count as parsed")
    ap.add_argument("--backfill", action="store_true",
                    help="Also record transitions (one-off; model persists to disk)")
    args = ap.parse_args()

    rt = MemoryOSRuntime.from_env()  # loads persisted transition model
    try:
        now = time.time()

        # ---- Pass 1: find active sessions only ----
        active_sessions = []
        for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
            if "subagents" in p.parts:
                continue
            try:
                if now - p.stat().st_mtime > ACTIVE_WINDOW_SEC:
                    continue
            except OSError:
                continue
            sig = parse_session(p)
            if sig["phases"]:
                active_sessions.append(sig)

        active_sessions.sort(key=lambda s: s["last_ts"] or "", reverse=True)
        n_model = sum(sum(v.values()) for v in rt.predictor._transitions.values())
        print(f"Loaded {n_model} persisted transitions; "
              f"{len(active_sessions)} sessions active in last 6h")

        # Optional explicit re-backfill (normally NOT needed — ingest_history owns this)
        n_trans = 0
        if args.backfill:
            backfill_cutoff = now - args.days * 86400
            all_sessions = []
            for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
                if "subagents" in p.parts:
                    continue
                try:
                    if p.stat().st_mtime < backfill_cutoff:
                        continue
                except OSError:
                    continue
                sig = parse_session(p)
                if sig["phases"]:
                    all_sessions.append(sig)
            n_trans = backfill_transitions(rt, all_sessions)
            print(f"Re-recorded {n_trans} transitions (--backfill)")

        # ---- Pass 3: observe + predict on active sessions ----
        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model_transitions_loaded": n_model,
            "transitions_recorded_this_run": n_trans,
            "active_sessions": len(active_sessions),
            "sessions": [],
            "predictions": [],
        }

        phase_counts = Counter()
        for s in active_sessions:
            seq = [p for p, _ in s["phases"]]
            phase_counts.update(seq)

            obs = rt.observe_session(
                tool_calls=seq,
                files_modified=s["files_modified"],
                time_spent=s["duration_min"],
                goals=[s["project"]],
                tests_written=s["test_runs"],
                abstractions_created=s["abstractions"],
            )

            last_phase = seq[-1] if seq else "unknown"
            pred = rt.predict_next(
                current_state=last_phase,
                event_type="phase_advance",
                harness="omp",
                stage="coding",
                project=s["project"],
            )

            report["sessions"].append({
                "project": s["project"].lstrip("-Users-rig128gb") or "(home)",
                "session_id": s["session_id"][:12],
                "duration_min": s["duration_min"],
                "phases": len(seq),
                "last_phase": last_phase,
                "files_touched": len(s["files_modified"]),
                "test_runs": s["test_runs"],
                "pushbacks": obs["pushbacks"],
            })
            report["predictions"].append({
                "for_project": s["project"].lstrip("-Users-rig128gb") or "(home)",
                "current_phase": last_phase,
                "predicted_next_phase": pred["predicted_state"],
                "probability": pred["probability"],
                "prediction_id": pred["prediction_id"],
                "expires_at": pred["expires_at"],
            })

        report["phase_histogram"] = dict(phase_counts.most_common())

        out = Path.home() / ".rig" / "state" / f"jake-live-predictions-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nReport written: {out}\n")
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        rt.close()


if __name__ == "__main__":
    sys.exit(main())
