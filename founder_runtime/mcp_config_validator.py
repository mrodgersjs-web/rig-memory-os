"""MCP configuration schema validation for Phase 8.

Validates .mcp.json conforms to Claude Code's expected MCP server
config format before attempting to spawn the server.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MCP_SERVER_SCHEMA = {
    "type": "object",
    "required": ["mcpServers"],
    "properties": {
        "mcpServers": {
            "type": "object",
            "patternProperties": {
                "^.+$": {
                    "type": "object",
                    "required": ["command", "args"],
                    "properties": {
                        "command": {"type": "string", "minLength": 1},
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "cwd": {"type": "string"},
                        "timeout": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": True,
                }
            },
        }
    },
    "additionalProperties": True,
}


def validate_mcp_config(data: dict[str, Any]) -> list[str]:
    """Validate an MCP config dict. Returns list of error messages (empty = valid)."""
    errors: list[str] = []

    if not isinstance(data, dict):
        errors.append("root must be a JSON object")
        return errors

    if "mcpServers" not in data:
        errors.append("missing required key 'mcpServers'")
        return errors

    servers = data["mcpServers"]
    if not isinstance(servers, dict):
        errors.append("mcpServers must be an object")
        return errors

    if len(servers) == 0:
        errors.append("mcpServers must have at least one server entry")
        return errors

    for name, entry in servers.items():
        if not isinstance(entry, dict):
            errors.append(f"server '{name}': must be an object")
            continue

        if "command" not in entry:
            errors.append(f"server '{name}': missing required key 'command'")
        elif not isinstance(entry["command"], str) or len(entry["command"]) < 1:
            errors.append(f"server '{name}': command must be a non-empty string")

        if "args" not in entry:
            errors.append(f"server '{name}': missing required key 'args'")
        elif not isinstance(entry["args"], list):
            errors.append(f"server '{name}': args must be an array")
        elif not all(isinstance(a, str) for a in entry["args"]):
            errors.append(f"server '{name}': all args must be strings")

        if "env" in entry:
            if not isinstance(entry["env"], dict):
                errors.append(f"server '{name}': env must be an object")
            elif not all(isinstance(v, str) for v in entry["env"].values()):
                errors.append(f"server '{name}': all env values must be strings")

    return errors


def load_and_validate(path: str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load an MCP config file and validate it.

    Returns (parsed_config, errors). If JSON parsing fails, config is None.
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        return None, [f"invalid JSON: {e}"]
    return data, validate_mcp_config(data)
