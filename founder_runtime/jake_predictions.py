#!/usr/bin/env python3
"""
Jake Predictions v2 — multi-type predictions + walk-forward backtest.

Builds on top of `founder_runtime.jake_live_report.parse_session`:

1. INFER: map each parsed session to a coding project by mining
   `files_modified` for a `Developer/<name>` (or Documents/Desktop) path
   segment, falling back to the raw Claude project-dir name.
2. BASELINE: compute per-project duration/testless-rate baselines from
   session history.
3. BACKTEST: walk sessions in chronological order and, at three points in
   each session's life (25% / 50% / 75% of its phases), predict whether it
   will end without ever running tests using a Laplace-smoothed bucket
   model — then observe the real outcome and update the model. This is a
   strict walk-forward backtest: predictions are made with model state as
   of that point in time, never with future information.
4. LIVE: for sessions active in the last 6h, emit a live snapshot compared
   against the project's historical baseline.

Usage:
  PYTHONPATH=. .venv/bin/python -m founder_runtime.jake_predictions
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from founder_runtime.jake_live_report import parse_session, CLAUDE_PROJECTS

ACTIVE_WINDOW_SEC = 6 * 3600
FRACS: tuple[float, ...] = (0.25, 0.5, 0.75)
TOP_PROJECTS = 25

PROJECT_DIR_RE = re.compile(r"(?:Developer|Documents|Desktop)/([^/]+)")
UNSCOPED_HOME_PROJECT = "Users-rig128gb"
UNSCOPED_LABEL = "(unscoped ~)"
HOME_PROJECT_PREFIX = "Users-rig128gb-"

CALIBRATION_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.2, "0-20%"),
    (0.2, 0.4, "20-40%"),
    (0.4, 0.6, "40-60%"),
    (0.6, 0.8, "60-80%"),
    (0.8, 1.01, "80-100%"),
)


def infer_project(sig: dict) -> str:
    """Infer a human-friendly project name for a parsed session.

    Prefers the most common `Developer/<name>` (or Documents/Desktop)
    path segment seen across `files_modified`; falls back to the raw
    Claude project directory name (with the home-dir prefix stripped).
    """
    counts: Counter[str] = Counter()
    for fp in sig.get("files_modified", []) or []:
        m = PROJECT_DIR_RE.search(fp)
        if m:
            counts[m.group(1)] += 1
    if counts:
        return counts.most_common(1)[0][0]

    fallback = (sig.get("project") or "").lstrip("-")
    if fallback in (UNSCOPED_HOME_PROJECT, ""):
        return UNSCOPED_LABEL
    if fallback.startswith(HOME_PROJECT_PREFIX):
        fallback = fallback[len(HOME_PROJECT_PREFIX):]
    return fallback


def session_outcome(sig: dict) -> dict:
    """Terminal-state summary of a parsed session."""
    return {
        "ended_without_tests": sig.get("test_runs", 0) == 0,
        "duration_min": sig.get("duration_min", 0.0),
        "files": len(sig.get("files_modified", []) or []),
        "phases": len(sig.get("phases", []) or []),
    }


def prefix_signals(sig: dict, frac: float) -> dict:
    """Signals observable after `frac` of a session's phases have elapsed."""
    phases = sig.get("phases", []) or []
    n_phases = max(1, int(len(phases) * frac))
    prefix = phases[:n_phases]
    tests_so_far = sum(1 for p in prefix if p[0] == "test")
    edits_so_far = sum(1 for p in prefix if p[0] == "edit")
    last_phase = prefix[-1][0] if prefix else None
    return {
        "frac": frac,
        "n_phases": n_phases,
        "tests_so_far": tests_so_far,
        "edits_so_far": edits_so_far,
        "last_phase": last_phase,
        "elapsed_min": sig.get("duration_min", 0.0) * frac,
    }


