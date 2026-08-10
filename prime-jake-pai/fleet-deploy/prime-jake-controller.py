#!/usr/bin/env python3
"""
Prime Jake — Program & Session Controller

Manages open programs, terminals, coding sessions, and agent processes
across the local machine and all fleet nodes.

Usage:
    python3 prime-jake-controller.py local-programs          # List local programs
    python3 prime-jake-controller.py local-sessions           # List local tmux/terminal sessions
    python3 prime-jake-controller.py fleet-sessions           # List sessions on all nodes
    python3 prime-jake-controller.py start <program>          # Start a program locally
    python3 prime-jake-controller.py start-fleet <program>    # Start on all nodes
    python3 prime-jake-controller.py stop <pattern>           # Stop a program locally (Gate-D)
    python3 prime-jake-controller.py stop-fleet <pattern>     # Stop on all nodes (Gate-D)
    python3 prime-jake-controller.py tmux-new <name> <cmd>    # Create a tmux session
    python3 prime-jake-controller.py tmux-send <name> <cmd>   # Send command to tmux session
    python3 prime-jake-controller.py tmux-list                # List local tmux sessions
    python3 prime-jake-controller.py prime-agent-status       # Check prime-agent on all nodes
    python3 prime-jake-controller.py health                   # Full system health
"""
from __future__ import annotations
import os, sys, json, subprocess, time, socket, platform
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

FLEET_SCRIPT = Path.home() / ".rig" / "scripts" / "prime-jake-fleet.py"
INVENTORY_PATH = Path.home() / ".rig" / "mesh" / "fleet-inventory.json"

def load_inventory():
    with open(INVENTORY_PATH) as f:
        return json.load(f)

# ── Local Programs ──────────────────────────────────────────────────────────
INTERESTING_PROGRAMS = [
    "prime-agent", "hermes", "ollama", "vllm", "node", "python3",
    "claude", "codex", "tmux", "cursor", "code", "docker",
    "prometheus", "grafana", "postgres", "redis", "nginx",
    "tailscale", "ssh", "litellm", "windmill"
]

def list_local_programs():
    """List interesting programs running locally."""
    print("\nLocal running programs:")
    print("─" * 80)
    result = subprocess.run(
        "ps aux | grep -iE '" + "|".join(INTERESTING_PROGRAMS) + "' | grep -v grep | awk '{print $2, $11, $12}' | sort -u",
        shell=True, capture_output=True, text=True
    )
    if result.stdout.strip():
        seen = set()
        for line in result.stdout.strip().split("\n"):
            parts = line.split(None, 2)
            if len(parts) >= 2:
                proc = parts[1] if len(parts) < 3 else parts[1]
                if proc not in seen:
                    seen.add(proc)
                    pid = parts[0]
                    cmd = " ".join(parts[1:]) if len(parts) > 1 else ""
                    print(f"  PID {pid:<8} {cmd}")
    else:
        print("  (no interesting programs found)")

    # Also count total processes
    total = subprocess.run("ps aux | wc -l", shell=True, capture_output=True, text=True)
    print(f"\n  Total processes: {total.stdout.strip()}")

