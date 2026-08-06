"""RIG Memory OS v10 — Agent bootstrap CLI (Phase 6).

Prints a session-start packet (JSON or human-readable) for coding agents
to load at session start.

Usage:
    python -m founder_runtime.bootstrap          # human-readable
    python -m founder_runtime.bootstrap --json    # raw JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone


def gather_status() -> dict:
    from founder_runtime.runtime import MemoryOSRuntime

    rt = MemoryOSRuntime.from_env()
    try:
        snap = rt.cockpit.snapshot()
        intents = rt.intent.all_intents()
        pending = sum(1 for i in intents if i.status.value == "pending")
        completed = sum(1 for i in intents if i.status.value == "completed")

        gateway_failures = rt.gateway.persistence_failures()
        intent_failures = rt.intent.persistence_failures()

        return {
            "status": "ok",
            "cockpit_state": snap.control_state.value,
            "budget": snap.budget_remaining,
            "pending_intents": pending,
            "completed_intents": completed,
            "receipt_count": len(rt.gateway.all_receipts()),
            "panel_count": len(snap.panels),
            "persistence_failures": {
                "gateway": len(gateway_failures),
                "intent": len(intent_failures),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        rt.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="RIG Memory OS agent bootstrap")
    p.add_argument("--json", action="store_true", help="Raw JSON output")
    args = p.parse_args(argv)

    try:
        status = gather_status()
    except Exception as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"Memory OS Status: {status['status']}")
        print(f"  Cockpit:   {status['cockpit_state']}")
        print(f"  Budget:    {status['budget']:.0%}")
        print(f"  Intents:   {status['pending_intents']} pending, "
              f"{status['completed_intents']} completed")
        print(f"  Receipts:  {status['receipt_count']}")
        print(f"  Panels:    {status['panel_count']}")
        fails = status["persistence_failures"]
        total_fails = fails["gateway"] + fails["intent"]
        if total_fails:
            print(f"  FAILURES:  {fails}")
        else:
            print(f"  Failures:  0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
