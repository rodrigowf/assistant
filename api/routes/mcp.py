"""MCP (Model Context Protocol) server management endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from utils.mcp_config import load_available_mcps
from utils.paths import get_project_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _load_mcp_servers() -> dict[str, dict[str, Any]]:
    """Compatibility shim — prefer :func:`utils.mcp_config.load_available_mcps`.

    Kept because ``api/routes/chat.py`` still imports this name. New code
    should use the canonical helper directly.
    """
    return load_available_mcps()


@router.get("/servers")
async def list_mcp_servers() -> dict[str, Any]:
    """List all available MCP servers for this project.

    Returns a dict with:
        - servers: dict mapping server name to its configuration
        - project_dir: the current project directory
    """
    return {
        "servers": load_available_mcps(),
        "project_dir": str(get_project_dir()),
    }


@router.get("/servers/{server_name}")
async def get_mcp_server(server_name: str) -> dict[str, Any]:
    """Get configuration for a specific MCP server."""
    servers = load_available_mcps()
    if server_name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_name}' not found")
    return {
        "name": server_name,
        "config": servers[server_name],
    }
