"""Install + manage the founder-runtime worker via launchd.

Idempotent. Generates a per-host plist from the template, places it in
~/Library/LaunchAgents/, and (with --load) registers it with launchctl.

Usage:
    uv run python -m founder_runtime.launchd_install [--load] [--unload]
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .store import DEFAULT_DB_PATH


TEMPLATE = Path(__file__).parent.parent / "services" / "macos" / "com.rig.founder-worker.plist.template"
RUNTIME_DIR = Path(__file__).parent.parent.resolve()
LOG_DIR = Path.home() / ".rig" / "founder-runtime" / "logs"


def host_shortname() -> str:
    """e.g. 'rig-control-128gb' for RIG128GBs-MacBook-Pro.local"""
    name = platform.node()
    if name.endswith(".local"):
        name = name[:-len(".local")]
    return name.lower().replace(" ", "-")


def render_plist(node_id: str, db_path: Path = DEFAULT_DB_PATH,
                 runtime_dir: Path = RUNTIME_DIR, log_dir: Path = LOG_DIR) -> str:
    raw = TEMPLATE.read_text(encoding="utf-8")
    return (raw
            .replace("__NODE_ID__", node_id)
            .replace("__RUNTIME_DIR__", str(runtime_dir))
            .replace("__RUNTIME_DB__", str(db_path))
            .replace("__LOG_DIR__", str(log_dir)))


def label(node_id: str) -> str:
    return f"com.rig.founder-worker.{node_id}"


def plist_path(node_id: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label(node_id)}.plist"


def install(node_id: str, *, load: bool = False, unload: bool = False) -> dict[str, str]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pp = plist_path(node_id)
    pp.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_plist(node_id)
    pp.write_text(rendered, encoding="utf-8")

    result = {"node_id": node_id, "plist_path": str(pp), "label": label(node_id)}

    if unload:
        r = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label(node_id)}"],
                           capture_output=True, text=True)
        result["unload_stdout"] = r.stdout.strip()
        result["unload_stderr"] = r.stderr.strip()
        result["unload_rc"] = str(r.returncode)

    if load:
        r = subprocess.run(["launchctl", "load", "-w", str(pp)], capture_output=True, text=True)
        result["load_stdout"] = r.stdout.strip()
        result["load_stderr"] = r.stderr.strip()
        result["load_rc"] = str(r.returncode)
        # Start now (in case launchd hasn't picked it up via RunAtLoad)
        r2 = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label(node_id)}"],
                            capture_output=True, text=True)
        result["kickstart_rc"] = str(r2.returncode)

    return result


def status(node_id: str) -> dict[str, str]:
    r = subprocess.run(["launchctl", "list", label(node_id)], capture_output=True, text=True)
    return {"list_stdout": r.stdout.strip(), "list_stderr": r.stderr.strip(), "rc": str(r.returncode)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default=host_shortname(),
                   help="node identity (default: auto-detected short hostname)")
    p.add_argument("--load", action="store_true", help="load + start via launchctl")
    p.add_argument("--unload", action="store_true", help="unload via launchctl first")
    p.add_argument("--status", action="store_true", help="report current launchd state")
    p.add_argument("--print", action="store_true", help="print rendered plist to stdout")
    args = p.parse_args(argv)

    if args.print:
        print(render_plist(args.node_id))
        return 0

    if args.status:
        print(status(args.node_id))
        return 0

    out = install(args.node_id, load=args.load, unload=args.unload)
    for k, v in out.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())