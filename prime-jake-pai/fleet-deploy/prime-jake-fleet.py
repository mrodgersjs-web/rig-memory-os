#!/usr/bin/env python3
"""
Prime Jake — Fleet Control Plane

Run commands across all RIG fleet nodes, manage processes, terminals,
coding sessions, and monitor node health. This is Jake's hands on the fleet.

Usage:
    python3 prime-jake-fleet.py status              # Fleet health snapshot
    python3 prime-jake-fleet.py exec "uptime"        # Run command on all online nodes
    python3 prime-jake-fleet.py exec "uptime" --nodes blackwell,rig-96gb
    python3 prime-jake-fleet.py programs             # List running programs per node
    python3 prime-jake-fleet.py sessions             # List tmux/coding sessions per node
    python3 prime-jake-fleet.py deploy <local-file> <remote-path>  # SCP to all nodes
    python3 prime-jake-fleet.py models               # List loaded models per node
    python3 prime-jake-fleet.py route <model-hint>   # Find which node has a model loaded
    python3 prime-jake-fleet.py kill <pattern>       # Kill process by pattern on all nodes
    python3 prime-jake-fleet.py tailscale            # Tailscale network status
"""
from __future__ import annotations
import json, os, sys, time, subprocess, argparse, platform, socket
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
INVENTORY_PATH = Path.home() / ".rig" / "mesh" / "fleet-inventory.json"
SSH_KEY = os.path.expanduser("~/.ssh/rig_id_ed25519")
SSH_TIMEOUT = 10

def load_inventory() -> dict:
    with open(INVENTORY_PATH) as f:
        return json.load(f)

def get_nodes(inv: dict) -> dict:
    return inv.get("nodes", {})

def local_node_name(inv: dict) -> str:
    return inv.get("local_node", "blackwell")

def ssh_key_for(node_cfg: dict) -> str:
    return os.path.expanduser(node_cfg.get("ssh_key", SSH_KEY))

# ── Data ────────────────────────────────────────────────────────────────────
@dataclass
class NodeResult:
    name: str
    success: bool
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    error: str = ""

# ── Execution ───────────────────────────────────────────────────────────────
def is_local(node_name: str, inv: dict) -> bool:
    local = local_node_name(inv)
    if node_name == local:
        return True
    node_cfg = get_nodes(inv).get(node_name, {})
    return node_cfg.get("is_local", False)

def exec_on_node(node_name: str, cmd: str, inv: dict, timeout: int = SSH_TIMEOUT) -> NodeResult:
    nodes = get_nodes(inv)
    if node_name not in nodes:
        return NodeResult(node_name, False, error=f"unknown node")
    cfg = nodes[node_name]
    if cfg.get("status") in ("OFFLINE", "OFFLINE_UNVERIFIED"):
        return NodeResult(node_name, False, error=f"node offline")
    t0 = time.time()
    try:
        if is_local(node_name, inv):
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return NodeResult(node_name, result.returncode == 0, result.stdout, result.stderr,
                            int((time.time() - t0) * 1000))
        ip = cfg.get("lan_ip") or cfg.get("tailscale_ip")
        user = cfg["user"]
        key = ssh_key_for(cfg)
        ssh_cmd = ["ssh", "-i", key, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                   "-o", "BatchMode=yes", f"{user}@{ip}", cmd]
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        return NodeResult(node_name, result.returncode == 0, result.stdout, result.stderr,
                        int((time.time() - t0) * 1000))
    except subprocess.TimeoutExpired:
        return NodeResult(node_name, False, error="timeout", duration_ms=int((time.time()-t0)*1000))
    except Exception as e:
        return NodeResult(node_name, False, error=str(e), duration_ms=int((time.time()-t0)*1000))

def exec_fleet(cmd: str, inv: dict, nodes_filter: Optional[List[str]] = None, timeout: int = SSH_TIMEOUT) -> List[NodeResult]:
    nodes = get_nodes(inv)
    targets = nodes_filter if nodes_filter else list(nodes.keys())
    results = []
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {pool.submit(exec_on_node, n, cmd, inv, timeout): n for n in targets}
        for f in as_completed(futures):
            results.append(f.result())
    # Sort by node name for consistent output
    order = list(nodes.keys())
    results.sort(key=lambda r: order.index(r.name) if r.name in order else 999)
    return results

