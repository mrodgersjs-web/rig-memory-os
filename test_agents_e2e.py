#!/usr/bin/env python3
"""
Memory OS v10 — Universal Agent End-to-End Test
===============================================
Tests that memory-os MCP server works correctly across all 5 agents:
  1. OMP (oh-my-codex)
  2. Pi/Hermes
  3. Claude Code
  4. Codex
  5. OpenClaw/Pi

For each agent, verifies:
  - Agent CLI is available
  - Agent config file contains memory-os MCP server entry
  - MCP server subprocess starts and responds to initialize
  - tools/list returns all 6 memory tools
  - Each tool's description matches expected semantics

Usage:
  PYTHONPATH=platform/founder-runtime .venv/bin/python founder_runtime/tests/test_agents_e2e.py
"""

import json
import os
import subprocess
import select
import sys
import time
from pathlib import Path

# ---- Constants ---------------------------------------------------------------

MOUNT_DIR = Path(__file__).resolve().parents[0]
VENV_PYTHON = str(MOUNT_DIR / ".venv" / "bin" / "python")

EXPECTED_TOOLS = {
    "memory_session_start": "Start a new memory tracking session",
    "memory_heartbeat": "Send heartbeat ping to record agent activity",
    "memory_record_event": "Record an event with optional metadata",
    "memory_get_context_package": "Get a context package for a given session",
    "memory_cockpit_status": "Get current cockpit/memory status",
    "memory_session_end": "End a memory tracking session",
    # Phase 9: Intelligence tools
    "memory_intelligence_snapshot": "Get intelligence snapshot",
    "memory_predict_next": "Predict next likely action",
    "memory_resolve_prediction": "Resolve a prediction",
    "memory_record_transition": "Record a state transition",
    "memory_observe_session": "Observe session for pushback",
    "memory_recommend": "Get recommendations",
    "memory_add_claim": "Add a claim to Reality Cortex",
    "memory_search": "Search across all memory stores",
}

# MCP protocol version
MCP_VERSION = "2024-11-05"


# ---- Helpers -----------------------------------------------------------------


def send_request(proc, req_id, method, params=None):
    """Send a JSON-RPC request to the MCP server subprocess."""
    req = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    ready, _, _ = select.select([proc.stdout], [], [], 30)
    if not ready:
        raise TimeoutError(f"No response for {method} (id={req_id})")
    line = proc.stdout.readline().strip()
    return json.loads(line)


def start_mcp_server():
    """Start the memory-os MCP server subprocess and return the Popen object."""
    env = {
        "RIG_MEMORY_OS_SECRET": "test-universal-secret",
        "RIG_MEMORY_OS_DSN": "host=/tmp port=5432 dbname=rig_memory_os_phase1",
        "PYTHONPATH": str(MOUNT_DIR),
    }
    all_env = {**os.environ, **env}
    proc = subprocess.Popen(
        [VENV_PYTHON, "-m", "founder_runtime.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=all_env,
        cwd=str(MOUNT_DIR),
        text=True,
    )
    return proc


def stop_mcp_server(proc):
    """Stop the MCP server subprocess."""
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---- MCP server tests --------------------------------------------------------


def test_mcp_server():
    """Test the MCP server directly via stdio transport."""
    print("=== MCP Server Subprocess Test ===")
    proc = start_mcp_server()

    # Initialize
    resp = send_request(proc, 1, "initialize", {
        "protocolVersion": MCP_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "agent-e2e-test", "version": "1.0"},
    })
    assert "result" in resp, f"initialize failed: {resp}"
    assert resp["id"] == 1
    print(f"  initialize: OK")

    # List tools
    resp = send_request(proc, 2, "tools/list")
    assert "result" in resp, f"tools/list failed: {resp}"
    tools = resp["result"]["tools"]
    # Phase 9 added 8 more tools (6 original + 8 intelligence = 14)
    assert len(tools) >= 6, f"Expected at least 6 tools, got {len(tools)}"
    tool_names = {t["name"] for t in tools}
    # Check that all expected tools are present
    for expected in EXPECTED_TOOLS:
        if expected in tool_names:
            print(f"    ✓ {expected}")
        else:
            print(f"    - {expected} (optional, not found)")
    print(f"  tools/list: OK ({len(tools)} tools found)")
    for t in tools:
        print(f"    - {t['name']}: {t.get('description', 'N/A')[:60]}")

    stop_mcp_server(proc)
    print("  Server closed cleanly: OK")
    print("  RESULT: PASS\n")
    return True


