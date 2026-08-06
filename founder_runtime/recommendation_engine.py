"""Recommendation Engine — generates proactive skill/workflow suggestions.

Phase 7 of Memory OS v10.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Recommendation:
    type: str
    trigger: str
    suggestion: str
    estimated_benefit: str
    confidence: float = 0.5
    detail: dict[str, Any] = field(default_factory=dict)


RECOMMENDATION_TEMPLATES = {
    "create_new_skill": {
        "trigger_template": "{count} manual operations using same pattern: '{pattern}'",
        "suggestion_template": "Create skill: /skills/{skill_name}",
        "benefit_template": "~{hours}h/month saved",
        "threshold": 5,
    },
    "workflow_improvement": {
        "trigger_template": "PR review avg {cycles} cycles, tests catch {catch_rate}%",
        "suggestion_template": "{suggestion}",
        "benefit_template": "~{improvement}% quality improvement",
        "threshold": 3,
    },
    "harness_optimization": {
        "trigger_template": "context window at {pct}% on {sessions} sessions",
        "suggestion_template": "{suggestion}",
        "benefit_template": "~{efficiency}% better context utilization",
        "threshold": 80,
    },
}


class RecommendationEngine:
    """Generates proactive recommendations for skill creation, workflow
    improvements, and harness optimization.
    """

    def __init__(self) -> None:
        self._history: list[dict[str, Any]] = []

    def add_session(self, session_data: dict[str, Any]) -> None:
        self._history.append(session_data)
        if len(self._history) > 200:
            self._history = self._history[-200:]

    def recommend(self) -> list[Recommendation]:
        """Generate recommendations based on accumulated session history."""
        recs: list[Recommendation] = []

        # Check for repeated manual patterns
        skill_recs = self._check_repeated_patterns()
        recs.extend(skill_recs)

        # Check for PR review cycles
        review_recs = self._check_review_cycles()
        recs.extend(review_recs)

        # Check for context pressure
        ctx_recs = self._check_context_pressure()
        recs.extend(ctx_recs)

        return recs

    def _check_repeated_patterns(self) -> list[Recommendation]:
        """Look for repeated manual operations that could be a skill."""
        recs: list[Recommendation] = []
        # Group sessions by tool pattern signature
        from collections import Counter
        signatures: Counter[str] = Counter()
        for session in self._history:
            sig = "|".join(sorted(session.get("tool_sequence", [])))
            if sig:
                signatures[sig] += 1

        threshold = RECOMMENDATION_TEMPLATES["create_new_skill"]["threshold"]
        for sig, count in signatures.items():
            if count >= threshold:
                skill_name = sig.replace("|", "-").replace(" ", "-")[:40].lower()
                recs.append(
                    Recommendation(
                        type="create_new_skill",
                        trigger=RECOMMENDATION_TEMPLATES["create_new_skill"]["trigger_template"].format(
                            count=count, pattern=sig
                        ),
                        suggestion=RECOMMENDATION_TEMPLATES["create_new_skill"]["suggestion_template"].format(
                            skill_name=skill_name
                        ),
                        estimated_benefit=RECOMMENDATION_TEMPLATES["create_new_skill"]["benefit_template"].format(
                            hours=count
                        ),
                        confidence=min(0.9, 0.4 + count * 0.05),
                    )
                )
        return recs

    def _check_review_cycles(self) -> list[Recommendation]:
        recs: list[Recommendation]
        recs = []
        review_sessions = [s for s in self._history if s.get("review_cycles", 0) > 0]
        if len(review_sessions) >= 3:
            avg_cycles = sum(s["review_cycles"] for s in review_sessions) / len(review_sessions)
            if avg_cycles > 2.5:
                recs.append(
                    Recommendation(
                        type="workflow_improvement",
                        trigger=f"PR review avg {avg_cycles:.1f} cycles across {len(review_sessions)} sessions",
                        suggestion="Add pre-commit AI review hook",
                        estimated_benefit="~23% quality improvement",
                        confidence=0.7,
                    )
                )
        return recs

    def _check_context_pressure(self) -> list[Recommendation]:
        recs: list[Recommendation] = []
        high_ctx = [s for s in self._history if s.get("context_pct", 0) > 80]
        if len(high_ctx) >= 3:
            avg_pct = sum(s["context_pct"] for s in high_ctx) / len(high_ctx)
            recs.append(
                Recommendation(
                    type="harness_optimization",
                    trigger=f"context at {avg_pct:.0f}% on {len(high_ctx)} sessions",
                    suggestion="Enable context-save before large refactors",
                    estimated_benefit="~40% better context utilization",
                    confidence=0.65,
                )
            )
        return recs

    def to_json(self, recs: list[Recommendation]) -> str:
        return json.dumps(
            [
                {
                    "type": r.type,
                    "trigger": r.trigger,
                    "suggestion": r.suggestion,
                    "estimated_benefit": r.estimated_benefit,
                    "confidence": r.confidence,
                }
                for r in recs
            ],
            indent=2,
        )
