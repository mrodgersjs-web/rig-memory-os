"""RIG Memory OS v10 — Phase 0 flow policies (S0 Contracts, task 1.3).

Defines the six separated Prefect flow queues for Phase 0, each with explicit
concurrency limit, queue TTL, timeout, retry budget, stale-run coalescing
policy, and queue TTL action. Replaces the monolithic control flow with
bounded, observable lanes.

Following the v10 spec:
- Prefect coordinates schedules, ingestion, consolidation, evaluation,
  backfills, calibration, backup, and reconciliation.
- Each queue MUST declare its own concurrency limit, queue TTL, timeout,
  retry budget, and stale-run coalescing policy.
- Transport failures yield bounded degraded receipts; retries occur at the
  next scheduled admission, never inside a sleeping worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class RetryPolicy(str, Enum):
    NEXT_ADMISSION = "next_admission"
    COALESCE = "coalesce"
    DEAD_LETTER = "dead_letter"


class StaleRunCoalescing(str, Enum):
    MOST_RECENT_ONLY = "most_recent_only"
    NONE = "none"


class QueueTtlAction(str, Enum):
    DEGRADE = "degrade"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class FlowPolicy:
    """Bounded policy for one Prefect flow queue.

    Required: flow_name, concurrency_limit, queue_ttl_seconds,
    timeout_seconds, retry_budget, retry_policy, stale_run_coalescing,
    queue_ttl_action, description.
    """

    flow_name: str
    concurrency_limit: int
    queue_ttl_seconds: int
    timeout_seconds: int
    retry_budget: int
    retry_policy: RetryPolicy
    stale_run_coalescing: StaleRunCoalescing
    queue_ttl_action: QueueTtlAction
    description: str

    def __post_init__(self) -> None:
        if self.concurrency_limit < 1:
            raise ValueError(f"{self.flow_name}: concurrency_limit must be >= 1")
        if self.queue_ttl_seconds <= 0:
            raise ValueError(f"{self.flow_name}: queue_ttl_seconds must be > 0")
        if self.timeout_seconds <= 0:
            raise ValueError(f"{self.flow_name}: timeout_seconds must be > 0")
        if self.timeout_seconds > self.queue_ttl_seconds:
            raise ValueError(
                f"{self.flow_name}: timeout_seconds ({self.timeout_seconds}) "
                f"exceeds queue_ttl_seconds ({self.queue_ttl_seconds})"
            )
        if self.retry_budget < 0:
            raise ValueError(f"{self.flow_name}: retry_budget must be >= 0")


# =====================================================================
# The six Phase 0 flow policies from design D2
# =====================================================================

PHASE0_FLOW_POLICIES: list[FlowPolicy] = [
    FlowPolicy(
        flow_name="control-watchdog",
        concurrency_limit=1,
        queue_ttl_seconds=300,
        timeout_seconds=60,
        retry_budget=0,
        retry_policy=RetryPolicy.NEXT_ADMISSION,
        stale_run_coalescing=StaleRunCoalescing.MOST_RECENT_ONLY,
        queue_ttl_action=QueueTtlAction.DEGRADE,
        description="liveness checks, degraded receipts",
    ),
    FlowPolicy(
        flow_name="collection-36gb",
        concurrency_limit=4,
        queue_ttl_seconds=3600,
        timeout_seconds=600,
        retry_budget=3,
        retry_policy=RetryPolicy.NEXT_ADMISSION,
        stale_run_coalescing=StaleRunCoalescing.MOST_RECENT_ONLY,
        queue_ttl_action=QueueTtlAction.DEAD_LETTER,
        description="signal collection from the 36GB node",
    ),
    FlowPolicy(
        flow_name="youtube-transcript",
        concurrency_limit=2,
        queue_ttl_seconds=7200,
        timeout_seconds=1200,
        retry_budget=2,
        retry_policy=RetryPolicy.NEXT_ADMISSION,
        stale_run_coalescing=StaleRunCoalescing.MOST_RECENT_ONLY,
        queue_ttl_action=QueueTtlAction.DEAD_LETTER,
        description="YouTube transcript and video processing",
    ),
    FlowPolicy(
        flow_name="recall-derived",
        concurrency_limit=1,
        queue_ttl_seconds=600,
        timeout_seconds=120,
        retry_budget=3,
        retry_policy=RetryPolicy.NEXT_ADMISSION,
        stale_run_coalescing=StaleRunCoalescing.MOST_RECENT_ONLY,
        queue_ttl_action=QueueTtlAction.DEAD_LETTER,
        description="Recall API synchronization",
    ),
    FlowPolicy(
        flow_name="memory-convergence",
        concurrency_limit=1,
        queue_ttl_seconds=900,
        timeout_seconds=180,
        retry_budget=2,
        retry_policy=RetryPolicy.NEXT_ADMISSION,
        stale_run_coalescing=StaleRunCoalescing.MOST_RECENT_ONLY,
        queue_ttl_action=QueueTtlAction.DEAD_LETTER,
        description="GBrain/Obsidian sync, card reconciliation",
    ),
    FlowPolicy(
        flow_name="daily-briefing",
        concurrency_limit=1,
        queue_ttl_seconds=600,
        timeout_seconds=120,
        retry_budget=0,
        retry_policy=RetryPolicy.NEXT_ADMISSION,
        stale_run_coalescing=StaleRunCoalescing.MOST_RECENT_ONLY,
        queue_ttl_action=QueueTtlAction.DEAD_LETTER,
        description="daily Memory OS brief",
    ),
]


def policy_by_name(flow_name: str) -> FlowPolicy:
    """Return the policy for `flow_name` or raise KeyError."""
    for p in PHASE0_FLOW_POLICIES:
        if p.flow_name == flow_name:
            return p
    raise KeyError(f"unknown flow: {flow_name!r}")


def all_flow_names() -> list[str]:
    """Return the list of registered Phase 0 flow names."""
    return [p.flow_name for p in PHASE0_FLOW_POLICIES]


# =====================================================================
# Failure-handling policy (universal)
# =====================================================================

# Per the v10 spec, transport failures MUST yield a bounded degraded receipt;
# retries occur at the next scheduled admission, never inside a sleeping worker.
# This is enforced by all six policies above (RetryPolicy.NEXT_ADMISSION +
# retry_budget is bounded + stale_run_coalescing is MOST_RECENT_ONLY).