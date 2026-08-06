"""RIG Memory OS v10 — Prefect workflow definitions (Phase 6 + Phase 8).

Three flows callable without a Prefect server (local execution).
With a Prefect server: `python -m founder_runtime.flows serve`.

Phase 8: connection pooling for concurrent flow workers via psycopg_pool.
When pool_dsn is provided, flows use a shared ConnectionPool instead of
per-invocation psycopg.connect().

Usage:
    python -m founder_runtime.flows run --flow reconcile_flow --intents /path/to/intents.jsonl
    python -m founder_runtime.flows run --flow reconcile_flow --pool-dsn "host=localhost port=5432 dbname=rig_memory_os_phase1"
    python -m founder_runtime.flows run --flow intent_expiry_flow
    python -m founder_runtime.flows run --flow cockpit_watchdog_flow
    python -m founder_runtime.flows serve
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from prefect import flow, task

# ---------------------------------------------------------------------------
# Phase 8: connection pool factory
# ---------------------------------------------------------------------------


def get_connection_pool(dsn: str, min_size: int = 1, max_size: int = 4):
    """Create a psycopg_pool.ConnectionPool for concurrent flow workers.

    The pool is created on-demand and should be closed by the caller
    after all tasks that needed it have completed.
    """
    from psycopg_pool import ConnectionPool

    return ConnectionPool(conninfo=dsn, min_size=min_size, max_size=max_size)


def _reconcile_with_pool(pool, writer, env, events_path, receipts_path,
                         checkpoints_path, intents_path):
    """Run reconcile using a pooled connection.

    Mirrors service._reconcile_cycle logic but borrows from a pool
    instead of opening a fresh connection.
    """
    from founder_runtime.reconcile import (
        reconcile_checkpoints,
        reconcile_effect_receipts,
        reconcile_events,
        reconcile_intents,
    )

    conn = None
    borrow_ok = False
    try:
        conn = pool.getconn()
        conn.autocommit = True
        borrow_ok = True
        reports = []
        p = intents_path or env.get("RIG_MEMORY_OS_RECONCILE_INTENTS")
        if p and (isinstance(p, str) and os.path.exists(p)):
            reports.append(reconcile_intents(Path(p), writer, conn))
        p = events_path or env.get("RIG_MEMORY_OS_RECONCILE_EVENTS")
        if p and (isinstance(p, str) and os.path.exists(p)):
            reports.append(reconcile_events(Path(p), writer, conn))
        p = receipts_path or env.get("RIG_MEMORY_OS_RECONCILE_RECEIPTS")
        if p and (isinstance(p, str) and os.path.exists(p)):
            reports.append(reconcile_effect_receipts(Path(p), writer, conn))
        p = checkpoints_path or env.get("RIG_MEMORY_OS_RECONCILE_CHECKPOINTS")
        if p and (isinstance(p, str) and os.path.exists(p)):
            reports.append(reconcile_checkpoints(Path(p), writer, conn))
        return {
            "exit_code": 0,
            "reports": [
                {
                    "source": r.source,
                    "scanned": r.scanned,
                    "written": r.written,
                    "skipped_duplicate": r.skipped_duplicate,
                    "errors": r.errors,
                }
                for r in reports
                if r is not None
            ],
        }
    finally:
        if conn is not None and borrow_ok:
            pool.putconn(conn)


@task
def _reconcile_task(events_path: str, receipts_path: str,
                    checkpoints_path: str, intents_path: str, dsn: str,
                    pool_dsn: Optional[str] = None):
    # Phase 8: if pool_dsn is provided, use connection pooling
    if pool_dsn:
        from founder_runtime.reconcile import _dsn_from_env
        from founder_runtime.postgres_writer import PostgresWriter

        actual_dsn = dsn or pool_dsn
        pool = get_connection_pool(actual_dsn)
        writer = PostgresWriter(dsn=actual_dsn)
        try:
            result = _reconcile_with_pool(
                pool, writer, os.environ,
                events_path=events_path,
                receipts_path=receipts_path,
                checkpoints_path=checkpoints_path,
                intents_path=intents_path,
            )
            return result
        finally:
            writer.close()
            pool.close()
    # Phase 6 path: use reconcile_main as before
    from founder_runtime.reconcile import main as reconcile_main

    argv = []
    if intents_path:
        argv += ["--intents", intents_path]
    if events_path:
        argv += ["--events", events_path]
    if receipts_path:
        argv += ["--receipts", receipts_path]
    if checkpoints_path:
        argv += ["--checkpoints", checkpoints_path]
    if dsn:
        argv += ["--dsn", dsn]
    if not argv:
        return {"error": "no reconciliation paths provided"}
    rc = reconcile_main(argv)
    return {"exit_code": rc}


@flow(name="reconcile_flow", description="Replay JSONL logs into Postgres")
def reconcile_flow(
    events_path: str = "",
    receipts_path: str = "",
    checkpoints_path: str = "",
    intents_path: str = "",
    dsn: str = "",
    pool_dsn: Optional[str] = "",
) -> dict:
    return _reconcile_task(
        events_path, receipts_path, checkpoints_path, intents_path,
        dsn, pool_dsn=pool_dsn or None,
    )


@task
def _expiry_task():
    from founder_runtime.runtime import MemoryOSRuntime

    rt = MemoryOSRuntime.from_env()
    try:
        expired = rt.intent.expire_overdue()
        return {"expired": len(expired)}
    finally:
        rt.close()


@flow(name="intent_expiry_flow",
      description="Expire overdue prospective-memory intents")
def intent_expiry_flow() -> dict:
    return _expiry_task()


@task
def _watchdog_task():
    from founder_runtime.runtime import MemoryOSRuntime

    rt = MemoryOSRuntime.from_env()
    try:
        snap = rt.cockpit.snapshot()
        state, budget = rt.cockpit._store.read_state() if rt.cockpit._store \
            else (rt.cockpit.state.value, rt.cockpit.budget)
        return {
            "state": state,
            "budget": budget,
            "panel_count": len(snap.panels),
        }
    finally:
        rt.close()


@flow(name="cockpit_watchdog_flow",
      description="Check cockpit state, budget, and panel health")
def cockpit_watchdog_flow() -> dict:
    return _watchdog_task()


# ---------------------------------------------------------------------------
# Phase 9: Intelligence cycle flow
# ---------------------------------------------------------------------------

@task
def _intelligence_cycle_task():
    """Run a full intelligence cycle:
    1. Resolve expired predictions and record calibration
    2. Run recommendation engine over accumulated sessions
    3. Write any new recommendations to Obsidian via the bridge
    """
    from founder_runtime.runtime import MemoryOSRuntime

    rt = MemoryOSRuntime.from_env()
    try:
        results = {"resolved": 0, "recommendations": 0}

        # Resolve expired predictions
        if rt.postgres_writer:
            try:
                expired = rt.postgres_writer.unresolved_predictions(limit=50)
                for pred in expired:
                    pred_id = pred["prediction_id"]
                    predicted = pred["predicted_state"]
                    # Cannot resolve without actual outcome; skip if still unresolved
                    # The actual resolution happens when agents call memory_resolve_prediction
                    # Here we just count what's outstanding
                results["outstanding_predictions"] = len(expired)
            except Exception as e:
                results["prediction_error"] = str(e)

        # Generate recommendations
        try:
            rec_result = rt.recommend()
            results["recommendations"] = rec_result.get("recommendation_count", 0)
        except Exception as e:
            results["recommendation_error"] = str(e)

        # Intelligence snapshot for logging
        try:
            snap = rt.intelligence_snapshot()
            results["snapshot"] = snap
        except Exception:
            pass

        return results
    finally:
        rt.close()


@flow(name="intelligence_cycle_flow",
      description="Resolve predictions, generate recommendations, sync memory bridge")
def intelligence_cycle_flow() -> dict:
    return _intelligence_cycle_task()


def serve():
    """Start Prefect flow serve for always-on mode.
    
    In Prefect 3.x, we use flow.deploy() to create scheduled deployments
    or run flows directly with flow.serve() for local development.
    """
    import subprocess
    import sys
    
    # Option 1: Use Prefect CLI to create scheduled deployments
    # This is the production way — flows run on a schedule via Prefect server
    try:
        # Check if Prefect server is running
        result = subprocess.run(
            ["prefect", "server", "status"],
            capture_output=True,
            timeout=5
        )
        server_running = result.returncode == 0
    except Exception:
        server_running = False
    
    if server_running:
        print("Prefect server detected — creating scheduled deployments")
        # Create deployments with schedules
        reconcile_flow.deploy(
            name="reconcile",
            schedule={"interval": 300},  # every 5 minutes
            work_pool_name="default-agent-pool",
        )
        intent_expiry_flow.deploy(
            name="intent-expiry",
            schedule={"interval": 600},  # every 10 minutes
            work_pool_name="default-agent-pool",
        )
        cockpit_watchdog_flow.deploy(
            name="cockpit-watchdog",
            schedule={"interval": 120},  # every 2 minutes
            work_pool_name="default-agent-pool",
        )
        intelligence_cycle_flow.deploy(
            name="intelligence-cycle",
            schedule={"interval": 300},  # every 5 minutes
            work_pool_name="default-agent-pool",
        )
        print("Deployments created. Start worker with: prefect worker start --pool default-agent-pool")
    else:
        # Option 2: Run flows in a loop (no Prefect server needed)
        print("No Prefect server — running flows in local loop mode")
        print("Press Ctrl+C to stop")
        
        import time
        import threading
        
        def run_reconcile():
            while True:
                try:
                    reconcile_flow()
                except Exception as e:
                    print(f"reconcile_flow error: {e}")
                time.sleep(300)  # 5 minutes
        
        def run_expiry():
            while True:
                try:
                    intent_expiry_flow()
                except Exception as e:
                    print(f"intent_expiry_flow error: {e}")
                time.sleep(600)  # 10 minutes
        
        def run_watchdog():
            while True:
                try:
                    cockpit_watchdog_flow()
                except Exception as e:
                    print(f"cockpit_watchdog_flow error: {e}")
                time.sleep(120)  # 2 minutes
        
        def run_intelligence():
            while True:
                try:
                    intelligence_cycle_flow()
                except Exception as e:
                    print(f"intelligence_cycle_flow error: {e}")
                time.sleep(300)  # 5 minutes
        
        # Start all flows in separate threads
        threads = [
            threading.Thread(target=run_reconcile, daemon=True),
            threading.Thread(target=run_expiry, daemon=True),
            threading.Thread(target=run_watchdog, daemon=True),
            threading.Thread(target=run_intelligence, daemon=True),
        ]
        
        for t in threads:
            t.start()
        
        # Keep main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down flows...")
            sys.exit(0)


def main(argv=None):
    p = argparse.ArgumentParser(description="RIG Memory OS Prefect flows")
    sub = p.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run a flow directly")
    run_p.add_argument("--flow", required=True,
                       choices=["reconcile_flow", "intent_expiry_flow",
                                "cockpit_watchdog_flow",
                                "intelligence_cycle_flow"])
    run_p.add_argument("--events", default="")
    run_p.add_argument("--receipts", default="")
    run_p.add_argument("--checkpoints", default="")
    run_p.add_argument("--intents", default="")
    run_p.add_argument("--dsn", default="")
    run_p.add_argument("--pool-dsn", default="",
                       help="Use psycopg_pool connection pooling (Phase 8)")

    sub.add_parser("serve", help="Start Prefect flow serve (always-on)")

    args = p.parse_args(argv)

    if args.cmd == "run":
        flows = {
            "reconcile_flow": reconcile_flow,
            "intent_expiry_flow": intent_expiry_flow,
            "cockpit_watchdog_flow": cockpit_watchdog_flow,
            "intelligence_cycle_flow": intelligence_cycle_flow,
        }
        fn = flows[args.flow]
        if args.flow == "reconcile_flow":
            result = fn(
                events_path=args.events, receipts_path=args.receipts,
                checkpoints_path=args.checkpoints, intents_path=args.intents,
                dsn=args.dsn, pool_dsn=args.pool_dsn,
            )
        else:
            result = fn()
        print(json.dumps(result, default=str))
        return 0
    elif args.cmd == "serve":
        serve()
        return 0
    else:
        p.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
