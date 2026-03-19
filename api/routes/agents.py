"""Agents discovery endpoint — lists available agent definitions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from utils.paths import PROJECT_ROOT, parse_md_frontmatter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents"])


def _get_agents_dir() -> Path:
    """Return the agents directory (context/agents symlinks)."""
    ctx_agents = PROJECT_ROOT / "context" / "agents"
    if ctx_agents.is_dir():
        return ctx_agents
    return PROJECT_ROOT / "default-agents"


def _load_agent_info(agent_file: Path) -> dict[str, Any] | None:
    """Parse agent .md frontmatter to get name and description."""
    if not agent_file.is_file():
        return None
    try:
        content = agent_file.read_text()
    except IOError:
        return None
    name, description = parse_md_frontmatter(content, agent_file.stem)
    return {"name": name, "description": description, "file": agent_file.name}


@router.get("")
async def list_agents() -> dict[str, Any]:
    """List all available agent definitions."""
    agents_dir = _get_agents_dir()
    agents: list[dict[str, Any]] = []

    if not agents_dir.is_dir():
        return {"agents": agents}

    for entry in sorted(agents_dir.iterdir()):
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved.suffix != ".md":
            continue
        info = _load_agent_info(resolved)
        if info is not None:
            agents.append(info)

    return {"agents": agents}
