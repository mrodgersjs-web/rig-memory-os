"""Jake Observer — real-time pushback engine for coding behavior.

Phase 6 of Memory OS v10. Watches agent actions and fires pushback
messages when anti-patterns are detected.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from founder_runtime.pattern_extractor import PatternExtractor, PatternMatch
from founder_runtime.anomaly_detector import AnomalyDetector, SessionSummary, Anomaly


@dataclass
class Pushback:
    pattern: str
    response: str
    counter: str
    severity: str = "high"
    timestamp: float = field(default_factory=time.time)


class JakeObserver:
    """Jake PAI's pushback engine for coding behavior.

    Observes agent actions and triggers pushback messages when
    anti-patterns are detected. Designed to run alongside the Memory
    OS runtime without blocking it.
    """

    ANTI_PATTERNS = {
        "over_engineering": {
            "trigger": "abstractions > 3 AND files_modified > 2",
            "response": "stop — you're over-engineering again. Pattern: 7 consecutive files with similar abstractions. Historical failure rate: 68%.",
            "counter": "Fix the leak, add test, done. NOT 'architect a new memory framework'.",
        },
        "test_avoidance": {
            "trigger": "files_modified > 1 AND tests_skipped == 1",
            "response": "stop — skipping tests. Rushing = bugs = more time. Fix this now.",
            "counter": "Rule: Never skip tests. If estimating wrong, fix that.",
        },
        "scope_creep": {
            "trigger": "git_diff_files > session_goals * 2",
            "response": "stop — scope creep detected. Your goal: 'Fix memory leak'. Your approach: 7 file changes.",
            "counter": "Ship the scary PR. Review is always less bad than you imagine.",
        },
        "rabbit_hole": {
            "trigger": "time_without_progress > 15",
            "response": "stop — rabbit hole. You've been going in circles for 15 minutes.",
            "counter": "Take 5 min break, then re-scope to smallest possible fix.",
        },
    }

    def __init__(self, tolerance: str = "low") -> None:
        self.tolerance = tolerance
        self._extractor = PatternExtractor()
        self._anomaly = AnomalyDetector()
        self._last_progress_ts: float = time.time()
        self._ignored_count: dict[str, int] = {}

    def observe(
        self,
        tool_calls: list[str] | None = None,
        files_modified: list[str] | None = None,
        time_spent: float = 0.0,
        goals: list[str] | None = None,
        tests_written: int = 0,
        abstractions_created: int = 0,
        concrete_implementations: int = 0,
        time_without_progress: float = 0.0,
        session_data: dict[str, Any] | None = None,
    ) -> list[Pushback]:
        """Observe agent actions and trigger pushback.

        Returns a list of Pushback messages to surface to the agent.
        """
        tool_calls = tool_calls or []
        files_modified = files_modified or []
        goals = goals or []
        violations: list[Pushback] = []

        # Build session data for pattern extractor
        sdata = session_data or {
            "files_modified": files_modified,
            "tests_written": tests_written,
            "time_spent": time_spent,
            "goals": goals,
            "tool_calls": tool_calls,
            "abstractions_created": abstractions_created,
            "concrete_implementations": concrete_implementations,
        }

        # Run pattern extraction
        patterns = self._extractor.extract_patterns(sdata)

        # Map patterns to pushback
        for pname, pmatch in patterns.items():
            if not pmatch.matched:
                continue

            anti = self.ANTI_PATTERNS.get(pname)
            if anti is None:
                continue

            # Tolerance gate: in "low" tolerance, all matches fire.
            # In "high" tolerance, only critical ones fire.
            if self.tolerance == "high" and pmatch.confidence < 0.8:
                continue

            violations.append(
                Pushback(
                    pattern=pname,
                    response=anti["response"],
                    counter=anti["counter"],
                    severity="critical" if pmatch.confidence > 0.85 else "high",
                )
            )

        # Rabbit hole detection (time-based)
        if time_without_progress > 15:
            anti = self.ANTI_PATTERNS["rabbit_hole"]
            violations.append(
                Pushback(
                    pattern="rabbit_hole",
                    response=anti["response"],
                    counter=anti["counter"],
                    severity="high",
                )
            )

        # Over-engineering (abstractions check)
        if abstractions_created > 3 and len(files_modified) > 2:
            anti = self.ANTI_PATTERNS["over_engineering"]
            violations.append(
                Pushback(
                    pattern="over_engineering",
                    response=anti["response"],
                    counter=anti["counter"],
                    severity="high",
                )
            )

        # Track ignored patterns for escalation
        for v in violations:
            self._ignored_count[v.pattern] = self._ignored_count.get(v.pattern, 0) + 1

        return violations

    def escalation_level(self, pattern: str) -> str:
        """Return escalation level based on how many times a pattern was ignored."""
        count = self._ignored_count.get(pattern, 0)
        if count >= 3:
            return "blocking"
        elif count >= 2:
            return "warning"
        return "advisory"

    def reset_progress_timer(self) -> None:
        """Call when the agent makes meaningful progress."""
        self._last_progress_ts = time.time()

    def to_json(self, pushbacks: list[Pushback]) -> str:
        return json.dumps(
            [
                {
                    "pattern": p.pattern,
                    "response": p.response,
                    "counter": p.counter,
                    "severity": p.severity,
                    "escalation": self.escalation_level(p.pattern),
                }
                for p in pushbacks
            ],
            indent=2,
        )

    @classmethod
    def main(cls) -> None:
        """CLI entrypoint: --test runs a self-test."""
        import sys

        if "--test" in sys.argv:
            observer = cls(tolerance="low")
            # Simulate a bad session
            pushbacks = observer.observe(
                files_modified=["a.py", "b.py", "c.py", "d.py", "e.py"],
                tests_written=0,
                time_spent=45,
                goals=["fix memory leak"],
                abstractions_created=4,
                concrete_implementations=1,
                time_without_progress=20,
            )
            print("=== Jake Observer Self-Test ===")
            print(f"Pushbacks triggered: {len(pushbacks)}")
            print(observer.to_json(pushbacks))
        elif "--mode" in sys.argv:
            idx = sys.argv.index("--mode")
            mode = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "pushback"
            print(f"Jake Observer started in {mode} mode (tolerance: low)")
            print("Run with --test to self-test.")
        else:
            print("Jake Observer. Use --test for self-test, --mode pushback to start.")


if __name__ == "__main__":
    JakeObserver.main()
