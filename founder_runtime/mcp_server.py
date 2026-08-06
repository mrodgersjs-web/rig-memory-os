"""RIG Memory OS v10 - MCP server (Phase 9: full intelligence routing).

Exposes the complete Memory OS toolset via the Model Context Protocol (stdio).

Session lifecycle:
  memory_session_start / memory_heartbeat / memory_session_end
  memory_record_event / memory_get_context_package

Cockpit:
  memory_cockpit_status / memory_intelligence_snapshot

Intelligence (Phase 9):
  memory_predict_next / memory_resolve_prediction / memory_record_transition
  memory_observe_session (Jake Observer pushback)
  memory_recommend (Recommendation Engine)
  memory_add_claim (Reality Cortex)
  memory_search (GBrain-Obsidian Bridge)

Usage:
    python -m founder_runtime.mcp_server   # stdio transport
"""

from __future__ import annotations

import json
import os
import uuid

from mcp.server.mcpserver.server import MCPServer

server = MCPServer("memory-os")

_runtime = None
_runtime_lock = __import__("threading").Lock()


def _get_runtime():
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                from founder_runtime.runtime import MemoryOSRuntime
                _runtime = MemoryOSRuntime.from_env()
    return _runtime


def _ctx(run_id, session_id, actor):
    from founder_runtime.memory_gateway import SensitivityCeiling, sign_context
    secret = os.environ.get("RIG_MEMORY_OS_SECRET", "test-universal-secret").encode("utf-8")
    trace_id = str(uuid.uuid4())[:8]
    return sign_context(
        secret,
        operator_id=actor, tenant_id="mcp", client_id="mcp",
        project_id="mcp", mission_id="mcp",
        agent_principal=actor, agent_instance="mcp-1",
        harness_version="mcp", adapter_version="mcp", node="mcp",
        purpose="mcp-tool",
        sensitivity_ceiling=SensitivityCeiling.INTERNAL,
        run_id=run_id, session_id=session_id, trace_id=trace_id,
        policy_version="v1",
    )


def _invoke(tool_name, run_id="default", session_id="default",
            actor="agent", body=None):
    rt = _get_runtime()
    ctx = _ctx(run_id, session_id, actor)
    result = rt.gateway.invoke(ctx, tool_name, body)
    return json.dumps({
        "accepted": result.accepted,
        "tool_name": result.tool_name,
        "reject_reason": result.reject_reason.value if result.reject_reason else None,
        "reject_detail": result.reject_detail,
        "receipt_id": result.receipt.receipt_id if result.receipt else None,
    })


# --- Session lifecycle ---

def _memory_session_start(run_id="default", session_id="default",
                          actor="agent"):
    """Start a memory session."""
    return _invoke("memory.session_start", run_id, session_id, actor)


def _memory_heartbeat(run_id="default", session_id="default",
                      actor="agent"):
    """Send a heartbeat."""
    return _invoke("memory.heartbeat", run_id, session_id, actor)


def _memory_record_event(event_type, action,
                         run_id="default", session_id="default",
                         actor="agent"):
    """Record a memory event."""
    return _invoke("memory.record_event", run_id, session_id, actor,
                   body={"event_type": event_type, "action": action})


def _memory_get_context_package(query,
                                run_id="default", session_id="default",
                                actor="agent"):
    """Get a context package for a query."""
    return _invoke("memory.get_context_package", run_id, session_id, actor,
                   body={"query": query})


def _memory_session_end(run_id="default", session_id="default",
                        actor="agent"):
    """End a memory session."""
    return _invoke("memory.session_end", run_id, session_id, actor)


# --- Cockpit ---

def _memory_cockpit_status():
    """Get cockpit status snapshot."""
    rt = _get_runtime()
    snap = rt.cockpit.snapshot()
    return json.dumps({
        "state": snap.control_state.value,
        "budget": snap.budget_remaining,
        "panel_count": len(snap.panels),
        "panels": [{"name": p.name, "status": p.status} for p in snap.panels],
    })


def _memory_intelligence_snapshot():
    """Get full intelligence layer status."""
    rt = _get_runtime()
    return json.dumps(rt.intelligence_snapshot(), default=str)


