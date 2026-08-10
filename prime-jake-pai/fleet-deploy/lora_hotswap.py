#!/usr/bin/env python3
"""
Prime Jake — Per-Node LoRA Hot-Swap

Each fleet node runs a different specialized LoRA adapter, chosen by a router
that predicts which specialty this task needs. Adapters are distributed across
the fleet and hot-swapped at inference time.

Specialties:
- blackwell: coding + RIG doctrine (primary)
- rig-96gb: synthesis + creative QA
- rig-256gb: strategy + long-context analysis
- rig-36gb: signal research + lead enrichment
- rig-128gb-mbp: dispatch + verification

Usage:
    python3 lora_hotswap.py list              # List available adapters
    python3 lora_hotswap.py deploy <node>     # Deploy adapter to node
    python3 lora_hotswap.py deploy-all        # Deploy to all nodes
    python3 lora_hotswap.py route <task>      # Find best node for task
"""
from __future__ import annotations
import os, sys, json, subprocess, argparse
from pathlib import Path

# Available LoRA adapters
ADAPTERS = {
    "rig-kat-v1": {
        "path": "/home/user/rig-ft/output/kat-lora-v2/adapter_model.safetensors",
        "config": "/home/user/rig-ft/output/kat-lora-v2/adapter_config.json",
        "specialty": "coding",
        "rank": 16,
        "trained_on": "7,288 examples (doctrine + traces)",
    },
    "rig-kat-v2": {
        "path": "/home/user/rig-ft/output/kat-lora-r64/adapter_model.safetensors",
        "config": "/home/user/rig-ft/output/kat-lora-r64/adapter_config.json",
        "specialty": "coding",
        "rank": 64,
        "trained_on": "7,288 examples, 2 epochs",
    },
}

# Node → specialty mapping
NODE_SPECIALTIES = {
    "blackwell": {"specialty": "coding", "adapter": "rig-kat-v2", "gpu": True},
    "rig-96gb": {"specialty": "synthesis", "adapter": None, "gpu": False},
    "rig-256gb": {"specialty": "strategy", "adapter": None, "gpu": False},
    "rig-36gb": {"specialty": "research", "adapter": None, "gpu": False},
    "rig-128gb-mbp": {"specialty": "verification", "adapter": None, "gpu": False},
}

# Task → specialty routing
TASK_ROUTING = {
    "code": "coding",
    "function": "coding",
    "debug": "coding",
    "refactor": "coding",
    "implement": "coding",
    "write": "coding",
    "fix": "coding",
    "synthesize": "synthesis",
    "review": "synthesis",
    "creative": "synthesis",
    "strategy": "strategy",
    "analyze": "strategy",
    "design": "strategy",
    "compare": "strategy",
    "research": "research",
    "enrich": "research",
    "scrape": "research",
    "verify": "verification",
    "check": "verification",
    "audit": "verification",
    "test": "verification",
}

def list_adapters():
    """List all available LoRA adapters."""
    print("\nAvailable LoRA Adapters:")
    print("─" * 70)
    for name, cfg in ADAPTERS.items():
        exists = "✓" if os.path.exists(cfg["path"]) else "✗"
        print(f"  {exists} {name}")
        print(f"    Specialty: {cfg['specialty']}")
        print(f"    Rank: r={cfg['rank']}")
        print(f"    Trained on: {cfg['trained_on']}")
        print(f"    Path: {cfg['path']}")
    print()
    print("Node Assignments:")
    print("─" * 70)
    for node, cfg in NODE_SPECIALTIES.items():
        adapter = cfg.get("adapter", "none")
        print(f"  {node}: {cfg['specialty']} → adapter: {adapter}")

def deploy_adapter(node: str):
    """Deploy the assigned adapter to a fleet node."""
    cfg = NODE_SPECIALTIES.get(node)
    if not cfg:
        print(f"Unknown node: {node}")
        return
    
    adapter_name = cfg.get("adapter")
    if not adapter_name:
        print(f"Node {node} has no adapter assigned (specialty: {cfg['specialty']})")
        return
    
    adapter = ADAPTERS.get(adapter_name)
    if not adapter:
        print(f"Adapter {adapter_name} not found")
        return
    
    if node == "blackwell":
        # Local deployment — already deployed via vLLM
        print(f"✓ {node}: adapter {adapter_name} already local")
        return
    
    # Remote deployment via SCP
    print(f"Deploying {adapter_name} to {node}...")
    fleet_script = Path.home() / ".rig" / "scripts" / "prime-jake-fleet.py"
    
    # Create remote directory
    subprocess.run([
        "python3", str(fleet_script), "exec",
        f"mkdir -p ~/.rig/adapters/{adapter_name}",
        "--nodes", node
    ], capture_output=True, text=True, timeout=30)
    
    # SCP adapter files
    adapter_path = adapter["path"]
    config_path = adapter["config"]
    
    import shutil
    # Use fleet deploy
    subprocess.run([
        "python3", str(fleet_script), "deploy",
        adapter_path, f"~/.rig/adapters/{adapter_name}/adapter_model.safetensors"
    ], capture_output=True, text=True, timeout=60)
    
    subprocess.run([
        "python3", str(fleet_script), "deploy",
        config_path, f"~/.rig/adapters/{adapter_name}/adapter_config.json"
    ], capture_output=True, text=True, timeout=60)
    
    print(f"✓ Deployed {adapter_name} to {node}")

def deploy_all():
    """Deploy adapters to all nodes."""
    for node in NODE_SPECIALTIES:
        deploy_adapter(node)

def route_task(task: str):
    """Find the best node for a task."""
    task_lower = task.lower()
    best_specialty = None
    for keyword, specialty in TASK_ROUTING.items():
        if keyword in task_lower:
            best_specialty = specialty
            break
    
    if not best_specialty:
        best_specialty = "coding"  # default
    
    # Find node with matching specialty
    for node, cfg in NODE_SPECIALTIES.items():
        if cfg["specialty"] == best_specialty:
            print(f"Task: {task[:80]}")
            print(f"  Specialty: {best_specialty}")
            print(f"  Best node: {node}")
            print(f"  Adapter: {cfg.get('adapter', 'none')}")
            return node
    return None

def main():
    parser = argparse.ArgumentParser(description="Per-Node LoRA Hot-Swap")
    parser.add_argument("command", choices=["list", "deploy", "deploy-all", "route"])
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()
    
    if args.command == "list":
        list_adapters()
    elif args.command == "deploy":
        if not args.args:
            print("Usage: deploy <node>")
            sys.exit(1)
        deploy_adapter(args.args[0])
    elif args.command == "deploy-all":
        deploy_all()
    elif args.command == "route":
        if not args.args:
            print("Usage: route <task description>")
            sys.exit(1)
        route_task(" ".join(args.args))

if __name__ == "__main__":
    main()
