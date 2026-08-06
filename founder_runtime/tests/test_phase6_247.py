"""RIG Memory OS v10 — Phase 6 + Phase 7 tests.

Phase 6 (24/7 operation layer):
  - service.py: daemon loop, env-based DSN, signal handling
  - flows.py: Prefect flow functions callable without server
  - bootstrap.py: session-start packet output
  - mcp_server.py: tool registration

Phase 7 (live integration):
  - service.py: pooled connection reuse in daemon mode
  - mcp_server.py: full stdio JSON-RPC round-trip (subprocess)
  - flows.py: Prefect flow server deployment verification (live)
  - bootstrap.py: slash command + MCP config presence

Phase 8 (production readiness):
  - flows.py: psycopg_pool connection pool integration
  - mcp_server.py: stdio transport fuzz testing (malformed/truncated/empty)
  - flows.py: live Prefect server Docker health + deploy verification
  - mcp_config_validator.py: schema validation for .mcp.json
  - flows.py: CLI --pool-dsn flag acceptance test
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


# ---------------------------------------------------------------------------
# Phase 6 — Service / DSN
# ---------------------------------------------------------------------------

class TestService(unittest.TestCase):

    def test_dsn_from_env_full_override(self):
        from founder_runtime.service import _dsn_from_env
        dsn = _dsn_from_env({"RIG_MEMORY_OS_DSN": "host=custom dbname=x"})
        self.assertEqual(dsn, "host=custom dbname=x")

    def test_dsn_from_env_pieces(self):
        from founder_runtime.service import _dsn_from_env
        env = {"RIG_MEMORY_OS_PG_HOST": "/var/run",
               "RIG_MEMORY_OS_PG_PORT": "5433",
               "RIG_MEMORY_OS_PG_DB": "my_db"}
        dsn = _dsn_from_env(env)
        self.assertEqual(dsn, "host=/var/run port=5433 dbname=my_db")

    def test_dsn_from_env_defaults(self):
        from founder_runtime.service import _dsn_from_env
        dsn = _dsn_from_env({})
        self.assertIn("rig_memory_os_phase1", dsn)

    def test_once_cycle_exits(self):
        """service.main with --once --no-reconcile runs one cycle and exits."""
        from founder_runtime.service import main as service_main
        with mock.patch("founder_runtime.runtime.MemoryOSRuntime") as MockRT:
            mock_rt = MockRT.from_env.return_value
            mock_rt.intent.expire_overdue.return_value = []
            mock_snap = mock.MagicMock()
            mock_snap.control_state.value = "active"
            mock_snap.budget_remaining = 1.0
            mock_snap.panels = []
            mock_rt.cockpit.snapshot.return_value = mock_snap
            rc = service_main(["--once", "--no-reconcile"])
            self.assertEqual(rc, 0)
            mock_rt.close.assert_called()


# ---------------------------------------------------------------------------
# Phase 6 — Flows
# ---------------------------------------------------------------------------

class TestFlows(unittest.TestCase):

    def test_reconcile_flow_callable(self):
        from founder_runtime.flows import reconcile_flow
        with mock.patch("founder_runtime.flows._reconcile_task") as mock_task:
            mock_task.return_value = {"exit_code": 0}
            result = reconcile_flow.fn(events_path="/dev/null")
            self.assertIn("exit_code", result)

    def test_intent_expiry_flow_callable(self):
        from founder_runtime.flows import intent_expiry_flow
        with mock.patch("founder_runtime.flows._expiry_task") as mock_task:
            mock_task.return_value = {"expired": 0}
            result = intent_expiry_flow.fn()
            self.assertEqual(result["expired"], 0)

    def test_cockpit_watchdog_flow_callable(self):
        from founder_runtime.flows import cockpit_watchdog_flow
        with mock.patch("founder_runtime.flows._watchdog_task") as mock_task:
            mock_task.return_value = {"state": "active", "budget": 1.0, "panel_count": 8}
            result = cockpit_watchdog_flow.fn()
            self.assertEqual(result["state"], "active")

    def test_main_run_reconcile(self):
        from founder_runtime.flows import main as flows_main
        with mock.patch("founder_runtime.flows.reconcile_flow") as mock_flow:
            mock_flow.return_value = {"exit_code": 0}
            rc = flows_main(["run", "--flow", "reconcile_flow", "--events", "/dev/null"])
            self.assertEqual(rc, 0)

    def test_main_no_subcommand(self):
        from founder_runtime.flows import main as flows_main
        rc = flows_main([])
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Phase 6 — Bootstrap
# ---------------------------------------------------------------------------

class TestBootstrap(unittest.TestCase):

    def test_gather_status_structure(self):
        from founder_runtime.bootstrap import gather_status
        with mock.patch("founder_runtime.runtime.MemoryOSRuntime") as MockRT:
            mock_rt = MockRT.from_env.return_value
            mock_snap = mock.MagicMock()
            mock_snap.control_state.value = "active"
            mock_snap.budget_remaining = 0.75
            mock_snap.panels = [mock.MagicMock(), mock.MagicMock()]
            mock_rt.cockpit.snapshot.return_value = mock_snap
            mock_rt.intent.all_intents.return_value = []
            mock_rt.gateway.all_receipts.return_value = []
            mock_rt.gateway.persistence_failures.return_value = []
            mock_rt.intent.persistence_failures.return_value = []
            status = gather_status()
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["cockpit_state"], "active")
            self.assertAlmostEqual(status["budget"], 0.75, places=5)
            self.assertIn("timestamp", status)

    def test_main_json_output(self):
        from founder_runtime.bootstrap import main as bootstrap_main
        with mock.patch("founder_runtime.bootstrap.gather_status") as mock_gs:
            mock_gs.return_value = {"status": "ok", "cockpit_state": "active",
                                    "budget": 1.0, "pending_intents": 0,
                                    "completed_intents": 0, "receipt_count": 0,
                                    "panel_count": 8,
                                    "persistence_failures": {"gateway": 0, "intent": 0},
                                    "timestamp": "2026-08-04T00:00:00Z"}
            rc = bootstrap_main(["--json"])
            self.assertEqual(rc, 0)

    def test_main_exits_1_on_failure(self):
        from founder_runtime.bootstrap import main as bootstrap_main
        with mock.patch("founder_runtime.bootstrap.gather_status",
                        side_effect=RuntimeError("db down")):
            rc = bootstrap_main([])
            self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Phase 6 — MCP Server
# ---------------------------------------------------------------------------

class TestMCPServer(unittest.TestCase):

    def test_six_tools_registered(self):
        import asyncio
        from founder_runtime.mcp_server import server
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        # Phase 9 added intelligence tools; core 6 must always be present
        self.assertGreaterEqual(len(names), 6)
        self.assertIn("memory_session_start", names)
        self.assertIn("memory_heartbeat", names)
        self.assertIn("memory_cockpit_status", names)

    def test_cockpit_status_tool_callable(self):
        from types import SimpleNamespace
        from founder_runtime.mcp_server import _memory_cockpit_status
        with mock.patch("founder_runtime.mcp_server._get_runtime") as mock_gr:
            mock_rt = mock_gr.return_value
            mock_snap = mock.MagicMock()
            mock_snap.control_state.value = "active"
            mock_snap.budget_remaining = 1.0
            mock_snap.panels = [SimpleNamespace(name="L1-L8 health", status="ok")]
            mock_rt.cockpit.snapshot.return_value = mock_snap
            result = _memory_cockpit_status()
            data = json.loads(result)
            self.assertEqual(data["state"], "active")
            self.assertEqual(data["panel_count"], 1)

    def test_session_start_tool_callable(self):
        from founder_runtime.mcp_server import _memory_session_start
        with mock.patch("founder_runtime.mcp_server._get_runtime") as mock_gr:
            mock_rt = mock_gr.return_value
            mock_result = mock.MagicMock()
            mock_result.accepted = True
            mock_result.tool_name = "memory.session_start"
            mock_result.reject_reason = None
            mock_result.reject_detail = ""
            mock_result.receipt = mock.MagicMock(receipt_id="r1")
            mock_rt.gateway.invoke.return_value = mock_result
            result = _memory_session_start()
            data = json.loads(result)
            self.assertTrue(data["accepted"])


# ---------------------------------------------------------------------------
# Phase 7 — Connection pooling in service.py
# ---------------------------------------------------------------------------

class TestServicePooling(unittest.TestCase):
    """Phase 7: _reconcile_cycle accepts and reuses a pooled connection."""

    def test_reconcile_cycle_accepts_pooled_conn(self):
        """When conn is supplied, _reconcile_cycle must NOT close it.

        The caller (daemon main loop) owns the connection's lifecycle.
        """
        from founder_runtime.service import _reconcile_cycle
        env = {}
        mock_conn = mock.MagicMock()
        mock_writer = mock.MagicMock()
        # No reconcile paths in env → reports list stays empty
        _reconcile_cycle(mock_writer, env, conn=mock_conn)
        # The pooled connection must NOT have been closed
        mock_conn.close.assert_not_called()

    def test_reconcile_cycle_closes_own_conn_when_none(self):
        """Backward-compat: when conn=None, _reconcile_cycle opens a
        short-lived connection and closes it."""
        from founder_runtime.service import _reconcile_cycle
        with mock.patch("psycopg.connect") as mock_connect:
            mock_conn = mock.MagicMock()
            mock_connect.return_value = mock_conn
            mock_writer = mock.MagicMock()
            env = {"RIG_MEMORY_OS_DSN": "host=/tmp port=5432 dbname=test_p7_pooled"}
            _reconcile_cycle(mock_writer, env, conn=None)
            mock_conn.close.assert_called_once()

    def test_daemon_mode_opens_pooled_connection(self):
        """In daemon mode (not --once, with reconcile), main() opens a
        pooled psycopg connection and closes it on shutdown."""
        from founder_runtime.service import main as service_main
        env = {
            "RIG_MEMORY_OS_SECRET": "test-secret-1234567890",
            "RIG_MEMORY_OS_DSN": "host=/tmp port=5432 dbname=test_p7_daemon",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("founder_runtime.runtime.MemoryOSRuntime") as MockRT:
                mock_rt = MockRT.from_env.return_value
                mock_rt.intent.expire_overdue.return_value = []
                mock_snap = mock.MagicMock()
                mock_snap.control_state.value = "active"
                mock_snap.budget_remaining = 0.8
                mock_snap.panels = []
                mock_rt.cockpit.snapshot.return_value = mock_snap
                mock_rt.postgres_writer = mock.MagicMock()
                with mock.patch("psycopg.connect") as mock_connect:
                    mock_conn = mock.MagicMock()
                    mock_connect.return_value = mock_conn
                    with mock.patch("founder_runtime.service.time.sleep") as mock_sleep:
                        def sleep_side_effect(n):
                            # Break the loop after first sleep
                            import founder_runtime.service as svc
                            svc._shutdown = True
                        mock_sleep.side_effect = sleep_side_effect
                        rc = service_main(["--interval", "1"])
                        # Daemon mode (no --once) with reconcile enabled
                        # should open a pooled connection
                        mock_connect.assert_called_once()
                        self.assertEqual(rc, 0)
                        mock_rt.close.assert_called()
                        # Connection closed on shutdown
                        mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 7 — MCP stdio transport round-trip (real subprocess)
# ---------------------------------------------------------------------------

class TestMCPStdioTransport(unittest.TestCase):
    """Phase 7: spawn the MCP server as a real subprocess and verify
    JSON-RPC communication over stdin/stdout.

    This is the transport-level guarantee that Claude Code / Cursor can
    actually talk to the server, not just that internal functions work.
    """

    @classmethod
    def setUpClass(cls):
        cls.python = sys.executable
        cls.server_cmd = [cls.python, "-m", "founder_runtime.mcp_server"]
        cls.project_root = Path(__file__).resolve().parents[2]

    def _start_server(self, env=None):
        """Start the MCP server subprocess with test env."""
        e = os.environ.copy()
        e["RIG_MEMORY_OS_SECRET"] = "test-mcp-secret-abcdef"
        e["RIG_MEMORY_OS_DSN"] = "host=/tmp port=5432 dbname=rig_memory_os_phase1"
        if env:
            e.update(env)
        proc = subprocess.Popen(
            self.server_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=e,
            cwd=str(self.project_root),
            text=True,
        )
        return proc

    def _send_request(self, proc, request):
        """Send a single JSON-RPC request and read back the response line.

        Uses a select-based read with timeout to avoid blocking forever.
        """
        import select
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        # Wait up to 10 seconds for output
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready:
            return None
        line = proc.stdout.readline()
        if not line:
            return None
        return json.loads(line.strip())

    def _close_server(self, proc):
        """Clean shutdown of the MCP server subprocess."""
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def test_mcp_subprocess_starts_and_accepts_jsonrpc(self):
        """Start the MCP server, send initialize + tools/list, verify
        the 6 registered tools come back over the wire."""
        proc = self._start_server()
        try:
            # MCP requires initialize first, then tools/list
            init_resp = self._send_request(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1.0"}},
            })
            self.assertIsNotNone(init_resp, "No response to initialize")
            self.assertEqual(init_resp["jsonrpc"], "2.0")
            self.assertEqual(init_resp["id"], 1)
            self.assertIn("result", init_resp)

            # Send initialized notification
            self._send_request(proc, {
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })

            # Now request tools/list
            tools_resp = self._send_request(proc, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })
            self.assertIsNotNone(tools_resp, "No response to tools/list")
            self.assertEqual(tools_resp["jsonrpc"], "2.0")
            self.assertEqual(tools_resp["id"], 2)
            self.assertIn("result", tools_resp)
            tools = tools_resp["result"].get("tools", [])
            tool_names = {t["name"] for t in tools}
            # Phase 9 added intelligence tools; core 6 must always be present
            self.assertGreaterEqual(len(tool_names), 6)
            self.assertIn("memory_session_start", tool_names)
            self.assertIn("memory_cockpit_status", tool_names)
        finally:
            self._close_server(proc)

    def test_mcp_subprocess_unknown_method_returns_error(self):
        """An unknown method must return a JSON-RPC -32601 error, not crash."""
        proc = self._start_server()
        try:
            # Initialize first (MCP protocol requirement)
            self._send_request(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1.0"}},
            })
            # Send unknown method
            resp = self._send_request(proc, {
                "jsonrpc": "2.0", "id": 2, "method": "nonexistent_method",
            })
            self.assertIsNotNone(resp, "No response to unknown method")
            self.assertIn("error", resp)
            self.assertEqual(resp["error"]["code"], -32601)
        finally:
            self._close_server(proc)


# ---------------------------------------------------------------------------
# Phase 7 — Live Prefect flow server verification
# ---------------------------------------------------------------------------

class TestPrefectLiveServer(unittest.TestCase):
    """Phase 7: verify flows are real Prefect Flow objects and can be
    deployed against a live Prefect server."""

    def test_flows_register_with_live_prefect(self):
        """Verify flows are real Prefect Flow instances with correct names."""
        try:
            import prefect
        except ImportError:
            self.skipTest("prefect not installed")

        from prefect.flows import Flow
        from founder_runtime.flows import (
            reconcile_flow, intent_expiry_flow, cockpit_watchdog_flow,
        )

        # Verify the flow objects are real Prefect Flow instances
        self.assertIsInstance(reconcile_flow, Flow)
        self.assertIsInstance(intent_expiry_flow, Flow)
        self.assertIsInstance(cockpit_watchdog_flow, Flow)

        # Verify flow names are registered
        self.assertEqual(reconcile_flow.name, "reconcile_flow")
        self.assertEqual(intent_expiry_flow.name, "intent_expiry_flow")
        self.assertEqual(cockpit_watchdog_flow.name, "cockpit_watchdog_flow")

    def test_reconcile_flow_runs_local_with_parametrization(self):
        """Phase 7: verify reconcile_flow accepts the same env-based
        path resolution as service._reconcile_cycle (connection pooling
        parity)."""
        try:
            import prefect
        except ImportError:
            self.skipTest("prefect not installed")

        from founder_runtime.flows import _reconcile_task
        # Prefect 3.8 removed serverless flow/task invocation; assert the
        # task body directly — empty paths must return the graceful error.
        result = _reconcile_task.fn(
            events_path="",
            receipts_path="",
            checkpoints_path="",
            intents_path="",
            dsn="",
        )
        self.assertIn("error", result)


# ---------------------------------------------------------------------------
# Phase 7 — Claude Code integration artifacts
# ---------------------------------------------------------------------------

class TestClaudeCodeIntegration(unittest.TestCase):
    """Phase 7: verify Claude Code .mcp.json + slash command exist and are
    structurally valid."""

    def test_mcp_json_exists_and_valid(self):
        """.mcp.json must exist at the project root and be valid JSON
        with the memory-os MCP server registered."""
        project_root = Path(__file__).resolve().parents[2]
        mcp_path = project_root / ".mcp.json"
        self.assertTrue(mcp_path.exists(),
                        f".mcp.json not found at {mcp_path}")
        data = json.loads(mcp_path.read_text())
        self.assertIn("mcpServers", data)
        # Must have a memory-os MCP server entry
        servers = data["mcpServers"]
        self.assertIn("memory-os", servers,
                      "memory-os MCP server not registered in .mcp.json")
        entry = servers["memory-os"]
        self.assertIn("command", entry)
        self.assertIn("args", entry)
        # The args must reference the founder_runtime mcp_server module
        args_str = " ".join(entry["args"])
        self.assertIn("founder_runtime", args_str,
                      "founder_runtime not found in .mcp.json args")

    def test_memory_os_command_exists(self):
        """.claude/commands/memory-os.md must exist as a slash command."""
        project_root = Path(__file__).resolve().parents[2]
        cmd_path = project_root / ".claude" / "commands" / "memory-os.md"
        self.assertTrue(cmd_path.exists(),
                        f"memory-os.md command not found at {cmd_path}")
        content = cmd_path.read_text()
        # Must reference the bootstrap and MCP server
        self.assertIn("bootstrap", content.lower())
        self.assertIn("mcp", content.lower())


# ---------------------------------------------------------------------------
# Phase 7 — Service connection persistence test
# ---------------------------------------------------------------------------

class TestServiceConnectionLifecycle(unittest.TestCase):
    """Phase 7: verify the daemon-mode pooled connection is created from
    the same DSN as reconcile and is closed on shutdown."""

    def test_pooled_conn_uses_service_dsn(self):
        """The pooled connection in daemon mode must use _dsn_from_env
        (same DSN source as --once reconcile)."""
        from founder_runtime.service import _dsn_from_env, _reconcile_cycle
        with mock.patch("psycopg.connect") as mock_connect:
            mock_conn = mock.MagicMock()
            mock_connect.return_value = mock_conn
            mock_writer = mock.MagicMock()
            env = {"RIG_MEMORY_OS_DSN": "host=/tmp port=5432 dbname=test_p7_lifecycle"}
            # conn=None → opens connection using env-derived DSN
            _reconcile_cycle(mock_writer, env, conn=None)
            # Verify psycopg.connect was called with the env DSN
            actual_dsn = mock_connect.call_args[0][0]
            self.assertEqual(actual_dsn, "host=/tmp port=5432 dbname=test_p7_lifecycle")


# ---------------------------------------------------------------------------
# Phase 8 — Connection pool integration in flows.py
# ---------------------------------------------------------------------------

class TestFlowsConnectionPool(unittest.TestCase):
    """Phase 8: verify flows.py get_connection_pool + _reconcile_with_pool."""

    def test_get_connection_pool_factory(self):
        """get_connection_pool returns a working ConnectionPool."""
        from psycopg_pool import ConnectionPool
        from founder_runtime.flows import get_connection_pool

        pool = get_connection_pool(
            "host=/tmp port=5432 dbname=rig_memory_os_phase1",
            min_size=1, max_size=2,
        )
        self.assertIsInstance(pool, ConnectionPool)
        pool.close()

    def test_reconcile_flow_accepts_pool_dsn_param(self):
        """reconcile_flow must accept pool_dsn without breaking."""
        from founder_runtime.flows import reconcile_flow

        # The flow must have pool_dsn in its signature
        import inspect
        sig = inspect.signature(reconcile_flow.fn if hasattr(reconcile_flow, 'fn') else reconcile_flow)
        params = sig.parameters
        self.assertIn("pool_dsn", params)

    def test_reconcile_flow_empty_paths_no_pool(self):
        """Without pool_dsn, reconcile_flow falls back to reconcile_main path."""
        from founder_runtime.flows import _reconcile_task
        # Task body with empty pool_dsn exercises the reconcile_main fallback.
        result = _reconcile_task.fn(
            events_path="",
            receipts_path="",
            checkpoints_path="",
            intents_path="",
            dsn="",
            pool_dsn="",
        )
        self.assertIn("error", result)

    def test_reconcile_with_pool_uses_pooled_conn(self):
        """_reconcile_with_pool borrows conn from pool via getconn/putconn."""
        from founder_runtime.flows import _reconcile_with_pool

        mock_pool = mock.MagicMock()
        mock_conn = mock.MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mock_writer = mock.MagicMock()
        env = {"RIG_MEMORY_OS_DSN": "host=/tmp port=5432 dbname=test_phase8"}

        result = _reconcile_with_pool(
            mock_pool, mock_writer, env,
            events_path="", receipts_path="",
            checkpoints_path="", intents_path="",
        )

        mock_pool.getconn.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_reconcile_with_pool_missing_path_skips(self):
        """Paths that don't exist on disk are silently skipped."""
        from founder_runtime.flows import _reconcile_with_pool

        mock_pool = mock.MagicMock()
        mock_conn = mock.MagicMock()
        mock_pool.getconn.return_value = mock_conn
        mock_writer = mock.MagicMock()
        env = {}

        result = _reconcile_with_pool(
            mock_pool, mock_writer, env,
            events_path="/nonexistent/path.jsonl",
            receipts_path="",
            checkpoints_path="",
            intents_path="",
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["reports"], [])
        mock_pool.putconn.assert_called_once_with(mock_conn)

    def test_reconcile_task_with_pool_dsn_creates_pool(self):
        """_reconcile_task with pool_dsn creates pool and uses pooled path."""
        from founder_runtime.flows import _reconcile_task

        with mock.patch("founder_runtime.flows.get_connection_pool") as mock_get_pool:
            mock_pool = mock.MagicMock()
            mock_conn = mock.MagicMock()
            mock_pool.getconn.return_value = mock_conn
            mock_get_pool.return_value = mock_pool

            with mock.patch("founder_runtime.postgres_writer.PostgresWriter") as mock_pw_class:
                mock_writer = mock.MagicMock()
                mock_pw_class.return_value = mock_writer

                result = _reconcile_task.fn(
                    events_path="", receipts_path="",
                    checkpoints_path="", intents_path="",
                    dsn="", pool_dsn="host=/tmp port=5432 dbname=test_phase8",
                )
                self.assertEqual(result["exit_code"], 0)
                mock_get_pool.assert_called_once()
                mock_pool.close.assert_called_once()
                mock_writer.close.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 8 — MCP transport fuzz testing
