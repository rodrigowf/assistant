"""Global configuration endpoint — manages working directory, skills, and MCP defaults."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])

_CONFIG_FILE_NAME = "assistant_config.json"


def _get_config_path() -> Path:
    """Return path to the global config JSON."""
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / _CONFIG_FILE_NAME


def _load_config() -> dict[str, Any]:
    path = _get_config_path()
    if not path.is_file():
        return _default_config()
    try:
        with open(path) as f:
            data = json.load(f)
        # Ensure all expected keys exist (forward-compat)
        defaults = _default_config()
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to load assistant config: %s", e)
        return _default_config()


def _save_config(data: dict[str, Any]) -> None:
    path = _get_config_path()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _default_config() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent.parent.parent
    return {
        "working_directory": str(project_root),
        "working_directory_history": [str(project_root)],
        "enabled_mcps": [],   # empty = all enabled (legacy behavior)
        "disabled_skills": [],  # list of skill names to hide
    }


# -----------------------------------------------------------------------
# Pydantic models
# -----------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    working_directory: str | None = None
    enabled_mcps: list[str] | None = None
    disabled_skills: list[str] | None = None


# -----------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------

@router.get("")
async def get_config() -> dict[str, Any]:
    """Return the current global configuration."""
    return _load_config()


@router.put("")
async def update_config(body: ConfigUpdate) -> dict[str, Any]:
    """Update one or more config fields. Returns the full updated config."""
    config = _load_config()

    if body.working_directory is not None:
        new_dir = body.working_directory
        # Validate the path exists
        if not Path(new_dir).is_dir():
            raise HTTPException(status_code=400, detail=f"Directory does not exist: {new_dir}")
        config["working_directory"] = new_dir
        # Keep history, deduplicate, most-recent-first
        history: list[str] = config.get("working_directory_history", [])
        if new_dir in history:
            history.remove(new_dir)
        history.insert(0, new_dir)
        config["working_directory_history"] = history[:20]  # cap at 20

    if body.enabled_mcps is not None:
        config["enabled_mcps"] = body.enabled_mcps

    if body.disabled_skills is not None:
        config["disabled_skills"] = body.disabled_skills

    _save_config(config)
    return config
