"""Stigmergic decay-survival discovery over the persisted transition model."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

MODEL_PATH = Path.home() / ".rig" / "state" / "predictor-transitions.json"
EDGES_PATH = Path.home() / ".rig" / "state" / "stigmergy-edges.json"
CANDIDATES_PATH = Path.home() / ".rig" / "state" / "stigmergy-candidates.json"

FLOOR = 0.05
SURVIVE_CYCLES = 12
PERIODIC_RATIO = 0.8


def _atomic_write_json(path: Path, data) -> None:
    """Write ``data`` as JSON to ``path`` atomically via a per-PID tmp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + f"-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_edges() -> dict:
    """Load the persisted edge map, restoring project sets from sorted lists."""
    if not EDGES_PATH.exists():
        return {}

    raw = json.loads(EDGES_PATH.read_text())
    edges: dict = {}
    for edge_id, edge in raw.items():
        if edge_id == "_meta":
            edges["_meta"] = edge
            continue
        edges[edge_id] = {
            "weight_fast": edge.get("weight_fast", 0.0),
            "weight_slow": edge.get("weight_slow", 0.0),
            "projects": set(edge.get("projects", [])),
            "seen": edge.get("seen", 0),
            "survival_streak": edge.get("survival_streak", 0),
        }
    return edges


def save_edges(edges: dict) -> None:
    """Persist the edge map, serializing project sets as sorted lists."""
    serializable: dict = {}
    for edge_id, edge in edges.items():
        if edge_id == "_meta":
            serializable[edge_id] = edge
            continue
        serializable[edge_id] = {
            "weight_fast": edge["weight_fast"],
            "weight_slow": edge["weight_slow"],
            "projects": sorted(edge["projects"]),
            "seen": edge["seen"],
            "survival_streak": edge["survival_streak"],
        }
    _atomic_write_json(EDGES_PATH, serializable)


def run_cycle(decay_fast: float = 0.8, decay_slow: float = 0.98) -> dict:
    """Decay existing edges, absorb new transitions, and update survival streaks."""
    edges = load_edges()
    meta = edges.setdefault("_meta", {"cycles": 0})

    for edge_id, edge in edges.items():
        if edge_id == "_meta":
            continue
        edge["weight_fast"] *= decay_fast
        edge["weight_slow"] *= decay_slow

    if MODEL_PATH.exists():
        data = json.loads(MODEL_PATH.read_text())
    else:
        data = {"transitions": []}

    incoming = defaultdict(lambda: {"count": 0.0, "project": None})
    for row in data.get("transitions", []):
        harness = row.get("harness")
        stage = row.get("stage")
        project = row.get("project")
        state = row.get("state")
        event_type = row.get("event_type")
        # Identifying tuple for this transition row; edge derivation below
        # only needs project/state/next_state, but the full key documents
        # the source granularity of the model.
        _key = (harness, stage, project, state, event_type)

        for next_state, count in row.get("next", {}).items():
            edge_id = f"{project}|{state}|{next_state}"
            agg = incoming[edge_id]
            agg["count"] += float(count)
            agg["project"] = project

    for edge_id, agg in incoming.items():
        edge = edges.get(edge_id)
        if edge is None:
            edge = {
                "weight_fast": 0.0,
                "weight_slow": 0.0,
                "projects": set(),
                "seen": 0,
                "survival_streak": 0,
            }
            edges[edge_id] = edge
        edge["weight_fast"] += agg["count"]
        edge["weight_slow"] += agg["count"]
        edge["projects"].add(agg["project"])
        edge["seen"] += int(agg["count"])

    for edge_id, edge in edges.items():
        if edge_id == "_meta":
            continue
        edge["survival_streak"] = (
            edge["survival_streak"] + 1 if edge["weight_slow"] > FLOOR else 0
        )

    meta["cycles"] += 1

    save_edges(edges)
    return edges


def find_candidates(edges: dict) -> list:
    """Select surviving, non-noisy, non-periodic edges as discovery candidates."""
    cycles = edges.get("_meta", {}).get("cycles", 1) or 1
    candidates: list = []

    for edge_id, edge in edges.items():
        if edge_id == "_meta":
            continue

        survival_streak = edge["survival_streak"]
        seen = edge["seen"]
        weight_fast = edge["weight_fast"]
        weight_slow = edge["weight_slow"]
        projects = edge["projects"]

        is_candidate = (
            survival_streak >= SURVIVE_CYCLES
            and (seen / max(1, survival_streak)) < 3
            and (seen / max(1, cycles)) <= PERIODIC_RATIO
        )
        if not is_candidate:
            continue

        sleeper = weight_slow > 10 * weight_fast and weight_fast < 1.0

        candidates.append(
            {
                "edge": edge_id,
                "projects": set(projects),
                "survival_streak": survival_streak,
                "seen": seen,
                "weight_fast": weight_fast,
                "weight_slow": weight_slow,
                "sleeper": sleeper,
            }
        )

    candidates.sort(key=lambda c: (len(c["projects"]), c["survival_streak"]), reverse=True)
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stigmergic decay-survival discovery over the persisted transition model."
    )
    parser.add_argument("--cycles", type=int, default=1)
    args = parser.parse_args()

    edges: dict = {}
    for _ in range(args.cycles):
        edges = run_cycle()

    if not edges:
        edges = load_edges()

    candidates = find_candidates(edges)

    output = [
        {
            "edge": c["edge"],
            "projects": sorted(c["projects"]),
            "survival_streak": c["survival_streak"],
            "seen": c["seen"],
            "weight_fast": c["weight_fast"],
            "weight_slow": c["weight_slow"],
            "sleeper": c["sleeper"],
        }
        for c in candidates
    ]
    _atomic_write_json(CANDIDATES_PATH, output)

    total_edges = len([k for k in edges if k != "_meta"])
    cycles_tracked = edges.get("_meta", {}).get("cycles", 0)
    top3 = candidates[:3]
    top3_str = ", ".join(c["edge"] for c in top3) if top3 else "none"

    print(f"Total edges: {total_edges}")
    print(f"Cycles tracked: {cycles_tracked}")
    print(f"Candidates found: {len(candidates)}")
    print("Top 3 by streak:")
    print(top3_str)


if __name__ == "__main__":
    main()
