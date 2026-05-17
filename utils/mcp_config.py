"""Single source of truth for resolving available MCP server configurations.

Claude Code stores MCP server definitions in two places:

1. ``.claude_config/.claude.json`` — the bundled CLI's per-project map. Entries
   added via ``claude mcp add ...`` land here under
   ``projects[<project_dir>].mcpServers``.
2. ``<project_root>/.mcp.json`` — the project-scoped file the CLI auto-loads
   on startup and merges into the same logical pool. ``enabledMcpjsonServers``
   on the project entry whitelists which of these are active.

Historically the orchestrator only consulted (1), so when a user added an MCP
via ``.mcp.json`` (the recommended project-scoped path) the orchestrator's
system prompt advertised an empty MCP list and the model hallucinated names.
This module unifies both sources so prompt-builders, the
``open_agent_session`` tool, and the REST ``/api/mcp/servers`` endpoint all
see the same list.

Public API:
    - :func:`load_available_mcps` — full ``name → config`` mapping
    - :func:`get_mcp_configs` — subset for a requested list of names
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from utils.paths import get_project_dir

logger = logging.getLogger(__name__)

# File names used by the bundled Claude CLI.
_CLAUDE_JSON = ".claude.json"
_PROJECT_MCP_JSON = ".mcp.json"


def _claude_json_path() -> Path:
    """Resolve ``.claude.json`` honouring ``CLAUDE_CONFIG_DIR``."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / _CLAUDE_JSON
    return get_project_dir() / ".claude_config" / _CLAUDE_JSON


def _project_mcp_json_path() -> Path:
    """Resolve the project-scoped ``.mcp.json``."""
    return get_project_dir() / _PROJECT_MCP_JSON


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read %s: %s", path, e)
        return {}


def _project_section(claude_config: dict[str, Any]) -> dict[str, Any]:
    """Return the project block from ``.claude.json`` for this project."""
    project_dir = str(get_project_dir())
    return claude_config.get("projects", {}).get(project_dir, {})


def load_available_mcps() -> dict[str, dict[str, Any]]:
    """Return all MCP servers available to agent sessions in this project.

    Merges the two sources Claude Code recognises, with project-scoped
    ``.mcp.json`` taking precedence on name collisions (it's the file the
    user owns and edits directly; the bundled CLI's per-project map is
    machine-managed and can fall out of date).

    Honours ``enabledMcpjsonServers`` / ``disabledMcpjsonServers`` from the
    bundled CLI's project block when present — these are how the CLI lets a
    user opt out of a ``.mcp.json`` entry without deleting it. If both are
    empty the project-scoped entries are all considered enabled (the CLI's
    default).
    """
    project_block = _project_section(_read_json(_claude_json_path()))
    claude_json_mcps: dict[str, dict[str, Any]] = (
        project_block.get("mcpServers") or {}
    )

    project_mcp = _read_json(_project_mcp_json_path())
    project_file_mcps: dict[str, dict[str, Any]] = (
        project_mcp.get("mcpServers") or {}
    )

    enabled = set(project_block.get("enabledMcpjsonServers") or [])
    disabled = set(project_block.get("disabledMcpjsonServers") or [])
    if enabled:
        project_file_mcps = {
            n: c for n, c in project_file_mcps.items() if n in enabled
        }
    if disabled:
        project_file_mcps = {
            n: c for n, c in project_file_mcps.items() if n not in disabled
        }

    merged: dict[str, dict[str, Any]] = {**claude_json_mcps, **project_file_mcps}
    return merged


def get_mcp_configs(names: list[str]) -> dict[str, dict[str, Any]]:
    """Return only the requested MCPs, dropping (with a warning) any unknown."""
    available = load_available_mcps()
    result: dict[str, dict[str, Any]] = {}
    for name in names:
        if name in available:
            result[name] = available[name]
        else:
            logger.warning("MCP server %r not found in config", name)
    return result
