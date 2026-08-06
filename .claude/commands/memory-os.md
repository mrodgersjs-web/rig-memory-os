# /memory-os — RIG Memory OS v10

Quick status, MCP discovery, and daemon control for the Memory OS founder runtime.

## Usage

```bash
# Status (requires RIG_MEMORY_OS_SECRET)
python -m founder_runtime.bootstrap --json

# MCP server (stdio transport — used by Claude Code via .mcp.json)
python -m founder_runtime.mcp_server

# 24/7 daemon (pooled connection, Phase 7)
python -m founder_runtime.service --interval 300
python -m founder_runtime.service --once       # one cycle, exit
python -m founder_runtime.service --no-reconcile

# Prefect flows (local or server)
python -m founder_runtime.flows run --flow cockpit_watchdog_flow
python -m founder_runtime.flows serve          # always-on
```

## Environment

| Variable | Required | Default | Description |
|---|---|---|---|
| `RIG_MEMORY_OS_SECRET` | Yes | — | HMAC root for gateway signing |
| `RIG_MEMORY_OS_DSN` | No | `host=/tmp port=5432 dbname=rig_memory_os_phase1` | Full Postgres DSN |
| `RIG_MEMORY_OS_PG_HOST` | No | `/tmp` | DSN host (if not using full DSN) |
| `RIG_MEMORY_OS_PG_PORT` | No | `5432` | DSN port |
| `RIG_MEMORY_OS_PG_DB` | No | `rig_memory_os_phase1` | DSN database |

## MCP Tools (6)

Discovered automatically when `.mcp.json` is installed. Each tool requires a
signed context — the bootstrap output provides the run/session/author identity.

- `memory_session_start` — begin a memory session
- `memory_heartbeat` — send a heartbeat
- `memory_record_event` — record an event (requires event_type, action)
- `memory_get_context_package` — retrieve scoped context
- `memory_cockpit_status` — cockpit state + budget + panels
- `memory_session_end` — end a memory session

## Phase 7: Connection Pooling

The daemon (`service.py`) creates one psycopg connection at start-up and
reuses it across all reconcile cycles within a single daemon lifetime.
The `--once` path and tests open/close a short-lived connection per call
for backward compatibility.

## Proof & Testing

```bash
# Run Phase 6+7 test suite
.venv/bin/python -m pytest founder_runtime/tests/test_phase6_247.py -v

# Full suite
.venv/bin/python -m pytest founder_runtime/tests --tb=short -q
```

Proof artifacts: `proof/phase7-*.json`, `proof/phase7-*.txt`
