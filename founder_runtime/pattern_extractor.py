"""Pattern Extractor — extracts coding patterns from session data.

Phase 5 of Memory OS v10.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PatternMatch:
    pattern_name: str
    matched: bool
    confidence: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


class PatternExtractor:
    """Extracts coding patterns from session data.

    Patterns are derived from the v10 implementation plan and extended
    with data-driven detection from the Postgres store.
    """

    PATTERN_DEFINITIONS = {
        "debug_time_scaling": {
            "small_bug": {"avg_minutes": 12, "tools": ["grep", "read", "grep"]},
            "large_bug": {"avg_minutes": 47, "tools": ["grep", "read", "bash", "bash", "edit", "test"]},
        },
        "refactor_depth": {
            "shallow": {"files_touched": 1, "confidence": 0.95},
            "deep": {"files_touched": 5, "confidence": 0.6, "rollback_rate": 0.4},
        },
        "test_avoidance": {
            "trigger": "task_estimate > 25 AND time_spent > 15",
            "behavior": "skip tests when rushing",
        },
        "premature_abstraction": {
            "trigger": "abstract_before_concrete_count >= 1",
            "behavior": "extract interface after 1 implementation",
        },
        "scope_creep": {
            "trigger": "files_modified.count > goals.count * 3",
            "behavior": "changes beyond stated task",
        },
    }

    def extract_patterns(self, session_data: dict[str, Any]) -> dict[str, PatternMatch]:
        """Extract patterns from session data.

        Args:
            session_data: dict with keys like:
                - files_modified: list[str]
                - tests_written: int
                - time_spent: float (minutes)
                - goals: list[str]
                - tool_calls: list[str]
                - abstractions_created: int
                - concrete_implementations: int

        Returns:
            dict mapping pattern_name -> PatternMatch
        """
        results: dict[str, PatternMatch] = {}
        for pattern_name, pattern_def in self.PATTERN_DEFINITIONS.items():
            results[pattern_name] = self._match_pattern(session_data, pattern_name, pattern_def)
        return results

    def _match_pattern(
        self, session_data: dict[str, Any], pattern_name: str, pattern_def: dict[str, Any]
    ) -> PatternMatch:
        files = session_data.get("files_modified", [])
        tests = session_data.get("tests_written", 0)
        time_spent = session_data.get("time_spent", 0)
        goals = session_data.get("goals", [])
        tool_calls = session_data.get("tool_calls", [])
        abstractions = session_data.get("abstractions_created", 0)
        concretes = session_data.get("concrete_implementations", 0)

        if pattern_name == "test_avoidance":
            triggered = len(files) > 1 and tests == 0 and time_spent > 15
            return PatternMatch(
                pattern_name,
                triggered,
                0.9 if triggered else 0.0,
                {"files_modified": len(files), "tests_written": tests, "time_spent": time_spent},
            )

        if pattern_name == "premature_abstraction":
            triggered = abstractions > concretes
            return PatternMatch(
                pattern_name,
                triggered,
                0.85 if triggered else 0.0,
                {"abstractions": abstractions, "concretes": concretes},
            )

        if pattern_name == "scope_creep":
            # Scope creep = topically UNRELATED file changes, not "many files".
            # Cluster modified files by path prefix (project + first subdir);
            # creep = a secondary cluster with >=2 files outside the main one.
            clusters: dict[str, int] = {}
            for f in files:
                parts = [p for p in str(f).split("/") if p and p not in (".", "~")]
                key = "/".join(parts[:4]) if len(parts) >= 4 else "/".join(parts)
                clusters[key] = clusters.get(key, 0) + 1
            if len(clusters) <= 1:
                return PatternMatch(pattern_name, False, 0.0,
                                    {"clusters": list(clusters), "files": len(files)})
            counts = sorted(clusters.values(), reverse=True)
            main_cluster, drift = counts[0], sum(counts[1:])
            triggered = drift >= 2 and drift >= main_cluster * 0.34
            confidence = min(0.95, 0.6 + 0.1 * drift) if triggered else 0.0
            return PatternMatch(
                pattern_name,
                triggered,
                confidence,
                {"clusters": clusters, "main_cluster_files": main_cluster,
                 "drift_files": drift},
            )

        if pattern_name == "refactor_depth":
            is_deep = len(files) > 4
            return PatternMatch(
                pattern_name,
                is_deep,
                0.6 if is_deep else 0.95,
                {"files_touched": len(files), "classification": "deep" if is_deep else "shallow"},
            )

        if pattern_name == "debug_time_scaling":
            is_large = time_spent > 30
            return PatternMatch(
                pattern_name,
                is_large,
                0.7 if is_large else 0.3,
                {"time_spent": time_spent, "classification": "large_bug" if is_large else "small_bug"},
            )

        return PatternMatch(pattern_name, False, 0.0)

    def to_json(self, results: dict[str, PatternMatch]) -> str:
        """Serialize pattern results to JSON."""
        return json.dumps(
            {k: {"matched": v.matched, "confidence": v.confidence, "detail": v.detail} for k, v in results.items()},
            indent=2,
        )
