"""MCP (Model Context Protocol) server management endpoints."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _get_claude_config_path() -> Path:
    """Get the path to .claude.json config file."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".claude.json"
    # Fallback to project root
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / ".claude_config" / ".claude.json"


def _get_project_dir() -> str:
    """Get the current project directory."""
    project_root = Path(__file__).resolve().parent.parent.parent
    return str(project_root)


def _load_mcp_servers() -> dict[str, dict[str, Any]]:
    """Load MCP server configurations from .claude.json."""
    config_path = _get_claude_config_path()
    if not config_path.is_file():
        logger.warning("Claude config not found at %s", config_path)
        return {}

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to load Claude config: %s", e)
        return {}

    # Get project-specific MCP servers
    project_dir = _get_project_dir()
    projects = config.get("projects", {})
    project_config = projects.get(project_dir, {})

    return project_config.get("mcpServers", {})


@router.get("/servers")
async def list_mcp_servers() -> dict[str, Any]:
    """List all available MCP servers from the Claude config.

    Returns a dict with:
        - servers: dict mapping server name to its configuration
        - project_dir: the current project directory
    """
    servers = _load_mcp_servers()
    return {
        "servers": servers,
        "project_dir": _get_project_dir(),
    }


@router.get("/servers/{server_name}")
async def get_mcp_server(server_name: str) -> dict[str, Any]:
    """Get configuration for a specific MCP server."""
    servers = _load_mcp_servers()
    if server_name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_name}' not found")
    return {
        "name": server_name,
        "config": servers[server_name],
    }
