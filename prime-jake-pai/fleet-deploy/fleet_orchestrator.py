#!/usr/bin/env python3
"""
Prime Jake — Fleet Orchestrator (24/7)

Top-level orchestration of the full RIG fleet. Runs every minute, dispatching
work to the right node, monitoring health, and keeping all systems operational.

This is the "systems that build the systems" layer — it manages:
1. Fleet node health monitoring (all 7 nodes)
2. Model serving (vLLM instances across GPUs)
3. Training pipeline (continuous LoRA updates)
4. Data collection (scrapers, fermenter, death trails)
5. Prime Agent daemon (always-on Jake PAI)
6. Business intelligence collection
7. QNAP storage management
8. Alerting and auto-recovery

Usage:
    python3 fleet_orchestrator.py [--daemon] [--interval 60]
    python3 fleet_orchestrator.py --daemon  # runs forever, 60s intervals
"""
from __future__ import annotations
import os, sys, json, subprocess, time, argparse, socket, platform
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

FLEET_SCRIPT = Path.home() / ".rig" / "scripts" / "prime-jake-fleet.py"
CONTROLLER = Path.home() / ".rig" / "scripts" / "prime-jake-controller.py"
QNAP_BRIDGE = Path.home() / ".rig" / "scripts" / "qnap-bridge.py"
FT_DIR = Path.home() / "rig-ft"
LOG_FILE = Path.home() / ".rig" / "logs" / "fleet_orchestrator.jsonl"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Services to monitor
SERVICES = {
    "prime-agent": {"check": "prime-agent status", "restart": "prime-agent --offline -p --no-tools 'ok'"},
    "vllm-daily": {"check": "curl -s http://192.168.68.90:8001/v1/models", "port": 8001},
    "vllm-kat": {"check": "curl -s http://localhost:8003/v1/models", "port": 8003},
    "model-router": {"check": "curl -s http://localhost:8010/health", "port": 8010},
    "ollama": {"check": "curl -s http://localhost:11434/api/tags", "port": 11434},
    "watchdog-timer": {"check": "systemctl --user is-active prime-agent-watchdog.timer"},
}

# Fleet nodes to monitor
NODES = ["blackwell", "rig-96gb", "rig-256gb", "rig-36gb", "rig-qnap"]

def log_event(event: dict):
    """Log an event to the orchestrator log."""
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")
    print(f"[{event['timestamp']}] {event.get('type', 'info')}: {event.get('message', '')}")

def check_service(name: str, config: dict) -> dict:
    """Check if a service is healthy."""
    try:
        result = subprocess.run(config["check"], shell=True, capture_output=True, text=True, timeout=10)
        is_healthy = result.returncode == 0 and bool(result.stdout.strip())
        return {"name": name, "healthy": is_healthy, "output": result.stdout[:200]}
    except Exception as e:
        return {"name": name, "healthy": False, "output": str(e)[:200]}

def check_fleet_node(node: str) -> dict:
    """Check if a fleet node is reachable."""
    try:
        result = subprocess.run(
            ["python3", str(FLEET_SCRIPT), "exec", "hostname", "--nodes", node],
            capture_output=True, text=True, timeout=15
        )
        is_healthy = result.returncode == 0 and "✓" in result.stdout
        return {"node": node, "healthy": is_healthy, "output": result.stdout[:200]}
    except Exception as e:
        return {"node": node, "healthy": False, "output": str(e)[:200]}

def check_gpu() -> dict:
    """Check GPU status."""
    try:
        result = subprocess.run(
            "nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv,noheader",
            shell=True, capture_output=True, text=True, timeout=10
        )
        gpus = []
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "index": int(parts[0]),
                    "memory_used_mb": int(parts[1]),
                    "memory_free_mb": int(parts[2]),
                    "utilization_pct": int(parts[3]),
                    "temp_c": int(parts[4]),
                })
        return {"gpus": gpus, "healthy": len(gpus) > 0}
    except Exception as e:
        return {"gpus": [], "healthy": False, "error": str(e)}

def check_training() -> dict:
    """Check if training is running."""
    try:
        result = subprocess.run("ps aux | grep axolotl | grep -v grep", shell=True, capture_output=True, text=True, timeout=5)
        is_running = bool(result.stdout.strip())
        
        # Check latest training log
        latest_log = None
        latest_loss = None
        for log_file in sorted(FT_DIR.glob("training-r*.log"), reverse=True):
            if log_file.exists():
                latest_log = log_file.name
                # Get last loss
                result = subprocess.run(f"grep \"'loss'\" {log_file} | tail -1", shell=True, capture_output=True, text=True, timeout=5)
                if result.stdout:
                    import re
                    m = re.search(r"'loss': '([\d.]+)'", result.stdout)
                    if m:
                        latest_loss = float(m.group(1))
                break
        
        return {"running": is_running, "latest_log": latest_log, "latest_loss": latest_loss}
    except:
        return {"running": False}