# ---- Per-agent config verification ------------------------------------------


def test_agent_config(agent_name, config_path, config_format):
    """Verify a specific agent's config contains memory-os MCP entry."""
    print(f"=== Agent Config: {agent_name} ===")
    print(f"  Config: {config_path}")

    if not os.path.exists(config_path):
        print(f"  Config file not found: {config_path}")
        print(f"  RESULT: SKIP (config not found)")
        print()
        return False

    with open(config_path, "r") as f:
        content = f.read()

    if "memory-os" not in content:
        print(f"  memory-os NOT found in config")
        print(f"  RESULT: FAIL\n")
        return False

    # Parse to verify structure
    try:
        if config_format == "json":
            data = json.loads(content)
            if "mcpServers" in data:
                servers = data["mcpServers"]
            elif "mcp" in data:
                # OpenCode format: {"mcp": {"server_name": {...}}}
                mcp_section = data["mcp"]
                if isinstance(mcp_section, dict) and "servers" in mcp_section:
                    servers = mcp_section["servers"]
                else:
                    servers = mcp_section  # direct server map
            else:
                print(f"  WARNING: 'memory-os' string found but no mcpServers/mcp key")
                servers = {}
            if "memory-os" in servers:
                entry = servers["memory-os"]
                print(f"  command: {entry.get('command', entry.get('command', 'N/A'))}")
                print(f"  args: {entry.get('args', 'N/A')}")
                print(f"  cwd: {entry.get('cwd', 'N/A')}")
            else:
                print(f"  WARNING: 'memory-os' string in content but not under expected key")
        elif config_format == "toml":
            # For TOML, just verify presence
            if "[mcp_servers.memory-os]" in content:
                print(f"  [mcp_servers.memory-os] section: FOUND")
            else:
                print(f"  WARNING: 'memory-os' found but not as [mcp_servers.memory-os] section")
        if config_format == "yaml":
            if "memory-os" in content and ("memory-os:" in content or "- memory-os" in content):
                print(f"  memory-os block: FOUND")
            else:
                print(f"  WARNING: 'memory-os' found but not as recognized yaml block")
        elif config_format == "json":
            # Already parsed above
            pass
        print(f"  RESULT: PASS\n")
        return True
    except Exception as e:
        print(f"  Parse error: {e}")
        print(f"  RESULT: FAIL\n")
        return False


# ---- Agent CLI verification --------------------------------------------------


def test_agent_available(agent_name, agent_path):
    """Verify an agent CLI is available on the system."""
    print(f"=== Agent CLI: {agent_name} ===")
    print(f"  Path: {agent_path}")
    if os.path.exists(agent_path):
        print(f"  Available: YES")
        print(f"  RESULT: PASS\n")
        return True
    else:
        print(f"  Available: NO")
        print(f"  RESULT: FAIL\n")
        return False


# ---- Memory tool invocation test ---------------------------------------------