def list_local_sessions():
    """List local terminal/coding sessions."""
    print("\nLocal sessions:")
    print("─" * 60)

    # tmux
    print("  tmux:")
    result = subprocess.run("tmux list-sessions 2>/dev/null", shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            print(f"    {line}")
    else:
        print("    (none)")

    # screen
    print("  screen:")
    result = subprocess.run("screen -ls 2>/dev/null", shell=True, capture_output=True, text=True)
    if result.stdout.strip() and "No Sockets" not in result.stdout:
        for line in result.stdout.strip().split("\n"):
            if line.strip() and "Sockets" not in line:
                print(f"    {line.strip()}")
    else:
        print("    (none)")

    # prime-agent
    print("  prime-agent:")
    result = subprocess.run("prime-agent status 2>/dev/null", shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            print(f"    {line}")
    else:
        print("    (not running)")

    # systemd user services
    print("  systemd user services (active):")
    result = subprocess.run(
        "systemctl --user list-units --type=service --state=running --no-legend 2>/dev/null | awk '{print $1}'",
        shell=True, capture_output=True, text=True
    )
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            print(f"    {line}")
    else:
        print("    (none)")

    # docker containers
    print("  docker containers:")
    result = subprocess.run("docker ps --format '{{.Names}}: {{.Status}}' 2>/dev/null", shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            print(f"    {line}")
    else:
        print("    (none)")

# ── Fleet Sessions ──────────────────────────────────────────────────────────
def list_fleet_sessions():
    """List sessions on all fleet nodes."""
    cmd = """echo "=== tmux ==="; tmux list-sessions 2>/dev/null || echo "none"; echo "=== prime-agent ==="; prime-agent status 2>/dev/null || echo "not installed"; echo "=== docker ==="; docker ps --format '{{.Names}}: {{.Status}}' 2>/dev/null || echo "none\""""
    subprocess.run([sys.executable, str(FLEET_SCRIPT), "exec", cmd], cwd=os.getcwd())

# ── Start/Stop ──────────────────────────────────────────────────────────────
def start_program(program: str):
    """Start a program locally in the background."""
    print(f"Starting: {program}")
    try:
        subprocess.Popen(program, shell=True, start_new_session=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        # Verify it started
        result = subprocess.run(f"pgrep -f '{program.split()[0]}'", shell=True, capture_output=True, text=True)
        if result.stdout.strip():
            print(f"  ✓ Started (PID: {result.stdout.strip().split()[0]})")
        else:
            print(f"  ⚠ Process may not have started")
    except Exception as e:
        print(f"  ✗ {e}")

def start_fleet_program(program: str):
    """Start a program on all fleet nodes."""
    subprocess.run([sys.executable, str(FLEET_SCRIPT), "exec", f"nohup {program} &>/dev/null &"], cwd=os.getcwd())

def stop_program(pattern: str, confirm: bool = False):
    """Stop a program locally. Gate-D protected."""
    print(f"⚠ Gate-D: Stopping processes matching '{pattern}'")
    if not confirm:
        print("  (dry run — use --confirm to execute)")
        result = subprocess.run(f"pgrep -af '{pattern}'", shell=True, capture_output=True, text=True)
        if result.stdout.strip():
            print("  Would kill:")
            for line in result.stdout.strip().split("\n"):
                print(f"    {line}")
        else:
            print("  No matching processes found")
        return
    print("  ⚠ EXECUTING — killing processes")
    subprocess.run(f"pkill -f '{pattern}'", shell=True)
    time.sleep(1)
    result = subprocess.run(f"pgrep -f '{pattern}'", shell=True, capture_output=True, text=True)
    if not result.stdout.strip():
        print("  ✓ All processes stopped")
    else:
        print(f"  ⚠ Some processes still running: {result.stdout.strip()}")

def stop_fleet_program(pattern: str, confirm: bool = False):
    """Stop a program on all fleet nodes. Gate-D protected."""
    if not confirm:
        print(f"⚠ Gate-D: Would kill '{pattern}' on all fleet nodes")
        print("  (dry run — use --confirm to execute)")
        subprocess.run([sys.executable, str(FLEET_SCRIPT), "exec", f"pgrep -af '{pattern}' || echo 'no match'"], cwd=os.getcwd())
        return
    subprocess.run([sys.executable, str(FLEET_SCRIPT), "exec", "--confirm" if False else "",
                    f"pkill -f '{pattern}' && echo 'killed' || echo 'no match'"], cwd=os.getcwd())

# ── tmux Management ─────────────────────────────────────────────────────────
def tmux_new(name: str, cmd: str = ""):
    """Create a new tmux session."""
    tmux_cmd = f"tmux new-session -d -s {name}"
    if cmd:
        tmux_cmd += f" '{cmd}'"
    subprocess.run(tmux_cmd, shell=True)
    print(f"✓ Created tmux session: {name}")
    # List sessions
    subprocess.run("tmux list-sessions", shell=True)

def tmux_send(name: str, cmd: str):
    """Send a command to a tmux session."""
    subprocess.run(f"tmux send-keys -t {name} '{cmd}' Enter", shell=True)
    print(f"✓ Sent to tmux:{name}: {cmd}")

def tmux_list():
    """List local tmux sessions."""
    result = subprocess.run("tmux list-sessions 2>/dev/null", shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        print("\ntmux sessions:")
        for line in result.stdout.strip().split("\n"):
            print(f"  {line}")
    else:
        print("\nNo tmux sessions")

# ── Prime Agent Fleet Status ────────────────────────────────────────────────
def prime_agent_fleet_status():
    """Check prime-agent status on all nodes."""
    subprocess.run([sys.executable, str(FLEET_SCRIPT), "exec",
                    "prime-agent status 2>/dev/null || echo 'prime-agent not installed'"],
                   cwd=os.getcwd())

# ── Full Health ─────────────────────────────────────────────────────────────
def full_health():
    """Full system health check."""
    print("=" * 70)
    print(f"PRIME JAKE — SYSTEM HEALTH — {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Local system
    print(f"\nLocal host: {socket.gethostname()}")
    print(f"OS: {platform.system()} {platform.release()}")
    print(f"CPU cores: {os.cpu_count()}")

    # Load average
    try:
        loadavg = os.getloadavg()
        print(f"Load average: {loadavg[0]:.2f} {loadavg[1]:.2f} {loadavg[2]:.2f}")
    except:
        pass

    # Memory
    try:
        with open("/proc/meminfo") as f:
            meminfo = dict(line.split(":", 1) for line in f if ":" in line)
        total = int(meminfo.get("MemTotal", "0").strip()) // 1024
        avail = int(meminfo.get("MemAvailable", "0").strip()) // 1024
        used = total - avail
        print(f"Memory: {used//1024}GB / {total//1024}GB ({avail//1024}GB free)")
    except:
        pass

    # Disk
    result = subprocess.run("df -h / | tail -1", shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        print(f"Disk: {result.stdout.strip()}")

    # GPU
    result = subprocess.run("nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null", shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        print(f"\nGPU:")
        for line in result.stdout.strip().split("\n"):
            print(f"  {line}")

    # Local programs
    list_local_programs()

    # Local sessions
    list_local_sessions()

    # Fleet status
    print(f"\n{'=' * 70}")
    print("FLEET STATUS")
    print("=" * 70)
    subprocess.run([sys.executable, str(FLEET_SCRIPT), "status"], cwd=os.getcwd())

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Prime Jake Program & Session Controller")
    parser.add_argument("command", choices=[
        "local-programs", "local-sessions", "fleet-sessions",
        "start", "start-fleet", "stop", "stop-fleet",
        "tmux-new", "tmux-send", "tmux-list",
        "prime-agent-status", "health"
    ])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--confirm", action="store_true", help="confirm Gate-D actions")
    args = parser.parse_args()

    if args.command == "local-programs":
        list_local_programs()
    elif args.command == "local-sessions":
        list_local_sessions()
    elif args.command == "fleet-sessions":
        list_fleet_sessions()
    elif args.command == "start":
        start_program(" ".join(args.args))
    elif args.command == "start-fleet":
        start_fleet_program(" ".join(args.args))
    elif args.command == "stop":
        stop_program(" ".join(args.args), args.confirm)
    elif args.command == "stop-fleet":
        stop_fleet_program(" ".join(args.args), args.confirm)
    elif args.command == "tmux-new":
        name = args.args[0] if args.args else "jake"
        cmd = " ".join(args.args[1:]) if len(args.args) > 1 else ""
        tmux_new(name, cmd)
    elif args.command == "tmux-send":
        if len(args.args) < 2:
            print("Usage: tmux-send <session-name> <command>")
            sys.exit(1)
        tmux_send(args.args[0], " ".join(args.args[1:]))
    elif args.command == "tmux-list":
        tmux_list()
    elif args.command == "prime-agent-status":
        prime_agent_fleet_status()
    elif args.command == "health":
        full_health()

if __name__ == "__main__":
    main()
