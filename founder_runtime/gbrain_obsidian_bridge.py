"""GBrain-Obsidian Bridge — merges gbrain recall into the Memory OS.

This module provides the unified memory surface. Per the v10 plan and
the base capsule doctrine:
- Obsidian is the SOLE canonical knowledge writer
- GBrain is the recall/search layer (read-mostly)
- Memory OS (founder_runtime) is the operational memory (sessions, patterns, pushback)

The bridge:
1. Reads episodic/semantic data from the goal-loop-memory SQLite DB
2. Reads gbrain.db events (if present)
3. Writes insights to Obsidian daily notes / pattern reports
4. Exposes a unified search across all three stores
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class UnifiedMemoryEntry:
    source: str  # "goal_loop", "gbrain", "obsidian"
    layer: str  # "episodic", "semantic", "procedural", "identity"
    key: str
    value: str
    timestamp: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class GBrainObsidianBridge:
    """Unified memory surface merging gbrain, goal-loop-memory, and Obsidian.

    Per doctrine: Obsidian is the canonical writer. This bridge READS
    from gbrain and goal-loop-memory, and WRITES insights to Obsidian.
    It never writes directly to gbrain (gbrain syncs FROM Obsidian).
    """

    def __init__(
        self,
        goal_loop_db: str = "~/.rig/state/goal-loop-memory.db",
        gbrain_db: str = "~/.rig/gbrain.db",
        obsidian_vault: str = "~/Documents/JakeStudio",
    ) -> None:
        self.goal_loop_db = Path(goal_loop_db).expanduser()
        self.gbrain_db = Path(gbrain_db).expanduser()
        self.obsidian_vault = Path(obsidian_vault).expanduser()

    def search_all(self, query: str, limit: int = 20) -> list[UnifiedMemoryEntry]:
        """Search across all memory stores for a query."""
        results: list[UnifiedMemoryEntry] = []

        # 1. Goal-loop-memory (SQLite FTS or LIKE)
        results.extend(self._search_goal_loop(query, limit))

        # 2. GBrain DB (if it has data)
        results.extend(self._search_gbrain(query, limit))

        # 3. Obsidian (grep-style search)
        results.extend(self._search_obsidian(query, limit))

        # Sort by relevance/timestamp and limit
        return results[:limit]

    def _search_goal_loop(self, query: str, limit: int) -> list[UnifiedMemoryEntry]:
        """Search the goal-loop-memory.db."""
        results: list[UnifiedMemoryEntry] = []
        if not self.goal_loop_db.exists():
            return results

        try:
            conn = sqlite3.connect(str(self.goal_loop_db))
            conn.row_factory = sqlite3.Row
            q = f"%{query}%"
            for row in conn.execute(
                """SELECT * FROM memories WHERE key LIKE ? OR value LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (q, q, limit),
            ).fetchall():
                results.append(
                    UnifiedMemoryEntry(
                        source="goal_loop",
                        layer=row["layer"],
                        key=row["key"],
                        value=row["value"],
                    )
                )
            # Also search learning_records
            for row in conn.execute(
                """SELECT * FROM learning_records
                   WHERE what_worked LIKE ? OR what_failed LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (q, q, limit),
            ).fetchall():
                results.append(
                    UnifiedMemoryEntry(
                        source="goal_loop",
                        layer="episodic",
                        key=f"learning:{row['goal_id']}",
                        value=row.get("what_worked") or row.get("what_failed") or "",
                        metadata={"run_id": row.get("run_id")},
                    )
                )
            conn.close()
        except Exception:
            pass
        return results

    def _search_gbrain(self, query: str, limit: int) -> list[UnifiedMemoryEntry]:
        """Search gbrain.db (read-only)."""
        results: list[UnifiedMemoryEntry] = []
        if not self.gbrain_db.exists():
            return results

        try:
            conn = sqlite3.connect(str(self.gbrain_db))
            conn.row_factory = sqlite3.Row
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            if "memories" in tables:
                q = f"%{query}%"
                for row in conn.execute(
                    "SELECT * FROM memories WHERE key LIKE ? OR value LIKE ? LIMIT ?",
                    (q, q, limit),
                ).fetchall():
                    results.append(
                        UnifiedMemoryEntry(
                            source="gbrain",
                            layer=row["layer"] if "layer" in row.keys() else "semantic",
                            key=row["key"] if "key" in row.keys() else "",
                            value=row["value"] if "value" in row.keys() else str(dict(row)),
                        )
                    )
            conn.close()
        except Exception:
            pass
        return results

    def _search_obsidian(self, query: str, limit: int) -> list[UnifiedMemoryEntry]:
        """Search Obsidian vault (simple text search)."""
        results: list[UnifiedMemoryEntry] = []
        if not self.obsidian_vault.exists():
            return results

        query_lower = query.lower()
        count = 0
        for md_file in self.obsidian_vault.rglob("*.md"):
            if count >= limit:
                break
            try:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
                if query_lower in text.lower():
                    # Extract a snippet around the match
                    idx = text.lower().find(query_lower)
                    snippet = text[max(0, idx - 50) : idx + 200].strip()
                    results.append(
                        UnifiedMemoryEntry(
                            source="obsidian",
                            layer="semantic",
                            key=str(md_file.relative_to(self.obsidian_vault)),
                            value=snippet[:300],
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                    )
                    count += 1
            except Exception:
                continue
        return results

    def write_obsidian_insight(self, title: str, content: str, folder: str = "Memory") -> str:
        """Write an insight to Obsidian (canonical writer).

        Returns the path of the written file.
        """
        target_dir = self.obsidian_vault / folder
        target_dir.mkdir(parents=True, exist_ok=True)

        # Slugify title
        slug = title.lower().replace(" ", "-").replace("/", "-")[:60]
        timestamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        filename = f"{slug} - {timestamp}.md"
        filepath = target_dir / filename

        frontmatter = f"""---
title: {title}
created: {datetime.now(timezone.utc).isoformat()}
source: memory-os-bridge
---

"""
        filepath.write_text(frontmatter + content, encoding="utf-8")
        return str(filepath)

    def status(self) -> dict[str, Any]:
        """Return the status of all memory stores."""
        status: dict[str, Any] = {}

        # Goal-loop-memory
        if self.goal_loop_db.exists():
            try:
                conn = sqlite3.connect(str(self.goal_loop_db))
                for table in ["memories", "learning_records", "proof_packets"]:
                    count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    status[f"goal_loop_{table}"] = count
                conn.close()
            except Exception as e:
                status["goal_loop_error"] = str(e)
        else:
            status["goal_loop_db"] = "missing"

        # GBrain
        if self.gbrain_db.exists():
            try:
                conn = sqlite3.connect(str(self.gbrain_db))
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                ]
                for t in tables:
                    count = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                    status[f"gbrain_{t}"] = count
                conn.close()
            except Exception:
                status["gbrain_db"] = "exists (no tables)"
        else:
            status["gbrain_db"] = "missing"

        # Obsidian
        if self.obsidian_vault.exists():
            md_files = list(self.obsidian_vault.rglob("*.md"))
            status["obsidian_notes"] = len(md_files)
        else:
            status["obsidian_vault"] = "missing"

        return status
