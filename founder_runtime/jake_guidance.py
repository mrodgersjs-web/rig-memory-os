#!/usr/bin/env python3
"""
Jake Guidance — Jake's live advisory capability.

Aggregates everything Jake knows RIGHT NOW into one guidance object:
  - open swarm predictions (p_true + exact Beta base rate)
  - recent resolutions + running accuracy + sealed surprisal scores
  - persona calibration state (who to trust, current grades)
  - active anti-pattern warnings from the latest live report
  - Jake's synthesized advice (deterministic rules over the above)

Surfaces:
  MCP tool `memory_get_guidance` — any agent, mid-session
  Morning brief to Obsidian    — daily via cron
  CLI                          — python -m founder_runtime.jake_guidance
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
STATE = Path.home() / ".rig" / "state"
BRIDGE_STATE = STATE / "prediction-bridge.json"
STALENESS_PATH = STATE / "guidance-staleness.json"
STUDIO_DB = Path("/Users/rig128gb/Developer/RIGForge/repos/rig-prediction-studio-pro/data/brier_calibration.db")


def _text_hash(text: str) -> str:
    """Stable short hash for an advice line (content identity)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load_staleness() -> dict[str, int]:
    """Load persisted {text_hash: cycles_seen}. Empty on miss/corrupt."""
    if not STALENESS_PATH.exists():
        return {}
    try:
        data = json.loads(STALENESS_PATH.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _atomic_write_staleness(payload: dict[str, int]) -> None:
    """Persist staleness map via per-PID tmp + replace."""
    STALENESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STALENESS_PATH.with_name(
        STALENESS_PATH.stem + f"-{os.getpid()}.tmp"
    )
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(STALENESS_PATH)


def _apply_staleness(lines: list[str]) -> tuple[list[str], dict[str, int]]:
    """Increment per-line cycle counts; flag/rewrite at 3+ identical shows.

    Single source for guidance fatigue: get_guidance() (and any future
    emit_advice caller) routes advice strings through here once.

    Returns (possibly rewritten lines, {text_hash: cycles_seen}).
    """
    prev = _load_staleness()
    next_map: dict[str, int] = {}
    out: list[str] = []
    for line in lines:
        # Strip prior STALE wrapper so the same underlying advice keeps one hash.
        canonical = line
        if canonical.startswith("STALE — still seeing ") and canonical.endswith(
            ", consider addressing"
        ):
            inner = canonical[len("STALE — still seeing ") :]
            marker = " for the "
            idx = inner.rfind(marker)
            if idx > 0:
                canonical = inner[:idx]
        h = _text_hash(canonical)
        cycles = prev.get(h, 0) + 1
        next_map[h] = cycles
        if cycles >= 3:
            if cycles % 100 in (11, 12, 13):
                ord_s = f"{cycles}th"
            elif cycles % 10 == 1:
                ord_s = f"{cycles}st"
            elif cycles % 10 == 2:
                ord_s = f"{cycles}nd"
            elif cycles % 10 == 3:
                ord_s = f"{cycles}rd"
            else:
                ord_s = f"{cycles}th"
            out.append(
                f"STALE — still seeing {canonical} for the {ord_s} cycle, "
                f"consider addressing"
            )
        else:
            out.append(canonical)
    _atomic_write_staleness(next_map)
    return out, next_map

def _latest_report() -> dict:
    files = sorted(STATE.glob("jake-live-predictions-*.json"))
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return {}


def _resolution_stats(limit: int = 50) -> dict:
    """Running accuracy + recent outcomes from the studio db.
    Reports BOTH the recent window and all-time — regime shifts live
    in the gap between them."""
    if not STUDIO_DB.exists():
        return {"n": 0}
    conn = sqlite3.connect(str(STUDIO_DB))
    conn.row_factory = sqlite3.Row
    try:
        all_rows = conn.execute(
            """
            SELECT p.p_true, b.actual_outcome, p.created_at
              FROM predictions p
              JOIN (SELECT DISTINCT prediction_id, actual_outcome
                      FROM brier_scores WHERE actual_outcome IS NOT NULL) b
                ON p.prediction_id = b.prediction_id
             ORDER BY p.created_at DESC
            """,
        ).fetchall()
        if not all_rows:
            return {"n": 0}

        def window(rows):
            n = len(rows)
            if not n:
                return {"n": 0}
            correct = sum(1 for r in rows if (r["p_true"] > 0.5) == bool(r["actual_outcome"]))
            brier = sum((r["p_true"] - r["actual_outcome"]) ** 2 for r in rows) / n
            outcome_rate = sum(int(r["actual_outcome"]) for r in rows) / n
            return {"n": n, "accuracy": round(correct / n, 3),
                    "mean_brier": round(brier, 4),
                    "outcome_rate": round(outcome_rate, 3)}

        recent = window(all_rows[:limit])
        full = window(all_rows)
        return {
            "n": full["n"],
            "accuracy": full["accuracy"],
            "mean_brier": full["mean_brier"],
            "recent": recent,
            "regime_note": (
                f"recent {recent['n']}: outcome=1 rate {recent.get('outcome_rate', 0):.0%} "
                f"vs all-time {full['outcome_rate']:.0%}"
                if recent.get("n") else ""
            ),
        }
    finally:
        conn.close()


def _advice(open_preds: list[dict], stats: dict, pushbacks: list[dict]) -> list[str]:
    """Jake's deterministic advice rules. Every line cites its evidence."""
    out = []
    n = stats.get("n", 0)
    recent = stats.get("recent", {})
    if n >= 10:
        acc = stats["accuracy"]
        r_acc = recent.get("accuracy", acc)
        r_n = recent.get("n", 0)
        if r_n >= 10 and r_acc < 0.4:
            out.append(
                f"ANTI-CALIBRATED right now: {r_acc:.0%} over the last {r_n} resolutions "
                f"({stats.get('regime_note','')}). Fading my own lean — the base rate is the "
                f"better guide until the ensemble re-fits this regime."
            )
        elif acc < 0.55:
            out.append(
                f"Forecast accuracy {acc:.0%} over {n} resolutions — below useful. "
                f"Treat leans as coin-flips; watch the base rate instead."
            )
        elif acc >= 0.7:
            out.append(
                f"Calibration is working: {acc:.0%} over {n} resolutions. "
                f"My lean is worth acting on."
            )
    high = [p for p in open_preds if p.get("p_true", 0) > 0.6]
    if high:
        out.append(
            f"{len(high)} live session(s) predicted likely to end test-less "
            f"(p>0.6). Cheapest quality move right now: write one test in each."
        )
    blocking = [pb for pb in pushbacks if pb.get("escalation") == "blocking"]
    if blocking:
        out.append(
            f"{len(blocking)} session(s) at BLOCKING escalation for scope drift. "
            f"Rule: finish the current file before opening a new one."
        )
    if not out:
        out.append("No active warnings. Keep the read:edit ratio honest — read before you edit.")
    return out


def get_guidance() -> dict:
    """Jake's current guidance — the full advisory object."""
    bridge = {}
    if BRIDGE_STATE.exists():
        try:
            bridge = json.loads(BRIDGE_STATE.read_text())
        except Exception:
            pass

    open_preds = [
        {"question": q, "p_true": r.get("p_true"),
         "p_base_rate": r.get("p_base_rate"),
         "age_min": round((time.time() - r.get("created", time.time())) / 60)}
        for q, r in bridge.get("open", {}).items()
    ]
    open_preds.sort(key=lambda p: -(p.get("p_true") or 0))

    stats = _resolution_stats()
    report = _latest_report()
    pushbacks = [
        pb for s in report.get("sessions", []) for pb in s.get("pushbacks", [])
    ]

    # Mega-harness state (council-derived 16-capability detector registry)
    harness = {}
    harness_path = STATE / "jake-harness.json"
    if harness_path.exists():
        try:
            harness = json.loads(harness_path.read_text())
        except Exception:
            pass
    harness_interventions = harness.get("interventions", [])
    blocking = [i for i in harness_interventions if i.get("severity") == "blocking"]

    advice_lines, staleness = _apply_staleness(
        _advice(open_preds, stats, pushbacks)
    )

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "jake_advice": advice_lines,
        "staleness": staleness,
        "open_predictions": open_preds,
        "track_record": stats,
        "active_warnings": len(pushbacks),
        "harness": {
            "capabilities_loaded": harness.get("capabilities_loaded", 0),
            "interventions_active": len(harness_interventions),
            "blocking_count": len(blocking),
            "blocking": [
                {"id": i["id"], "domain": i["domain"], "detail": i["detail"],
                 "intervention": i["intervention"]}
                for i in blocking[:5]
            ],
        },
        "model": {
            "transitions": report.get("model_transitions_loaded"),
            "sessions_in_window": report.get("active_sessions"),
        },
    }


def write_morning_brief(guidance: dict) -> str:
    """Write Jake's guidance to Obsidian as the morning brief."""
    lines = [
        "# Jake — Guidance Brief",
        "",
        f"_{guidance['generated_at']}_",
        "",
        "## What I think you should do",
        "",
    ]
    for a in guidance["jake_advice"]:
        lines.append(f"- {a}")
    lines += [
        "",
        f"## Track record: {guidance['track_record'].get('n', 0)} resolutions, "
        f"accuracy {guidance['track_record'].get('accuracy', '—')}, "
        f"Brier {guidance['track_record'].get('mean_brier', '—')}",
        "",
        "## Open predictions",
        "",
    ]
    for p in guidance["open_predictions"][:10]:
        base = f" (base rate {p['p_base_rate']:.2f})" if p.get("p_base_rate") else ""
        lines.append(f"- **{p['question']}** — p={p['p_true']}{base}, open {p['age_min']}min")
    return "\n".join(lines)


def main() -> int:
    g = get_guidance()
    if "--brief" in sys.argv:
        print(write_morning_brief(g))
    else:
        print(json.dumps(g, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