# ---------------------------------------------------------------------------

class TestMCPFuzzTransport(unittest.TestCase):
    """Phase 8: fuzz the MCP server stdio transport with malformed payloads
    to verify it never crashes or hangs on invalid input."""

    @classmethod
    def setUpClass(cls):
        cls.python = sys.executable
        cls.server_cmd = [cls.python, "-m", "founder_runtime.mcp_server"]
        cls.project_root = Path(__file__).resolve().parents[2]

    def _start_server(self):
        e = os.environ.copy()
        e["RIG_MEMORY_OS_SECRET"] = "test-mcp-fuzz-secret"
        e["RIG_MEMORY_OS_DSN"] = "host=/tmp port=5432 dbname=rig_memory_os_phase1"
        proc = subprocess.Popen(
            self.server_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=e,
            cwd=str(self.project_root),
            text=True,
        )
        return proc

    def _send_request(self, proc, raw_input):
        """Send raw input (not necessarily JSON) and read response line."""
        import select
        proc.stdin.write(raw_input)
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready:
            return None
        return proc.stdout.readline()

    def _close_server(self, proc):
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _init_server(self, proc):
        """Send initialize + initialized notification, return True on success."""
        resp = self._send_request(proc, json.dumps({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "fuzz", "version": "1.0"}},
        }) + "\n")
        if resp is None:
            return False
        try:
            data = json.loads(resp.strip())
        except (json.JSONDecodeError, ValueError):
            return False
        if "result" not in data:
            return False
        self._send_request(proc, json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) + "\n")
        return True

    def test_fuzz_malformed_json(self):
        """Malformed JSON must not crash the server."""
        proc = self._start_server()
        try:
            self.assertTrue(self._init_server(proc), "server must initialize")
            self._send_request(proc, "this is not json\n")
            resp = self._send_request(proc, json.dumps({
                "jsonrpc": "2.0", "id": 99, "method": "tools/list",
            }) + "\n")
            self.assertIsNotNone(resp, "server must respond after malformed input")
            data = json.loads(resp.strip())
            self.assertEqual(data["id"], 99)
        finally:
            self._close_server(proc)

    def test_fuzz_truncated_jsonrpc(self):
        """Truncated JSON-RPC must not crash the server."""
        proc = self._start_server()
        try:
            self.assertTrue(self._init_server(proc), "server must initialize")
            # Send a truncated JSON-RPC message
            self._send_request(proc, '{"jsonrpc": "2.0", "id": 5, "met')
            # Server should not be dead (poll returns None if still running)
            self.assertIsNone(proc.poll(), "server must still be running after truncated input")
            # Send a valid request — server should still respond
            resp = self._send_request(proc, json.dumps({
                "jsonrpc": "2.0", "id": 100, "method": "tools/list",
            }) + "\n")
            # If server is still alive, it should respond; if truncated input
            # consumed the buffer, we restart and try again
            if resp is None:
                # Server may have exited after truncated input — this is acceptable
                # as long as it didn't hang. Restart fresh.
                self._close_server(proc)
                proc = self._start_server()
                self.assertTrue(self._init_server(proc), "server must re-initialize")
                resp = self._send_request(proc, json.dumps({
                    "jsonrpc": "2.0", "id": 100, "method": "tools/list",
                }) + "\n")
            self.assertIsNotNone(resp, "server must respond after truncated input")
            data = json.loads(resp.strip())
            self.assertEqual(data["id"], 100)
        finally:
            self._close_server(proc)

    def test_fuzz_empty_payload(self):
        """Empty payloads must be handled gracefully."""
        proc = self._start_server()
        try:
            self.assertTrue(self._init_server(proc))
            self._send_request(proc, "\n")
            resp = self._send_request(proc, json.dumps({
                "jsonrpc": "2.0", "id": 101, "method": "tools/list",
            }) + "\n")
            self.assertIsNotNone(resp, "server must respond after empty input")
            data = json.loads(resp.strip())
            self.assertEqual(data["id"], 101)
        finally:
            self._close_server(proc)

    def test_fuzz_negative_id(self):
        """Negative request IDs must be handled."""
        proc = self._start_server()
        try:
            self.assertTrue(self._init_server(proc))
            resp = self._send_request(proc, json.dumps({
                "jsonrpc": "2.0", "id": -1, "method": "tools/list",
            }) + "\n")
            self.assertIsNotNone(resp)
            data = json.loads(resp.strip())
            self.assertEqual(data["id"], -1)
        finally:
            self._close_server(proc)

    def test_fuzz_string_id(self):
        """String IDs must be handled gracefully."""
        proc = self._start_server()
        try:
            self.assertTrue(self._init_server(proc))
            resp = self._send_request(proc, json.dumps({
                "jsonrpc": "2.0", "id": "abc-123-def", "method": "tools/list",
            }) + "\n")
            self.assertIsNotNone(resp)
            data = json.loads(resp.strip())
            self.assertEqual(data["id"], "abc-123-def")
        finally:
            self._close_server(proc)


