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
        # Migrate legacy string history to WorkingDirectoryEntry objects
        data["working_directory_history"] = _migrate_wd_history(data["working_directory_history"])
        # Migrate legacy string working_directory to an entry id
        data["working_directory"] = _migrate_wd_active(
            data["working_directory"], data["working_directory_history"]
        )
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to load assistant config: %s", e)
        return _default_config()


def _migrate_wd_history(history: list) -> list[dict]:
    """Convert any legacy plain-string entries to WorkingDirectoryEntry dicts."""
    result = []
    for item in history:
        if isinstance(item, str):
            result.append({"id": item, "path": item, "label": None, "ssh_host": None, "ssh_user": None, "ssh_key": None})
        elif isinstance(item, dict):
            item.setdefault("id", item.get("path", ""))
            item.setdefault("label", None)
            item.setdefault("ssh_host", None)
            item.setdefault("ssh_user", None)
            item.setdefault("ssh_key", None)
            item.setdefault("claude_config_dir", None)
            result.append(item)
    return result


def _migrate_wd_active(active: str, history: list[dict]) -> str:
    """Ensure active working_directory is an entry id (not a raw path)."""
    ids = {e["id"] for e in history}
    if active in ids:
        return active
    # Maybe it's a legacy path — find a matching entry
    for entry in history:
        if entry["path"] == active and not entry.get("ssh_host"):
            return entry["id"]
    # Fallback: first entry
    return history[0]["id"] if history else active


def _save_config(data: dict[str, Any]) -> None:
    path = _get_config_path()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _default_config() -> dict[str, Any]:
    default_path = str(PROJECT_ROOT)
    default_entry = {"id": default_path, "path": default_path, "label": None, "ssh_host": None, "ssh_user": None, "ssh_key": None, "claude_config_dir": None}
    return {
        "working_directory": default_path,
        "working_directory_history": [default_entry],
        "enabled_mcps": [],   # empty = all enabled (legacy behavior)
        "disabled_skills": [],  # list of skill names to hide
        "disabled_agents": [],  # list of agent names to hide
        "chrome_extension": False,  # launch sessions with --chrome flag
        "default_model": "claude-sonnet-4-5-20250929",  # default model for orchestrator
    }


def _find_active_entry(config: dict[str, Any]) -> dict | None:
    """Return the WorkingDirectoryEntry dict for the currently-active working_directory."""
    active_id = config.get("working_directory", "")
    for entry in config.get("working_directory_history", []):
        if entry["id"] == active_id:
            return entry
    return None


# -----------------------------------------------------------------------
# Pydantic models
# -----------------------------------------------------------------------

class WorkingDirectoryEntry(BaseModel):
    """A working directory target — local or remote via SSH."""
    id: str                  # Unique stable identifier (path for local, host:path for SSH)
    path: str                # Absolute path on the target machine
    label: str | None = None # Optional human-readable name
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_key: str | None = None          # Path to private key file (on the local machine)
    claude_config_dir: str | None = None  # Override CLAUDE_CONFIG_DIR on the remote machine


class ConfigUpdate(BaseModel):
    working_directory: str | None = None  # entry id to set as active
    working_directory_history: list[WorkingDirectoryEntry] | None = None  # full replacement
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

    if body.working_directory_history is not None:
        validated: list[dict] = []
        for entry in body.working_directory_history:
            e = entry.model_dump()
            if entry.ssh_host:
                # Remote entry — we trust the user; we cannot validate a remote path locally.
                # Ensure the id is set to host:path if not explicitly set.
                if not e["id"] or e["id"] == e["path"]:
                    e["id"] = f"{entry.ssh_host}:{entry.path}"
            else:
                # Local entry — validate the path exists.
                if not Path(entry.path).is_dir():
                    raise HTTPException(status_code=400, detail=f"Directory does not exist: {entry.path}")
                if not e["id"] or e["id"] == "":
                    e["id"] = entry.path
            validated.append(e)
        config["working_directory_history"] = validated[:20]
        # If current active id was removed, reset to first entry
        active_ids = {e["id"] for e in validated}
        if config["working_directory"] not in active_ids:
            config["working_directory"] = validated[0]["id"] if validated else ""

    if body.working_directory is not None:
        new_id = body.working_directory
        history: list[dict] = config.get("working_directory_history", [])
        ids = {e["id"] for e in history}
        if new_id not in ids:
            raise HTTPException(status_code=400, detail=f"Unknown working directory id: {new_id}")
        config["working_directory"] = new_id

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
