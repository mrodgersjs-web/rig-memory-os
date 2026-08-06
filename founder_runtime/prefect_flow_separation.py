"""RIG Memory OS v10 — Phase 0 Prefect flow separation (S1 Durable Base, task 2.3).

Generates the six separated Prefect deployment specs for Phase 0, each
with explicit concurrency limit, queue TTL, timeout, retry budget,
stale-run coalescing, and queue TTL action. Replaces the monolithic
control flow with bounded, observable lanes.

Per design D2:
- control-watchdog (concurrency=1, TTL=300s, timeout=60s, retry=0)
- collection-36gb (concurrency=4, TTL=3600s, timeout=600s, retry=3)
- youtube-transcript (concurrency=2, TTL=7200s, timeout=1200s, retry=2)
- recall-derived (concurrency=1, TTL=600s, timeout=120s, retry=3)
- memory-convergence (concurrency=1, TTL=900s, timeout=180s, retry=2)
- daily-briefing (concurrency=1, TTL=600s, timeout=120s, retry=0)

Per the v10 spec:
- Prefect owns schedules, ingestion, consolidation, evaluation,
  backfills, calibration, backup, and reconciliation
- Each queue MUST declare its own concurrency limit, queue TTL, timeout,
  retry budget, and stale-run coalescing policy
- Transport failures yield bounded degraded receipts; retries occur at
  the next scheduled admission, never inside a sleeping worker
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .flow_policies import PHASE0_FLOW_POLICIES, FlowPolicy


@dataclass
class PrefectDeployment:
    """One Prefect deployment spec for a Phase 0 flow."""

    name: str
    flow_name: str  # entrypoint Python function name
    concurrency_limit: int
    queue_ttl_seconds: int
    timeout_seconds: int
    retry_budget: int
    retry_policy: str
    stale_run_coalescing: str
    queue_ttl_action: str
    schedule: Optional[str] = None  # cron-style or None for event-triggered
    paused: bool = True  # Phase 0 deploys start paused; activate after exit gate
    production: bool = False  # never set true by Phase 0; activated after exit gate

    def to_prefect_spec(self) -> dict:
        """Emit a Prefect deployment spec dict.

        Per the v10 spec, these deployments start paused, inactive,
        effect-disabled, retry-disabled, non-persistent, and
        production_admissible=False until the Phase 0 exit gate passes.
        """
        return {
            "name": self.name,
            "flow_name": self.flow_name,
            "concurrency_limit": self.concurrency_limit,
            "timeout_seconds": self.timeout_seconds,
            "schedules": (
                [{"schedule": self.schedule, "active": False}]
                if self.schedule
                else []
            ),
            "parameters": {},
            "tags": [
                "phase0",
                f"queue_ttl={self.queue_ttl_seconds}s",
                f"retry_budget={self.retry_budget}",
            ],
            "paused": self.paused,
            "production": False,  # never set true by Phase 0
            "admission_policy": {
                "queue_ttl_seconds": self.queue_ttl_seconds,
                "retry_policy": self.retry_policy,
                "stale_run_coalescing": self.stale_run_coalescing,
                "queue_ttl_action": self.queue_ttl_action,
            },
        }


# Phase 0 flow → Prefect deployment mapping
# Flow names match the entrypoints in founder_runtime.worker module
PHASE0_DEPLOYMENTS: list[PrefectDeployment] = []


def build_phase0_deployments(
    schedules: Optional[dict[str, str]] = None,
) -> list[PrefectDeployment]:
    """Build the six Phase 0 Prefect deployment specs from the policies.

    `schedules` maps flow_name → cron string. Default schedules are
    conservative (mostly event-triggered; only daily-briefing has a
    default cron).
    """
    schedules = schedules or {
        "daily-briefing": "0 6 * * *",  # 06:00 daily
    }

    deployments: list[PrefectDeployment] = []
    for policy in PHASE0_FLOW_POLICIES:
        deployments.append(
            PrefectDeployment(
                name=f"phase0-{policy.flow_name}",
                flow_name=f"founder_runtime.worker.{policy.flow_name.replace('-', '_')}",
                concurrency_limit=policy.concurrency_limit,
                queue_ttl_seconds=policy.queue_ttl_seconds,
                timeout_seconds=policy.timeout_seconds,
                retry_budget=policy.retry_budget,
                retry_policy=policy.retry_policy.value,
                stale_run_coalescing=policy.stale_run_coalescing.value,
                queue_ttl_action=policy.queue_ttl_action.value,
                schedule=schedules.get(policy.flow_name),
                paused=True,  # start paused
            )
        )
    PHASE0_DEPLOYMENTS.clear()
    PHASE0_DEPLOYMENTS.extend(deployments)
    return deployments


def all_deployment_specs() -> list[dict]:
    """Return all Phase 0 deployment specs as JSON-ready dicts.

    Caller writes these to disk and registers with Prefect. Phase 0
    leaves all deployments in paused=True state; activation happens
    after the Phase 0 exit gate passes.
    """
    if not PHASE0_DEPLOYMENTS:
        build_phase0_deployments()
    return [d.to_prefect_spec() for d in PHASE0_DEPLOYMENTS]


def emit_deployment_manifest(path: str) -> int:
    """Write the deployment manifest JSON to `path`.

    Returns the number of deployment specs written.
    """
    specs = all_deployment_specs()
    with open(path, "w") as f:
        json.dump(specs, f, indent=2, sort_keys=True)
    return len(specs)


# =====================================================================
# Failure-handling policy enforcement
# =====================================================================

# Per the v10 spec, transport failures MUST yield a bounded degraded
# receipt; retries occur at the next scheduled admission. The Phase 0
# deployments above enforce this via:
#   - retry_budget is bounded (0-3 depending on flow)
#   - retry_policy is "next_admission" for all flows
#   - queue_ttl_seconds is bounded (300-7200 depending on flow)
#   - queue_ttl_action is "degrade" or "dead_letter"
# This means a worker never sleeps waiting for a retry — it surfaces
# DEGRADED and frees the slot.