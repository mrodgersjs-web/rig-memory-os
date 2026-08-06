"""Operator CLI for the Founder Runtime.

Usage:
    founder-runtime init-db
    founder-runtime register-nodes [--config config/nodes.yaml]
    founder-runtime dispatch-tick
    founder-runtime founder-review
    founder-runtime morning-brief
    founder-runtime enqueue-signal --source-uri URL --summary "..."
    founder-runtime worker --node-id rig-36gb
    founder-runtime queue-status
    founder-runtime list-nodes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from .contracts import (
    NodeCapabilityContract,
    NodeStatus,
)
from .store import (
    Store,
    DEFAULT_DB_PATH,
    init_db,
    register_node,
    list_nodes,
    queue_metrics,
    enqueue_work_item,
)
from .dispatcher import dispatch_tick, enqueue_signal_research
from .founder_loop import founder_review, morning_brief, enqueue_mission
from .worker import Worker, make_signal_research_handler
from .verification import verify_and_seal


MIGRATION = Path(__file__).parent.parent / "migrations" / "001_founder_runtime.sql"


def _store(args: argparse.Namespace) -> Store:
    path = Path(args.db) if getattr(args, "db", None) else DEFAULT_DB_PATH
    s = Store(path)
    if getattr(args, "init", False) or not path.exists():
        init_db(s, MIGRATION)
    return s


def cmd_init_db(args: argparse.Namespace) -> int:
    s = _store(args)
    init_db(s, MIGRATION)
    print(f"initialized db at {s.path}")
    s.close()
    return 0


def cmd_register_nodes(args: argparse.Namespace) -> int:
    s = _store(args)
    if args.config:
        cfg = yaml.safe_load(Path(args.config).read_text())
        for n in cfg.get("nodes", []):
            register_node(s, n)
            print(f"  registered {n['node_id']}")
    else:
        # register a single local node
        register_node(s, NodeCapabilityContract(
            node_id="rig-control-128gb",
            hostname="control.local",
            capabilities=["dispatch", "verifier", "founder_review"],
            max_concurrency=2,
            status=NodeStatus.ONLINE,
        ).model_dump())
        print("  registered rig-control-128gb")
    print(f"{len(list_nodes(s))} nodes registered")
    s.close()
    return 0


def cmd_dispatch_tick(args: argparse.Namespace) -> int:
    s = _store(args)
    metrics = dispatch_tick(s)
    print(json.dumps(metrics, indent=2))
    s.close()
    return 0


def cmd_founder_review(args: argparse.Namespace) -> int:
    s = _store(args)
    print(json.dumps(founder_review(s), indent=2))
    s.close()
    return 0


def cmd_morning_brief(args: argparse.Namespace) -> int:
    s = _store(args)
    print(morning_brief(s))
    s.close()
    return 0


def cmd_queue_status(args: argparse.Namespace) -> int:
    s = _store(args)
    print(json.dumps(queue_metrics(s), indent=2))
    s.close()
    return 0


def cmd_list_nodes(args: argparse.Namespace) -> int:
    s = _store(args)
    for n in list_nodes(s):
        print(f"  {n['node_id']:<30} status={n['status']:<20} load={n['current_load']}/{n['max_concurrency']}")
    s.close()
    return 0


def cmd_enqueue_signal(args: argparse.Namespace) -> int:
    s = _store(args)
    item = enqueue_signal_research(
        s,
        source_uri=args.source_uri,
        source_type=args.source_type or "http",
        summary_seed=args.summary,
        objective=args.objective or f"Research signal: {args.source_uri}",
        priority=args.priority or 60,
    )
    print(f"  enqueued {item.work_item_id} (idempotency_key={item.idempotency_key})")
    s.close()
    return 0


def cmd_enqueue_mission(args: argparse.Namespace) -> int:
    s = _store(args)
    item = enqueue_mission(
        s,
        work_type=args.work_type,
        objective=args.objective,
        opportunity_id=args.opportunity_id,
        required_capabilities=args.capabilities.split(",") if args.capabilities else None,
        priority=args.priority or 60,
        approval_lane=args.approval_lane or "autonomous_local",
    )
    print(f"  enqueued mission {item.work_item_id}")
    s.close()
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = _store(args)
    # If --capabilities is provided, use those; else read from registry
    if args.capabilities:
        caps = args.capabilities.split(",")
    else:
        existing = [n for n in list_nodes(s) if n["node_id"] == args.node_id]
        caps = existing[0]["capabilities"] if existing else ["signal_research"]

    node = NodeCapabilityContract(
        node_id=args.node_id,
        hostname=args.hostname or f"{args.node_id}.local",
        capabilities=caps,
        max_concurrency=args.concurrency,
        lan_address=args.lan,
        tailnet_address=args.tailnet,
    )
    handlers = {
        "signal_research": make_signal_research_handler(),
    }
    Worker(s, node, handlers).run()
    s.close()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    s = _store(args)
    from .contracts import WorkResultContract, WorkResultStatus
    result = WorkResultContract(
        work_item_id=args.work_item_id,
        worker_id=args.worker_id or "unknown",
        status=WorkResultStatus(args.status) if args.status else WorkResultStatus.COMPLETED,
        summary=args.summary,
        artifact_paths=args.artifact.split(",") if args.artifact else [],
        source_refs=args.source_refs.split(",") if args.source_refs else [],
    )
    v = verify_and_seal(
        s,
        work_item_id=args.work_item_id,
        result=result,
        verifier_node=args.verifier_node or "verifier-local",
        verifier_model=args.verifier_model or "minimax-m3",
    )
    print(json.dumps(v.model_dump(mode="json"), indent=2))
    s.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="founder-runtime", description="RIG 24/7 Founder Runtime")
    p.add_argument("--db", help="override default db path")
    p.add_argument("--init", action="store_true", help="auto-init db before command")

    sp = p.add_subparsers(dest="cmd", required=True)

    for cmd, fn, help_text in [
        ("init-db", cmd_init_db, "Initialize the SQLite database"),
        ("register-nodes", cmd_register_nodes, "Register node workers"),
        ("dispatch-tick", cmd_dispatch_tick, "Run one dispatcher tick"),
        ("founder-review", cmd_founder_review, "Run Jake founder review"),
        ("morning-brief", cmd_morning_brief, "Print decision-dense morning brief"),
        ("queue-status", cmd_queue_status, "Queue metrics"),
        ("list-nodes", cmd_list_nodes, "List registered nodes"),
    ]:
        sub = sp.add_parser(cmd, help=help_text)
        if cmd == "register-nodes":
            sub.add_argument("--config", help="path to nodes.yaml (e.g. config/nodes.yaml)")
        sub.set_defaults(func=fn)

    # enqueue-signal
    sp_e = sp.add_parser("enqueue-signal", help="Enqueue a signal_research work item")
    sp_e.add_argument("--source-uri", required=True)
    sp_e.add_argument("--source-type", default="http")
    sp_e.add_argument("--summary", required=True)
    sp_e.add_argument("--objective")
    sp_e.add_argument("--priority", type=int)
    sp_e.set_defaults(func=cmd_enqueue_signal)

    # enqueue-mission
    sp_m = sp.add_parser("enqueue-mission", help="Enqueue a typed mission")
    sp_m.add_argument("--work-type", required=True)
    sp_m.add_argument("--objective", required=True)
    sp_m.add_argument("--opportunity-id")
    sp_m.add_argument("--capabilities", help="comma-separated")
    sp_m.add_argument("--priority", type=int)
    sp_m.add_argument("--approval-lane", choices=["autonomous_local", "mike_approval"])
    sp_m.set_defaults(func=cmd_enqueue_mission)

    # worker
    sp_w = sp.add_parser("worker", help="Run a persistent worker")
    sp_w.add_argument("--node-id", required=True)
    sp_w.add_argument("--hostname")
    sp_w.add_argument("--capabilities", help="comma-separated capability tags")
    sp_w.add_argument("--concurrency", type=int, default=2)
    sp_w.add_argument("--lan")
    sp_w.add_argument("--tailnet")
    sp_w.set_defaults(func=cmd_worker)

    # verify
    sp_v = sp.add_parser("verify", help="Independent verification + ProofPacket seal")
    sp_v.add_argument("--work-item-id", required=True)
    sp_v.add_argument("--worker-id", default="unknown")
    sp_v.add_argument("--summary", default="")
    sp_v.add_argument("--artifact", default="")
    sp_v.add_argument("--source-refs", default="")
    sp_v.add_argument("--status", choices=["COMPLETED", "FAILED"], default="COMPLETED")
    sp_v.add_argument("--verifier-node", default="verifier-local")
    sp_v.add_argument("--verifier-model", default="minimax-m3")
    sp_v.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())