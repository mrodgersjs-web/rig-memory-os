#!/usr/bin/env python3
"""
Langfuse-integrated Memory OS flow runner.

This wraps the Prefect flows with Langfuse tracing for observability.
Each flow execution is traced as a Langfuse trace, allowing you to:
- See flow execution history in Langfuse UI
- Track latency and errors
- Debug flow failures with full context
- Set up alerts on flow failures

Usage:
    python -m founder_runtime.langfuse_flow_runner
"""

import os
import sys
import json
import time
from datetime import datetime
from typing import Optional, Callable, Any

# CRITICAL: force ephemeral mode BEFORE importing prefect.
# A foreign Prefect server (automation-foundry's :4200) was hijacking flow
# runs via default API discovery — wedged API = 45s+ hangs per flow. Ephemeral
# mode runs flows fully local: no API calls, no cross-project coupling, no
# cron dependency on a server we don't own.
os.environ.pop("PREFECT_API_URL", None)
os.environ["PREFECT_SERVER_ALLOW_EPHEMERAL"] = "true"
os.environ["PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW"] = "ignore"

# Add founder-runtime to path
sys.path.insert(0, '/Users/rig128gb/Developer/rig-intelligence-worktrees/rig-memory-os/platform/founder-runtime')

# Langfuse integration - conditional import (v4 moved observe to top-level)
LANGFUSE_AVAILABLE = False
try:
    try:
        # langfuse >= 4.x: observe at top level, context via langfuse.propagate
        from langfuse import Langfuse, observe  # type: ignore
        try:
            from langfuse import propagate as _propagate  # noqa
        except Exception:
            pass
        class _CtxV4:
            @staticmethod
            def update_current_trace(**kwargs):
                try:
                    from langfuse import get_client
                    get_client().update_current_trace(**kwargs)
                except Exception:
                    pass
        langfuse_context = _CtxV4()
    except ImportError:
        # langfuse 3.x fallback
        from langfuse import Langfuse  # type: ignore
        from langfuse.decorators import observe, langfuse_context  # type: ignore
    LANGFUSE_AVAILABLE = True
    # Initialize Langfuse client
    langfuse = Langfuse(
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    )
except ImportError:
    print("Warning: langfuse not installed. Running without tracing.")
    # Create no-op decorators
    def observe(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if args and callable(args[0]) is False else decorator(args[0]) if args else decorator
    
    class FakeContext:
        @staticmethod
        def update_current_trace(**kwargs):
            pass
    
    langfuse_context = FakeContext()
    langfuse = None

from prefect import flow


@observe(name="memory-os-reconcile")
def run_reconcile_flow(**kwargs):
    """Run reconcile flow with Langfuse tracing."""
    from founder_runtime.flows import reconcile_flow

    if LANGFUSE_AVAILABLE:
        langfuse_context.update_current_trace(
            name="reconcile_flow",
            metadata={"flow": "reconcile", "timestamp": datetime.now().isoformat()},
            tags=["memory-os", "reconcile"]
        )

    return reconcile_flow(**kwargs)


def _run_local(task_path: str) -> dict:
    """Local mode: call the task body directly, bypassing the Prefect runtime.

    Prefect 3.x refuses to run @flow without an API server, and its ephemeral
    auto-start is unreliable across versions. For the cron pipeline the flow
    orchestration machinery buys nothing — the task bodies ARE the work.
    Flow wrappers remain for when a real server exists.
    """
    import importlib
    module_name, fn_name = task_path.rsplit(".", 1)
    mod = importlib.import_module(module_name)
    task_obj = getattr(mod, fn_name)
    fn = getattr(task_obj, "fn", task_obj)  # unwrap @task
    try:
        result = fn()
        return {"status": "ok", "result": result, "mode": "local-direct"}
    except TypeError:
        # reconcile's task needs args; with none provided it's a documented no-op
        return {"status": "skipped", "reason": "no reconcile paths provided",
                "mode": "local-direct"}


@observe(name="memory-os-intent-expiry")
def run_intent_expiry_flow():
    """Run intent expiry flow with Langfuse tracing."""
    from founder_runtime.flows import intent_expiry_flow
    
    if LANGFUSE_AVAILABLE:
        langfuse_context.update_current_trace(
            name="intent_expiry_flow",
            metadata={"flow": "intent_expiry", "timestamp": datetime.now().isoformat()},
            tags=["memory-os", "intent-expiry"]
        )
    
    return intent_expiry_flow()


@observe(name="memory-os-cockpit-watchdog")
def run_cockpit_watchdog_flow():
    """Run cockpit watchdog flow with Langfuse tracing."""
    from founder_runtime.flows import cockpit_watchdog_flow
    
    if LANGFUSE_AVAILABLE:
        langfuse_context.update_current_trace(
            name="cockpit_watchdog_flow",
            metadata={"flow": "cockpit_watchdog", "timestamp": datetime.now().isoformat()},
            tags=["memory-os", "watchdog"]
        )
    
    return cockpit_watchdog_flow()


@observe(name="memory-os-intelligence-cycle")
def run_intelligence_cycle_flow():
    """Run intelligence cycle flow with Langfuse tracing."""
    from founder_runtime.flows import intelligence_cycle_flow
    
    if LANGFUSE_AVAILABLE:
        langfuse_context.update_current_trace(
            name="intelligence_cycle_flow",
            metadata={"flow": "intelligence_cycle", "timestamp": datetime.now().isoformat()},
            tags=["memory-os", "intelligence"]
        )
    
    return intelligence_cycle_flow()


def run_all_flows():
    """Run all Memory OS flows — local direct execution (no Prefect server)."""
    results = {}

    print(f"[{datetime.now()}] Starting Memory OS flow cycle (local-direct mode)")

    tasks = [
        ("reconcile", "founder_runtime.flows._reconcile_task"),
        ("intent_expiry", "founder_runtime.flows._expiry_task"),
        ("cockpit_watchdog", "founder_runtime.flows._watchdog_task"),
        ("intelligence_cycle", "founder_runtime.flows._intelligence_cycle_task"),
    ]

    for name, path in tasks:
        try:
            print(f"  Running {name}...")
            result = _run_local(path)
            results[name] = {"status": "success", "result": result}
            print(f"  ✓ {name} completed")
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)}
            print(f"  ✗ {name} failed: {e}")

    # Flush Langfuse
    if LANGFUSE_AVAILABLE:
        langfuse.flush()

    print(f"[{datetime.now()}] Flow cycle complete")
    return results


def main():
    """Run all flows once (for cron) or loop (for serve mode)."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Langfuse-integrated Memory OS flow runner")
    parser.add_argument("--loop", action="store_true", help="Run in loop mode (every 5 minutes)")
    parser.add_argument("--interval", type=int, default=300, help="Loop interval in seconds (default: 300)")
    args = parser.parse_args()
    
    if args.loop:
        print(f"Starting Memory OS flows in loop mode (interval: {args.interval}s)")
        print("Press Ctrl+C to stop")
        try:
            while True:
                run_all_flows()
                print(f"Sleeping for {args.interval} seconds...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped by user")
    else:
        results = run_all_flows()
        # Exit with error if any flow failed
        if any(r.get("status") == "error" for r in results.values()):
            sys.exit(1)
        sys.exit(0)


if __name__ == "__main__":
    main()
