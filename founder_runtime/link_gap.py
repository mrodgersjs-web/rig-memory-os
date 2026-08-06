"""Link-gap orphan discovery (READ-ONLY). Never writes to vault or db."""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".rig" / "state" / "goal-loop-memory.db"
OUT_PATH = DB_PATH.parent / "link-gap-candidates.json"


def load_notes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load obsidian-note semantic memories into note dicts. Skips malformed rows."""
    notes: list[dict[str, Any]] = []
    cur = conn.execute(
        "SELECT key, value FROM memories WHERE layer='semantic' AND key LIKE 'obsidian-note:%'"
    )
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for key, value in rows:
            try:
                data = json.loads(value)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            slug = key[len("obsidian-note:") :]
            notes.append(
                {
                    "slug": slug,
                    "folder": data.get("folder"),
                    "tags": set(data.get("tags", []) or []),
                    "links": set(data.get("links", []) or []),
                    "title": data.get("title", ""),
                }
            )
    return notes


def tag_pair_cooccurrence(
    notes: list[dict[str, Any]], min_count: int = 5
) -> Counter:
    """Count distinct notes per sorted unordered tag pair; filter by min_count."""
    counter: Counter = Counter()
    for note in notes:
        tags = sorted(note["tags"])
        for pair in itertools.combinations(tags, 2):
            counter[pair] += 1
    return Counter({pair: count for pair, count in counter.items() if count >= min_count})


def gap_detection(
    notes: list[dict[str, Any]],
    pairs: Counter,
    top_pairs: int = 50,
    max_examples: int = 3,
) -> list[dict[str, Any]]:
    """For the top co-occurring tag pairs, find note pairs that never cite each other."""
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        for tag in note["tags"]:
            by_tag.setdefault(tag, []).append(note)

    results: list[dict[str, Any]] = []
    for (tag_a, tag_b), cooccurrence in pairs.most_common(top_pairs):
        examples: list[dict[str, Any]] = []
        notes_a = by_tag.get(tag_a, [])[:25]
        notes_b = by_tag.get(tag_b, [])
        for note_a in notes_a:
            if len(examples) >= max_examples:
                break
            for note_b in notes_b:
                if note_a is note_b:
                    continue
                if (
                    note_b["title"] not in note_a["links"]
                    and note_a["title"] not in note_b["links"]
                    and note_a["folder"] != note_b["folder"]
                ):
                    examples.append(
                        {
                            "note_a": note_a["slug"],
                            "note_b": note_b["slug"],
                            "folder_a": note_a["folder"],
                            "folder_b": note_b["folder"],
                        }
                    )
                    if len(examples) >= max_examples:
                        break
        results.append(
            {
                "tag_a": tag_a,
                "tag_b": tag_b,
                "cooccurrence": cooccurrence,
                "gap_examples": examples,
            }
        )
    return results


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically via a per-PID tmp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.{os.getpid()}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def main() -> None:
    """Run the read-only link-gap pipeline and write candidates atomically."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        notes = load_notes(conn)
    finally:
        conn.close()

    pairs = tag_pair_cooccurrence(notes)
    gaps = gap_detection(notes, pairs)

    output: list[dict[str, Any]] = []
    for gap in gaps:
        tag_a = gap["tag_a"]
        tag_b = gap["tag_b"]
        cooccurrence = gap["cooccurrence"]
        output.append(
            {
                "tag_a": tag_a,
                "tag_b": tag_b,
                "cooccurrence": cooccurrence,
                "gap_examples": gap["gap_examples"],
                "hypothesis": (
                    f"{tag_a} and {tag_b} co-occur in {cooccurrence} notes "
                    "but these clusters never cite each other"
                ),
            }
        )

    _atomic_write_json(OUT_PATH, output)

    header = f"{'tag_a':<24} {'tag_b':<24} {'cooccurrence':>12} {'examples':>10}"
    print(header)
    print("-" * len(header))
    for row in output[:10]:
        print(
            f"{row['tag_a']:<24} {row['tag_b']:<24} "
            f"{row['cooccurrence']:>12} {len(row['gap_examples']):>10}"
        )


if __name__ == "__main__":
    main()
