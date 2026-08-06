"""RIG Memory OS v10 — 24/7 daemon entrypoint (Phase 6 + Phase 7 pooling).

Runs reconcile + intent expiry + cockpit snapshot on a configurable
interval. Graceful shutdown on SIGTERM/SIGINT.

Phase 7: connection pooling for reconcile cycles. The daemon creates one
psycopg connection at start-up and reuses it across all reconcile cycles
within a single daemon lifetime. A fresh connection per cycle (Phase 6
behavior) is still the default for --once mode and for tests.

Usage:
    python -m founder_runtime.service --interval 300
    python -m founder_runtime.service --once   # one cycle, exit
    python -m founder_runtime.service --no-reconcile
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

logger = logging.getLogger("memory-os.service")

_shutdown = False


def _handle_signal(sig, _frame):
    global _shutdown
    _shutdown = True
    logger.info("Signal %s received, shutting down gracefully", sig)


def _dsn_from_env(env=None):
    env = env or os.environ
    dsn = env.get("RIG_MEMORY_OS_DSN")
    if dsn:
        return dsn
    host = env.get("RIG_MEMORY_OS_PG_HOST", "/tmp")
    port = env.get("RIG_MEMORY_OS_PG_PORT", "5432")
    db = env.get("RIG_MEMORY_OS_PG_DB", "rig_memory_os_phase1")
    return f"host={host} port={port} dbname={db}"


def _reconcile_cycle(writer, env, conn=None):
    """Run one reconcile pass.

    Phase 7: accepts an optional pre-opened *conn* (pooled by the caller).
    When None (the --once path and all existing tests), a short-lived
    connection is opened and closed within this call — preserving backward
    compatibility.

    A pooled connection must be autocommit=True (matching the old behaviour)
    so that a failure in one reconcile path does not poison subsequent cycles.
    """
    import psycopg
    from pathlib import Path
    from founder_runtime.reconcile import (
        reconcile_checkpoints, reconcile_effect_receipts,
        reconcile_events, reconcile_intents,
    )
    dsn = env.get("RIG_MEMORY_OS_DSN") or _dsn_from_env(env)
    own_conn = conn is None
    conn = conn or psycopg.connect(dsn, autocommit=True)
    reports = []
    try:
        p = env.get("RIG_MEMORY_OS_RECONCILE_INTENTS")
        if p and os.path.exists(p):
            reports.append(reconcile_intents.__doc__ and
                           reconcile_intents(Path(p), writer, conn))
        p = env.get("RIG_MEMORY_OS_RECONCILE_EVENTS")
        if p and os.path.exists(p):
            reports.append(reconcile_events(Path(p), writer, conn))
        p = env.get("RIG_MEMORY_OS_RECONCILE_RECEIPTS")
        if p and os.path.exists(p):
            reports.append(reconcile_effect_receipts(
                Path(p), writer, conn))
        p = env.get("RIG_MEMORY_OS_RECONCILE_CHECKPOINTS")
        if p and os.path.exists(p):
            reports.append(reconcile_checkpoints(
                Path(p), writer, conn))
    finally:
        if own_conn:
            conn.close()
    for r in reports:
        if r is not None:
            logger.info("reconcile %s: scanned=%d written=%d skipped=%d errors=%d",
                        r.source, r.scanned, r.written, r.skipped_duplicate, r.errors)


def main(argv=None):
    global _shutdown
    p = argparse.ArgumentParser(description="RIG Memory OS 24/7 daemon")
    p.add_argument("--interval", type=int, default=300,
                   help="Cycle interval in seconds (default 300)")
    p.add_argument("--once", action="store_true",
                   help="Run one cycle and exit")
    p.add_argument("--no-reconcile", action="store_true",
                   help="Skip reconcile step")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    from founder_runtime.runtime import MemoryOSRuntime

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Memory OS daemon starting (interval=%ds)", args.interval)
    rt = MemoryOSRuntime.from_env()
    dsn_masked = "***" + rt.cockpit.__class__.__name__
    logger.info("Memory OS daemon started (cockpit=%s)", dsn_masked)

    # Phase 7: pooled connection for the daemon's lifetime.
    # Only created when reconcile is active and not in --once mode,
    # which opens/closes its own short-lived connection.
    pool_conn = None
    if not args.no_reconcile and not args.once:
        try:
            dsn = _dsn_from_env(os.environ)
            import psycopg
            pool_conn = psycopg.connect(dsn, autocommit=True)
            logger.info("Reconcile connection pooled (Phase 7)")
        except Exception:
            logger.warning("Failed to create pooled connection; falling back to per-cycle")

    try:
        while not _shutdown:
            try:
                # Intent expiry
                expired = rt.intent.expire_overdue()
                if expired:
                    logger.info("expired %d intents", len(expired))

                # Cockpit snapshot
                snap = rt.cockpit.snapshot()
                logger.info("cockpit state=%s budget=%.3f panels=%d",
                            snap.control_state.value, snap.budget_remaining,
                            len(snap.panels))

                # Reconcile (pooled conn in daemon mode, fresh in --once)
                if not args.no_reconcile:
                    _reconcile_cycle(rt.postgres_writer, os.environ, conn=pool_conn)

            except Exception:
                logger.exception("cycle error (continuing)")

            if args.once:
                break
            # Interruptible sleep
            for _ in range(args.interval):
                if _shutdown:
                    break
                time.sleep(1)
    finally:
        if pool_conn is not None:
            pool_conn.close()
        rt.close()
        logger.info("Memory OS daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