# ── Commands ────────────────────────────────────────────────────────────────
def cmd_status(inv: dict):
    """Fleet health snapshot."""
    print(f"\n{'Node':<18} {'Status':<12} {'IP':<18} {'RAM':<6} {'Role':<25} {'Models'}")
    print("─" * 110)
    for name, cfg in get_nodes(inv).items():
        status = cfg.get("status", "?")
        ip = cfg.get("lan_ip") or cfg.get("tailscale_ip", "?")
        ram = f"{cfg.get('ram_gb', '?')}GB"
        role = cfg.get("role", "?")
        models = len(cfg.get("models", []))
        marker = " ★" if cfg.get("is_local") or name == local_node_name(inv) else ""
        print(f"{name+marker:<18} {status:<12} {ip:<18} {ram:<6} {role:<25} {models} models")

    # Live probe online nodes
    print(f"\n{'─'*60}")
    print("Live probe:")
    results = exec_fleet("hostname && uptime | sed 's/.*load average: //' || echo 'probe failed'", inv, timeout=8)
    for r in results:
        if r.success:
            lines = r.stdout.strip().split("\n")
            host = lines[0] if lines else "?"
            load = lines[1] if len(lines) > 1 else "?"
            print(f"  {r.name:<18} ✓ {host:<35} load: {load}")
        else:
            print(f"  {r.name:<18} ✗ {r.error}")

def cmd_exec(cmd: str, inv: dict, nodes_filter: Optional[List[str]] = None):
    """Run command on all (or subset of) nodes."""
    print(f"\n▶ Executing on fleet: {cmd}\n")
    results = exec_fleet(cmd, inv, nodes_filter)
    for r in results:
        print(f"{'─'*60}")
        print(f"〔{r.name}〕{'✓' if r.success else '✗'} ({r.duration_ms}ms)")
        if r.stdout.strip():
            print(r.stdout.rstrip())
        if r.stderr.strip():
            print(f"  ⚠ {r.stderr.rstrip()}")
        if r.error:
            print(f"  ⚠ {r.error}")
    success = sum(1 for r in results if r.success)
    print(f"\n{'─'*60}\n{success}/{len(results)} nodes succeeded.")

def cmd_programs(inv: dict):
    """List running programs per node."""
    cmd = "ps aux | grep -iE 'prime-agent|hermes|ollama|vllm|node|python|claude|codex|tmux' | grep -v grep | awk '{print $11}' | sort -u | head -20"
    cmd_exec(cmd, inv)

def cmd_sessions(inv: dict):
    """List tmux/coding sessions per node."""
    cmd = "echo 'tmux:'; tmux list-sessions 2>/dev/null || echo '  none'; echo 'screen:'; screen -ls 2>/dev/null || echo '  none'; echo 'prime-agent:'; prime-agent status 2>/dev/null || echo '  not installed'"
    cmd_exec(cmd, inv)

def cmd_models(inv: dict):
    """List loaded models per node."""
    print("\nLoaded models per node:")
    cmd = """echo "LM Studio:"; curl -s --connect-timeout 3 http://127.0.0.1:1234/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print([m['id'] for m in d.get('data',[])])" 2>/dev/null || echo "  not running"; echo "Ollama:"; curl -s --connect-timeout 3 http://127.0.0.1:11434/api/tags 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print([m['name'] for m in d.get('models',[])])" 2>/dev/null || echo "  not running"; echo "vLLM:"; curl -s --connect-timeout 3 http://127.0.0.1:8001/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print([m['id'] for m in d.get('data',[])])" 2>/dev/null || echo "  not running\""""
    cmd_exec(cmd, inv)