class BinaryBucketModel:
    """Laplace-smoothed binary predictor keyed on a discretized bucket."""

    def __init__(self) -> None:
        # key -> [neg_count, pos_count]
        self._counts: dict[str, list[int]] = {}

    @staticmethod
    def bucket(project: str, tests: int, edits: int) -> str:
        if tests == 0:
            t = "0"
        elif tests <= 2:
            t = "1-2"
        else:
            t = "3+"

        if edits == 0:
            e = "0"
        elif edits <= 5:
            e = "1-5"
        else:
            e = "6+"

        return f"{project}|tests:{t}|edits:{e}"

    def observe(self, key: str, outcome: bool) -> None:
        counts = self._counts.setdefault(key, [0, 0])
        if outcome:
            counts[1] += 1
        else:
            counts[0] += 1

    def predict(self, key: str) -> tuple[float, int]:
        neg, pos = self._counts.get(key, [0, 0])
        n = neg + pos
        return ((pos + 1) / (n + 2), n)


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (numpy-default style)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def backtest(sessions: list[dict]) -> dict:
    """Walk-forward backtest of the testless-ending prediction at 3 fracs.

    For every frac in FRACS, a fresh BinaryBucketModel is trained
    incrementally as sessions are replayed in chronological order:
    predict first (using only what the model has learned so far), record
    the prediction if the bucket has enough support, then observe the
    real outcome to update the model for the next session.
    """
    qualifying = [s for s in sessions if len(s.get("phases", []) or []) >= 4]
    qualifying = sorted(qualifying, key=lambda s: s.get("last_ts") or "")

    models = {frac: BinaryBucketModel() for frac in FRACS}
    records: dict[float, list[tuple[float, bool]]] = {frac: [] for frac in FRACS}

    for sig in qualifying:
        outcome = session_outcome(sig)
        actual = outcome["ended_without_tests"]
        project = infer_project(sig)

        for frac in FRACS:
            model = models[frac]
            ps = prefix_signals(sig, frac)
            key = model.bucket(project, ps["tests_so_far"], ps["edits_so_far"])
            prob, support = model.predict(key)
            if support >= 3:
                records[frac].append((prob, actual))
            model.observe(key, actual)

    result: dict[str, dict] = {}
    for frac in FRACS:
        recs = records[frac]
        n = len(recs)

        if n:
            brier = sum((p - (1.0 if o else 0.0)) ** 2 for p, o in recs) / n
            global_rate = sum(1 for _, o in recs if o) / n
            base_brier = sum(
                (global_rate - (1.0 if o else 0.0)) ** 2 for _, o in recs
            ) / n
            skill = 1 - (brier / base_brier) if base_brier > 0 else 0.0
        else:
            brier = 0.0
            base_brier = 0.0
            skill = 0.0

        calibration = []
        for lo, hi, label in CALIBRATION_BUCKETS:
            bucket_recs = [(p, o) for p, o in recs if lo <= p < hi]
            bn = len(bucket_recs)
            mean_pred = sum(p for p, _ in bucket_recs) / bn if bn else 0.0
            actual_rate = sum(1 for _, o in bucket_recs if o) / bn if bn else 0.0
            calibration.append(
                {
                    "bucket": label,
                    "n": bn,
                    "mean_pred": round(mean_pred, 3),
                    "actual_rate": round(actual_rate, 3),
                }
            )

        result[str(frac)] = {
            "n": n,
            "brier": round(brier, 4),
            "base_rate_brier": round(base_brier, 4),
            "skill": round(skill, 4),
            "calibration": calibration,
        }

    return result


def _iter_session_files(root: Path):
    if not root.exists():
        return
    for path in root.glob("**/*.jsonl"):
        if "subagents" in path.parts:
            continue
        yield path


def _write_atomic(out_path: Path, payload: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f"{out_path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, indent=2, default=str))
    tmp_path.replace(out_path)


def main() -> int:
    start = time.time()

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--active-window-sec",
        type=int,
        default=ACTIVE_WINDOW_SEC,
        help="how recently (in seconds) a session must have been touched to count as active",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=TOP_PROJECTS,
        help="number of projects to include in the project_histogram",
    )
    ap.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".rig" / "state",
        help="directory to write the jake-predictions-v2-<ts>.json report to",
    )
    args = ap.parse_args()

    now = time.time()
    sessions: list[dict] = []
    for path in _iter_session_files(CLAUDE_PROJECTS):
        sig = parse_session(path)
        if len(sig.get("phases", []) or []) < 4:
            continue
        sig["_project"] = infer_project(sig)
        sig["_active"] = (now - sig.get("mtime", 0)) <= args.active_window_sec
        sessions.append(sig)

    sessions_total = len(sessions)
    active_sessions = [s for s in sessions if s["_active"]]
    project_histogram = Counter(s["_project"] for s in sessions)
    projects_total = len(project_histogram)

    by_project: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        by_project[s["_project"]].append(s)

    baselines: dict[str, dict] = {}
    for project, sess_list in by_project.items():
        durations = [s.get("duration_min", 0.0) for s in sess_list]
        n = len(sess_list)
        testless = sum(1 for s in sess_list if s.get("test_runs", 0) == 0)
        baselines[project] = {
            "sessions": n,
            "p50_duration": round(_percentile(durations, 0.50), 1),
            "p75_duration": round(_percentile(durations, 0.75), 1),
            "testless_rate": round(testless / n, 3) if n else 0.0,
        }

    backtest_result = backtest(sessions)

    live: list[dict] = []
    for s in active_sessions:
        project = s["_project"]
        base = baselines.get(project, {})
        live.append(
            {
                "project": project,
                "session_id": (s.get("session_id") or "")[:12],
                "elapsed_min": s.get("duration_min", 0.0),
                "phases": len(s.get("phases", []) or []),
                "tests_so_far": s.get("test_runs", 0),
                "files_touched": len(s.get("files_modified", []) or []),
                "baseline_p75_duration": base.get("p75_duration", 0.0),
                "baseline_testless_rate": base.get("testless_rate", 0.0),
            }
        )

    elapsed_s = round(time.time() - start, 2)
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    report = {
        "generated_at": generated_at,
        "sessions_total": sessions_total,
        "projects_total": projects_total,
        "active_sessions": len(active_sessions),
        "project_histogram": dict(project_histogram.most_common(args.top)),
        "baselines": baselines,
        "backtest": backtest_result,
        "live": live,
        "elapsed_s": elapsed_s,
    }

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_path = args.state_dir / f"jake-predictions-v2-{ts}.json"
    _write_atomic(out_path, report)

    print(
        f"sessions_total={sessions_total} projects_total={projects_total} "
        f"active_sessions={len(active_sessions)} -> {out_path}"
    )
    for frac in FRACS:
        bt = backtest_result[str(frac)]
        print(
            f"backtest @{frac}: n={bt['n']} brier={bt['brier']} "
            f"base={bt['base_rate_brier']} skill={bt['skill']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
