"""Phase 4 — Fleet probe + status refresh.

Probes each registered node's TCP reachability and writes the actual
status back to the runtime database. Idempotent.

Usage:
    uv run python -m founder_runtime.fleet_probe [--probe]
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import (
    Store,
    DEFAULT_DB_PATH,
    init_db,
    list_nodes,
    register_node,
)


def probe_tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, OSError):
        return False


def probe_node(node: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    """Probe one node over its tailnet address on common ports."""
    addr = node.get("tailnet_address") or node.get("lan_address")
    open_ports: list[int] = []
    if addr:
        for port in (22, 11434, 18765, 2222, 3000, 8088):
            if probe_tcp(addr, port, timeout=timeout):
                open_ports.append(port)
    return {
        "node_id": node["node_id"],
        "hostname": node["hostname"],
        "tailnet_address": addr,
        "reachable": bool(open_ports),
        "open_ports": open_ports,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }


def refresh_fleet(store: Store, *, timeout: float = 3.0) -> dict[str, Any]:
    """Probe all registered nodes and update statuses."""
    nodes = list_nodes(store)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(probe_node, n, timeout): n for n in nodes}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                nid = futures[fut].get("node_id")
                results.append({"node_id": nid, "error": str(exc), "reachable": False, "open_ports": []})

    updated = 0
    for r in results:
        nid = r.get("node_id")
        if not nid:
            continue
        if r.get("error"):
            new_status = "OFFLINE_UNVERIFIED"
        elif r.get("reachable"):
            new_status = "ONLINE"
        else:
            new_status = "OFFLINE_UNVERIFIED"

        existing = [n for n in list_nodes(store) if n["node_id"] == nid]
        if not existing:
            continue
        n = existing[0]
        n["status"] = new_status
        n["health_details"] = {
            "reachable": r.get("reachable", False),
            "open_ports": r.get("open_ports", []),
            "probed_at": r.get("probed_at"),
            "tailnet_address": r.get("tailnet_address"),
        }
        register_node(store, n)
        updated += 1

    return {
        "probed": len(results),
        "updated": updated,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true", help="actually probe TCP")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH))
    p.add_argument("--timeout", type=float, default=3.0)
    args = p.parse_args(argv)

    store = Store(args.db)
    if not Path(args.db).exists():
        migration = Path(__file__).parent.parent / "migrations" / "001_founder_runtime.sql"
        init_db(store, migration)

    if args.probe:
        out = refresh_fleet(store, timeout=args.timeout)
    else:
        out = {"dry_run": True, "nodes": list_nodes(store)}
    print(json.dumps(out, indent=2, default=str))
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())