def cmd_route(model_hint: str, inv: dict):
    """Find which node has a model loaded."""
    print(f"\nSearching fleet for model: {model_hint}")
    cmd = f"""for ep in "http://127.0.0.1:1234/v1/models" "http://127.0.0.1:8001/v1/models" "http://127.0.0.1:11434/api/tags"; do curl -s --connect-timeout 3 "$ep" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); [print(m.get('id',m.get('name',''))) for m in d.get('data',d.get('models',[]))]" 2>/dev/null; done | grep -i "{model_hint}" || echo "not found\""""
    cmd_exec(cmd, inv)

def cmd_kill(pattern: str, inv: dict):
    """Kill process by pattern on all nodes."""
    print(f"\n⚠ Killing processes matching: {pattern}")
    print("⚠ Gate-D check: this is a destructive action. Requires Mike's approval.")
    print("⚠ To execute, run with --confirm")
    if "--confirm" not in sys.argv:
        print("  (dry run — no processes killed)")
        return
    cmd = f"pkill -f '{pattern}' && echo 'killed' || echo 'no match'"
    cmd_exec(cmd, inv)

def cmd_deploy(local_file: str, remote_path: str, inv: dict):
    """SCP a file to all online nodes."""
    key = SSH_KEY
    nodes = get_nodes(inv)
    print(f"\nDeploying {local_file} → {remote_path} on all online nodes:")
    for name, cfg in nodes.items():
        if cfg.get("status") in ("OFFLINE", "OFFLINE_UNVERIFIED"):
            print(f"  {name}: skipped (offline)")
            continue
        if is_local(name, inv):
            import shutil
            try:
                shutil.copy2(local_file, remote_path)
                print(f"  {name}: ✓ (local copy)")
            except Exception as e:
                print(f"  {name}: ✗ {e}")
            continue
        ip = cfg.get("lan_ip") or cfg.get("tailscale_ip")
        user = cfg["user"]
        node_key = ssh_key_for(cfg)
        scp_cmd = ["scp", "-i", node_key, "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                   local_file, f"{user}@{ip}:{remote_path}"]
        try:
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                print(f"  {name}: ✓")
            else:
                print(f"  {name}: ✗ {result.stderr.strip()}")
        except Exception as e:
            print(f"  {name}: ✗ {e}")

def cmd_tailscale():
    """Tailscale network status."""
    print("\nTailscale network:")
    subprocess.run("tailscale status", shell=True)

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Prime Jake Fleet Control Plane")
    parser.add_argument("command", choices=["status", "exec", "programs", "sessions", "models", "route", "kill", "deploy", "tailscale"])
    parser.add_argument("args", nargs="*", help="command arguments")
    parser.add_argument("--nodes", "-n", help="comma-separated node filter")
    parser.add_argument("--timeout", "-t", type=int, default=SSH_TIMEOUT)
    parser.add_argument("--confirm", action="store_true", help="confirm destructive actions (Gate-D)")
    args = parser.parse_args()

    inv = load_inventory()
    nodes_filter = args.nodes.split(",") if args.nodes else None

    if args.command == "status":
        cmd_status(inv)
    elif args.command == "exec":
        if not args.args:
            print("Error: exec requires a command argument")
            sys.exit(1)
        cmd_exec(" ".join(args.args), inv, nodes_filter)
    elif args.command == "programs":
        cmd_programs(inv)
    elif args.command == "sessions":
        cmd_sessions(inv)
    elif args.command == "models":
        cmd_models(inv)
    elif args.command == "route":
        if not args.args:
            print("Error: route requires a model hint")
            sys.exit(1)
        cmd_route(args.args[0], inv)
    elif args.command == "kill":
        if not args.args:
            print("Error: kill requires a process pattern")
            sys.exit(1)
        cmd_kill(args.args[0], inv)
    elif args.command == "deploy":
        if len(args.args) < 2:
            print("Error: deploy requires <local-file> <remote-path>")
            sys.exit(1)
        cmd_deploy(args.args[0], args.args[1], inv)
    elif args.command == "tailscale":
        cmd_tailscale()

if __name__ == "__main__":
    main()