def check_disk() -> dict:
    """Check disk space."""
    try:
        result = subprocess.run("df -h / | tail -1", shell=True, capture_output=True, text=True, timeout=5)
        parts = result.stdout.strip().split()
        return {"total": parts[1], "used": parts[2], "available": parts[3], "use_pct": parts[4]}
    except:
        return {}

def run_cycle():
    """Run one orchestration cycle."""
    cycle_start = time.time()
    timestamp = datetime.now(timezone.utc).isoformat()
    
    # 1. Check all services
    service_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(check_service, name, config): name for name, config in SERVICES.items()}
        for f in as_completed(futures):
            r = f.result()
            service_results[r["name"]] = r
    
    unhealthy_services = [n for n, r in service_results.items() if not r["healthy"]]
    
    # 2. Check fleet nodes
    node_results = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(check_fleet_node, node): node for node in NODES}
        for f in as_completed(futures):
            r = f.result()
            node_results[r["node"]] = r
    
    unhealthy_nodes = [n for n, r in node_results.items() if not r["healthy"]]
    
    # 3. Check GPU
    gpu_status = check_gpu()
    
    # 4. Check training
    training_status = check_training()
    
    # 5. Check disk
    disk_status = check_disk()
    
    # 6. Log cycle
    cycle = {
        "type": "fleet_cycle",
        "timestamp": timestamp,
        "services": {n: r["healthy"] for n, r in service_results.items()},
        "nodes": {n: r["healthy"] for n, r in node_results.items()},
        "gpu": gpu_status,
        "training": training_status,
        "disk": disk_status,
        "unhealthy_services": unhealthy_services,
        "unhealthy_nodes": unhealthy_nodes,
        "cycle_duration_ms": int((time.time() - cycle_start) * 1000),
    }
    log_event(cycle)
    
    # 7. Auto-recovery for unhealthy services
    if unhealthy_services:
        for svc in unhealthy_services:
            if svc == "model-router":
                log_event({"type": "recovery", "message": f"Restarting {svc}..."})
                subprocess.Popen(["python3", str(Path.home() / ".rig/scripts/model_router.py"), "serve", "--port", "8010"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(2)
            elif svc == "prime-agent":
                log_event({"type": "recovery", "message": "Prime-agent down, watchdog should recover..."})
    
    # 8. Print status
    print(f"\n{'='*60}")
    print(f"FLEET ORCHESTRATOR — {timestamp}")
    print(f"{'='*60}")
    
    print(f"\nServices:")
    for name, healthy in cycle["services"].items():
        status = "✅" if healthy else "❌"
        print(f"  {status} {name}")
    
    print(f"\nFleet Nodes:")
    for name, healthy in cycle["nodes"].items():
        status = "✅" if healthy else "❌"
        print(f"  {status} {name}")
    
    print(f"\nGPU Status:")
    for gpu in gpu_status.get("gpus", []):
        print(f"  GPU {gpu['index']}: {gpu['memory_used_mb']}MB used / {gpu['memory_free_mb']}MB free | {gpu['utilization_pct']}% util | {gpu['temp_c']}°C")
    
    print(f"\nTraining: {'✅ running' if training_status['running'] else '⏸️ idle'}")
    if training_status.get("latest_loss"):
        print(f"  Latest loss: {training_status['latest_loss']}")
    if training_status.get("latest_log"):
        print(f"  Log: {training_status['latest_log']}")
    
    print(f"\nDisk: {disk_status.get('used', '?')} / {disk_status.get('total', '?')} ({disk_status.get('use_pct', '?')})")
    
    if unhealthy_services:
        print(f"\n⚠️  Unhealthy services: {', '.join(unhealthy_services)}")
    if unhealthy_nodes:
        print(f"⚠️  Unhealthy nodes: {', '.join(unhealthy_nodes)}")
    
    print(f"\nCycle time: {cycle['cycle_duration_ms']}ms")

def main():
    parser = argparse.ArgumentParser(description="Prime Jake Fleet Orchestrator")
    parser.add_argument("--daemon", action="store_true", help="Run forever")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles")
    args = parser.parse_args()
    
    print("Prime Jake — Fleet Orchestrator")
    print(f"Mode: {'daemon (24/7)' if args.daemon else 'single cycle'}")
    print(f"Interval: {args.interval}s")
    
    if args.daemon:
        while True:
            try:
                run_cycle()
            except Exception as e:
                log_event({"type": "error", "message": f"Cycle error: {e}"})
            time.sleep(args.interval)
    else:
        run_cycle()

if __name__ == "__main__":
    main()
