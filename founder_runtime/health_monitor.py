"""Phase 4 — Fleet health monitor (daemon-like loop).

Runs `fleet_probe --probe` and `dispatch_tick` on a fixed cadence so the
runtime keeps node statuses fresh and the queue drained without external
cron.

Usage:
    uv run python -m founder_runtime.health_monitor [--interval 60] [--once]

The launchd-managed worker handles leases/heartbeats for one node.
The health monitor is separate and runs on the control plane: it keeps
node registry truth and pumps the queue.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .store import (
    Store,
    DEFAULT_DB_PATH,
    init_db,
    append_audit,
)
from .fleet_probe import refresh_fleet
from .dispatcher import dispatch_tick


logger = logging.getLogger("founder_runtime.health_monitor")


def _migration_path() -> Path:
    return Path(__file__).parent.parent / "migrations" / "001_founder_runtime.sql"


def run_once(store: Store) -> dict[str, object]:
    probe = refresh_fleet(store, timeout=3.0)
    tick = dispatch_tick(store)
    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "probed_nodes": probe.get("probed", 0),
        "probe_updated": probe.get("updated", 0),
        "items_leased": tick.get("items_leased", 0),
        "expired_leases_recovered": tick.get("expired_leases_recovered", 0),
        "stale_nodes_marked": tick.get("stale_nodes_marked", 0),
        "queue": tick.get("queue", {}),
        "healthy_nodes": tick.get("healthy_nodes", 0),
    }
    append_audit(store, actor="health_monitor", action="tick",
                 target=None, detail=summary)
    return summary


def run_forever(interval_seconds: float = 60.0) -> None:
    store = Store(DEFAULT_DB_PATH)
    if not Path(DEFAULT_DB_PATH).exists():
        init_db(store, _migration_path())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("health monitor started; interval=%ss", interval_seconds)
    try:
        while True:
            try:
                summary = run_once(store)
                logger.info(
                    "tick: probed=%s leased=%s recovered=%s queue=%s",
                    summary["probed_nodes"], summary["items_leased"],
                    summary["expired_leases_recovered"], summary["queue"],
                )
            except Exception as exc:
                logger.exception("tick failed: %s", exc)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=60.0,
                   help="seconds between ticks (default 60)")
    p.add_argument("--once", action="store_true",
                   help="run one tick and exit (for cron integration)")
    args = p.parse_args(argv)

    if args.once:
        store = Store(DEFAULT_DB_PATH)
        if not Path(DEFAULT_DB_PATH).exists():
            init_db(store, _migration_path())
        print(json.dumps(run_once(store), indent=2, default=str))
        store.close()
        return 0

    run_forever(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())