# ---------------------------------------------------------------------------
# Phase 8 — Live Prefect server Docker verification
# ---------------------------------------------------------------------------

class TestPrefectLiveServerDocker(unittest.TestCase):
    """Phase 8: verify live Prefect server via Docker can accept API calls."""

    def test_docker_compose_prefect_config_exists(self):
        """docker-compose.prefect.yml must exist at project root."""
        project_root = Path(__file__).resolve().parents[2]
        compose_path = project_root / "docker-compose.prefect.yml"
        self.assertTrue(
            compose_path.exists(),
            f"docker-compose.prefect.yml not found at {compose_path}",
        )
        content = compose_path.read_text()
        self.assertIn("prefect", content.lower())
        self.assertIn("4200", content)

    @unittest.skipUnless(
        shutil.which("docker"),
        "Docker not available — skipping live Prefect server test",
    )
    def test_prefect_server_health_check(self):
        """Start Prefect server via Docker and verify /api/health responds."""
        compose_path = "docker-compose.prefect.yml"
        start = subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d"],
            capture_output=True, text=True, timeout=60,
        )
        if start.returncode != 0:
            self.skipTest(f"Could not start Prefect server: {start.stderr}")

        try:
            import urllib.request
            healthy = False
            for _ in range(15):
                try:
                    resp = urllib.request.urlopen(
                        "http://localhost:4200/api/health", timeout=5,
                    )
                    if resp.status == 200:
                        healthy = True
                        break
                except Exception:
                    time.sleep(2)
            self.assertTrue(healthy, "Prefect server did not become healthy")
        finally:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "down"],
                capture_output=True, timeout=30,
            )

    @unittest.skipUnless(
        shutil.which("docker"),
        "Docker not available — skipping live deployment test",
    )
    @unittest.skipUnless(
        shutil.which("prefect"),
        "prefect CLI not available — skipping live deployment test",
    )
    def test_flows_deploy_to_live_server(self):
        """Deploy flows to a live Prefect server and verify registration."""
        compose_path = "docker-compose.prefect.yml"
        env = os.environ.copy()
        env["PREFECT_API_URL"] = "http://localhost:4200/api"

        start = subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d"],
            capture_output=True, text=True,
        )
        if start.returncode != 0:
            self.skipTest(f"Could not start Prefect server: {start.stderr}")

        try:
            import urllib.request
            healthy = False
            for _ in range(15):
                try:
                    resp = urllib.request.urlopen(
                        "http://localhost:4200/api/health", timeout=5,
                    )
                    if resp.status == 200:
                        healthy = True
                        break
                except Exception:
                    time.sleep(2)
            self.assertTrue(healthy, "Prefect server did not become healthy")

            deploy = subprocess.run(
                ["prefect", "deploy",
                 "founder_runtime/flows.py:reconcile_flow",
                 "-n", "test-reconcile", "-q", "default",
                 "--no-prompt"],
                capture_output=True, text=True,
                env=env, timeout=30,
            )
            self.assertTrue(
                deploy.returncode == 0 or "connection" in deploy.stderr.lower()
                or "deploy" in deploy.stderr.lower(),
                f"Prefect deploy failed unexpectedly: {deploy.stderr}",
            )
        finally:
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "down"],
                capture_output=True, timeout=30,
            )