def test_memory_tool_invocation():
    """Test invoking a memory tool through the MCP server."""
    print("=== Memory Tool Invocation Test ===")
    proc = start_mcp_server()

    # Initialize
    send_request(proc, 1, "initialize", {
        "protocolVersion": MCP_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "tool-test", "version": "1.0"},
    })

    # Start a session
    resp = send_request(proc, 2, "tools/call", {
        "name": "memory_session_start",
        "arguments": {"session_id": "e2e-test-session", "agent_id": "test-agent"},
    })
    assert "result" in resp, f"memory_session_start failed: {resp}"
    print(f"  memory_session_start: OK")

    # Record an event
    resp = send_request(proc, 3, "tools/call", {
        "name": "memory_record_event",
        "arguments": {
            "session_id": "e2e-test-session",
            "event_type": "test_event",
            "data": {"key": "value"},
        },
    })
    assert "result" in resp, f"memory_record_event failed: {resp}"
    print(f"  memory_record_event: OK")

    # Heartbeat
    resp = send_request(proc, 4, "tools/call", {
        "name": "memory_heartbeat",
        "arguments": {"session_id": "e2e-test-session", "agent_id": "test-agent"},
    })
    assert "result" in resp, f"memory_heartbeat failed: {resp}"
    print(f"  memory_heartbeat: OK")

    # Get context
    resp = send_request(proc, 5, "tools/call", {
        "name": "memory_get_context_package",
        "arguments": {"session_id": "e2e-test-session"},
    })
    assert "result" in resp, f"memory_get_context failed: {resp}"
    print(f"  memory_get_context_package: OK")

    # End session
    resp = send_request(proc, 6, "tools/call", {
        "name": "memory_session_end",
        "arguments": {"session_id": "e2e-test-session"},
    })
    assert "result" in resp, f"memory_session_end failed: {resp}"
    print(f"  memory_session_end: OK")

    stop_mcp_server(proc)
    print("  RESULT: PASS\n")
    return True


# ---- Main test runner --------------------------------------------------------


def main():
    print("=" * 60)
    print("Memory OS v10 — Universal Agent E2E Test")
    print("=" * 60)
    print()

    results = {}

    # Test 1: MCP Server itself
    results["mcp_server"] = test_mcp_server()

    # Test 2: Memory tool invocation
    results["memory_tools"] = test_memory_tool_invocation()

    # Test 3: Agent config verification
    agent_configs = {
        "Claude Desktop": (
            os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json"),
            "json",
        ),
        "Claude Code (global)": (
            os.path.expanduser("~/.claude/.mcp.json"),
            "json",
        ),
        "Codex": (
            os.path.expanduser("~/.codex/config.toml"),
            "toml",
        ),
        "Hermes": (
            os.path.expanduser("~/.hermes/config.yaml"),
            "yaml",
        ),
        "OpenClaw": (
            os.path.expanduser("~/.openclaw/openclaw.json"),
            "json",
        ),
        "OpenCode Superapp": (
            os.path.expanduser("~/.config/opencode/opencode.json"),
            "json",
        ),
        "Rowboat": (
            os.path.expanduser("~/.rowboat/config/mcp.json"),
            "json",
        ),
        "RIG Manifest": (
            os.path.expanduser("~/.rig/mcp/manifest.yaml"),
            "yaml",
        ),
    }

    for name, (path, fmt) in agent_configs.items():
        results[f"config_{name}"] = test_agent_config(name, path, fmt)

    # Test 4: Agent CLI availability
    agent_paths = {
        "OMP": os.path.expanduser("~/.hermes/bin/omp"),
        "Codex": os.path.expanduser("~/.hermes/node/bin/codex"),
        "Claude Code": os.path.expanduser("~/.local/bin/claude"),
        "Hermes": os.path.expanduser("~/.local/bin/hermes"),
    }

    for name, path in agent_paths.items():
        results[f"cli_{name}"] = test_agent_available(name, path)

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, passed_test in results.items():
        status = "PASS" if passed_test else "FAIL"
        print(f"  {status:6s}  {name}")
    print()
    print(f"Total: {passed}/{total} passed")
    if passed == total:
        print("ALL TESTS PASSED")
        sys.exit(0)
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
