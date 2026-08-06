"""Anomaly Detector — detects deviations from productive patterns.

Phase 5 of Memory OS v10.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Anomaly:
    type: str
    severity: str  # "critical", "high", "medium", "low"
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionSummary:
    time_spent: float = 0.0  # minutes
    expected_time: float = 0.0
    files_modified: int = 0
    tests_written: int = 0
    abstractions_created: int = 0
    concrete_implementations: int = 0
    tool_calls: list[str] = field(default_factory=list)
    rollback_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class AnomalyDetector:
    """Detects anomalies in session summaries.

    Compares against a learned baseline that is loaded from the Postgres
    store (or sensible defaults for cold-start).
    """

    DEFAULT_BASELINE = {
        "expected_time_minutes": 20,
        "files_per_session": 3,
        "tests_per_files_threshold": 3,
        "abstraction_ratio_max": 1.0,
        "rollback_rate_max": 0.2,
    }

    def __init__(self, baseline: dict[str, Any] | None = None) -> None:
        self.baseline = {**self.DEFAULT_BASELINE, **(baseline or {})}

    def detect(self, summary: SessionSummary) -> list[Anomaly]:
        """Detect anomalies in a session summary."""
        anomalies: list[Anomaly] = []

        # Time overrun
        expected = summary.expected_time or self.baseline["expected_time_minutes"]
        if summary.time_spent > expected * 2:
            anomalies.append(
                Anomaly(
                    type="time_overrun",
                    severity="high",
                    message=f"Session took {summary.time_spent:.0f}min vs {expected:.0f}min expected",
                    detail={"actual": summary.time_spent, "expected": expected},
                )
            )

        # Test coverage
        if summary.files_modified >= self.baseline["tests_per_files_threshold"] and summary.tests_written == 0:
            anomalies.append(
                Anomaly(
                    type="test_avoidance",
                    severity="critical",
                    message=f"Modified {summary.files_modified} files without writing tests",
                    detail={"files_modified": summary.files_modified, "tests_written": 0},
                )
            )

        # Premature abstraction
        if summary.abstractions_created > summary.concrete_implementations:
            anomalies.append(
                Anomaly(
                    type="premature_abstraction",
                    severity="high",
                    message="Abstracted before having multiple concrete implementations",
                    detail={
                        "abstractions": summary.abstractions_created,
                        "concretes": summary.concrete_implementations,
                    },
                )
            )

        # High rollback rate
        if summary.rollback_count > 2:
            anomalies.append(
                Anomaly(
                    type="high_rollback",
                    severity="medium",
                    message=f"{summary.rollback_count} rollbacks in a single session",
                    detail={"rollback_count": summary.rollback_count},
                )
            )

        return anomalies

    def to_json(self, anomalies: list[Anomaly]) -> str:
        return json.dumps(
            [{"type": a.type, "severity": a.severity, "message": a.message, "detail": a.detail} for a in anomalies],
            indent=2,
        )