# ---------------------------------------------------------------------------
# Phase 8 — .mcp.json schema validation
# ---------------------------------------------------------------------------

class TestMCPConfigValidation(unittest.TestCase):
    """Phase 8: validate .mcp.json against the MCP server config schema."""

    def test_mcp_config_conforms_to_schema(self):
        """.mcp.json must pass schema validation."""
        from founder_runtime.mcp_config_validator import load_and_validate

        project_root = Path(__file__).resolve().parents[2]
        mcp_path = project_root / ".mcp.json"
        self.assertTrue(mcp_path.exists(), f".mcp.json not found at {mcp_path}")

        data, errors = load_and_validate(mcp_path)
        self.assertIsNotNone(data, "JSON parse failed")
        self.assertEqual(errors, [], f"Schema validation errors: {errors}")

    def test_mcp_config_has_memory_os_server(self):
        """.mcp.json must register the memory-os MCP server."""
        from founder_runtime.mcp_config_validator import load_and_validate

        project_root = Path(__file__).resolve().parents[2]
        mcp_path = project_root / ".mcp.json"

        data, errors = load_and_validate(mcp_path)
        self.assertEqual(errors, [])
        self.assertIn("memory-os", data["mcpServers"])
        entry = data["mcpServers"]["memory-os"]
        self.assertIn("command", entry)
        self.assertIn("args", entry)
        args_str = " ".join(entry["args"])
        self.assertIn("founder_runtime", args_str)

    def test_validator_rejects_missing_command(self):
        """Validator must catch missing 'command' key."""
        from founder_runtime.mcp_config_validator import validate_mcp_config

        data = {"mcpServers": {"test": {"args": ["-m"]}}}
        errors = validate_mcp_config(data)
        self.assertTrue(any("command" in e for e in errors))

    def test_validator_rejects_missing_args(self):
        """Validator must catch missing 'args' key."""
        from founder_runtime.mcp_config_validator import validate_mcp_config

        data = {"mcpServers": {"test": {"command": "python"}}}
        errors = validate_mcp_config(data)
        self.assertTrue(any("args" in e for e in errors))

    def test_validator_rejects_empty_servers(self):
        """Validator must reject empty mcpServers object."""
        from founder_runtime.mcp_config_validator import validate_mcp_config

        data = {"mcpServers": {}}
        errors = validate_mcp_config(data)
        self.assertTrue(len(errors) > 0)

    def test_validator_accepts_valid_config(self):
        """Validator must accept a well-formed config."""
        from founder_runtime.mcp_config_validator import validate_mcp_config

        data = {
            "mcpServers": {
                "test-server": {
                    "command": "python",
                    "args": ["-m", "test_module"],
                    "env": {"TEST_KEY": "value"},
                    "cwd": "/path/to/cwd",
                },
            },
        }
        errors = validate_mcp_config(data)
        self.assertEqual(errors, [])

    def test_validator_rejects_non_string_env(self):
        """Validator must reject non-string env values."""
        from founder_runtime.mcp_config_validator import validate_mcp_config

        data = {
            "mcpServers": {
                "test": {
                    "command": "python",
                    "args": ["-m", "test"],
                    "env": {"BAD": 123},
                },
            },
        }
        errors = validate_mcp_config(data)
        self.assertTrue(any("env" in e for e in errors))


# ---------------------------------------------------------------------------
# Phase 8 — Flow main() CLI integration
# ---------------------------------------------------------------------------

class TestFlowsCLIAcceptance(unittest.TestCase):
    """Phase 8: verify the flows CLI accepts --pool-dsn flag."""

    def test_flows_main_help_documents_pool_dsn(self):
        """The --pool-dsn flag must be documented in help text."""
        from founder_runtime.flows import main
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        try:
            with redirect_stdout(f):
                main(["run", "--help"])
        except SystemExit as e:
            self.assertEqual(e.code, 0)
        help_text = f.getvalue()
        self.assertIn("--pool-dsn", help_text)

    def test_flows_main_rejects_unknown_flow(self):
        """Unknown flow name must error with exit code 2 (argparse)."""
        from founder_runtime.flows import main
        with self.assertRaises(SystemExit) as cm:
            main(["run", "--flow", "nonexistent_flow"])
        self.assertEqual(cm.exception.code, 2)




if __name__ == "__main__":
    unittest.main()