# --- Intelligence: Predictor ---

def _memory_predict_next(current_state, event_type="tool_call",
                         harness="default", stage="default",
                         project="default"):
    """Generate a prediction about the next state."""
    rt = _get_runtime()
    return json.dumps(rt.predict_next(current_state, event_type,
                                      harness, stage, project), default=str)


def _memory_resolve_prediction(prediction_id, actual_outcome):
    """Resolve a prediction with the actual outcome."""
    rt = _get_runtime()
    return json.dumps(rt.resolve_prediction(prediction_id, actual_outcome), default=str)


def _memory_record_transition(current_state, event_type, next_state,
                              harness="default", stage="default",
                              project="default"):
    """Feed observed state transitions into the predictor."""
    rt = _get_runtime()
    rt.record_transition(current_state, event_type, next_state,
                        harness, stage, project)
    return json.dumps({"accepted": True, "message": "transition recorded"})


# --- Intelligence: Jake Observer ---

def _memory_observe_session(files_modified_json="[]", goals_json="[]",
                            time_spent=0.0, tests_written=0,
                            abstractions_created=0,
                            concrete_implementations=0,
                            time_without_progress=0.0):
    """Run a session through the Jake Observer pushback engine."""
    rt = _get_runtime()
    files = json.loads(files_modified_json) if isinstance(files_modified_json, str) else files_modified_json
    goals = json.loads(goals_json) if isinstance(goals_json, str) else goals_json
    return json.dumps(rt.observe_session(
        files_modified=files, time_spent=time_spent, goals=goals,
        tests_written=tests_written, abstractions_created=abstractions_created,
        concrete_implementations=concrete_implementations,
        time_without_progress=time_without_progress,
    ), default=str)


# --- Intelligence: Recommendations ---

def _memory_recommend(session_data_json="{}"):
    """Generate proactive recommendations."""
    rt = _get_runtime()
    session_data = json.loads(session_data_json) if isinstance(session_data_json, str) else session_data_json
    return json.dumps(rt.recommend(session_data), default=str)


# --- Intelligence: Reality Cortex ---

def _memory_add_claim(subject, statement, evidence_refs_json="[]",
                      confidence=0.5):
    """Add a claim to the Reality Cortex."""
    rt = _get_runtime()
    evidence = json.loads(evidence_refs_json) if isinstance(evidence_refs_json, str) else evidence_refs_json
    return json.dumps(rt.add_reality_claim(subject, statement, evidence, confidence), default=str)


# --- Intelligence: GBrain-Obsidian Bridge ---

def _memory_search(query, limit=20):
    """Search across all memory stores."""
    rt = _get_runtime()
    return json.dumps(rt.search_memory(query, limit), default=str)


# --- Jake Guidance ---

def _memory_get_guidance():
    """Jake's live guidance: advice, open predictions, track record, warnings."""
    from founder_runtime.jake_guidance import get_guidance
    return json.dumps(get_guidance(), default=str)


# --- Register all tools ---

server.add_tool(_memory_session_start, name="memory_session_start")
server.add_tool(_memory_heartbeat, name="memory_heartbeat")
server.add_tool(_memory_record_event, name="memory_record_event")
server.add_tool(_memory_get_context_package, name="memory_get_context_package")
server.add_tool(_memory_cockpit_status, name="memory_cockpit_status")
server.add_tool(_memory_session_end, name="memory_session_end")
server.add_tool(_memory_intelligence_snapshot, name="memory_intelligence_snapshot")
server.add_tool(_memory_predict_next, name="memory_predict_next")
server.add_tool(_memory_resolve_prediction, name="memory_resolve_prediction")
server.add_tool(_memory_record_transition, name="memory_record_transition")
server.add_tool(_memory_observe_session, name="memory_observe_session")
server.add_tool(_memory_recommend, name="memory_recommend")
server.add_tool(_memory_add_claim, name="memory_add_claim")
server.add_tool(_memory_search, name="memory_search")
server.add_tool(_memory_get_guidance, name="memory_get_guidance")


def main():
    import asyncio
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
