"""Global configuration endpoint — manages working directory, skills, and MCP defaults."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])

_CONFIG_FILE_NAME = "assistant_config.json"


def _get_config_path() -> Path:
    """Return path to the global config JSON."""
    return PROJECT_ROOT / _CONFIG_FILE_NAME


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
    return {
        "working_directory": str(PROJECT_ROOT),
        "working_directory_history": [str(PROJECT_ROOT)],
        "enabled_mcps": [],   # empty = all enabled (legacy behavior)
        "disabled_skills": [],  # list of skill names to hide
        "disabled_agents": [],  # list of agent names to hide
        "chrome_extension": False,  # launch sessions with --chrome flag
        "default_model": "claude-sonnet-4-5-20250929",  # default model for orchestrator
    }


# -----------------------------------------------------------------------
# Pydantic models
# -----------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    working_directory: str | None = None
    working_directory_history: list[str] | None = None  # full replacement of the list
    enabled_mcps: list[str] | None = None
    disabled_skills: list[str] | None = None
    disabled_agents: list[str] | None = None
    chrome_extension: bool | None = None
    default_model: str | None = None  # default model for new orchestrator sessions


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
        if not Path(new_dir).is_dir():
            raise HTTPException(status_code=400, detail=f"Directory does not exist: {new_dir}")
        config["working_directory"] = new_dir
        # Ensure selected dir is in history
        history: list[str] = config.get("working_directory_history", [])
        if new_dir not in history:
            history.append(new_dir)
        config["working_directory_history"] = history[:20]

    if body.working_directory_history is not None:
        # Validate all paths exist; also ensure current selected dir stays consistent
        validated: list[str] = []
        for p in body.working_directory_history:
            if not Path(p).is_dir():
                raise HTTPException(status_code=400, detail=f"Directory does not exist: {p}")
            validated.append(p)
        config["working_directory_history"] = validated[:20]
        # If current working_directory was removed from history, reset to first entry
        if config["working_directory"] not in config["working_directory_history"]:
            config["working_directory"] = config["working_directory_history"][0] if config["working_directory_history"] else ""

    if body.enabled_mcps is not None:
        config["enabled_mcps"] = body.enabled_mcps

    if body.disabled_skills is not None:
        config["disabled_skills"] = body.disabled_skills

    if body.disabled_agents is not None:
        config["disabled_agents"] = body.disabled_agents

    if body.chrome_extension is not None:
        config["chrome_extension"] = body.chrome_extension

    if body.default_model is not None:
        # Validate model exists
        from orchestrator.config import AVAILABLE_MODELS
        if body.default_model not in AVAILABLE_MODELS:
            raise HTTPException(status_code=400, detail=f"Unknown model: {body.default_model}")
        config["default_model"] = body.default_model

    _save_config(config)
    return config
