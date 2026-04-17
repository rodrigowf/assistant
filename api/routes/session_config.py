"""Per-session configuration — overrides for working directory, MCP servers, skills, and agents."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from utils.paths import get_context_dir

logger = logging.getLogger(__name__)

# Keys that are valid in a session config (subset of global config)
_ALLOWED_KEYS = {"working_directory", "enabled_mcps", "chrome_extension"}

_DEFAULTS: dict[str, Any] = {
    "working_directory": None,     # None = inherit active from global config
    "enabled_mcps": None,          # None = inherit from global config
    "chrome_extension": None,      # None = inherit from global config
}


def _config_path(session_id: str) -> Path:
    return get_context_dir() / f"{session_id}.config.json"


def load_session_config(session_id: str) -> dict[str, Any]:
    """Load per-session config from disk. Missing keys default to None (inherit)."""
    path = _config_path(session_id)
    if not path.is_file():
        return dict(_DEFAULTS)
    try:
        with open(path) as f:
            data = json.load(f)
        result = dict(_DEFAULTS)
        for k in _ALLOWED_KEYS:
            if k in data:
                result[k] = data[k]
        return result
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to load session config for %s: %s", session_id, e)
        return dict(_DEFAULTS)


def save_session_config(session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Persist per-session config overrides. Only allowed keys are saved."""
    path = _config_path(session_id)
    current = load_session_config(session_id)
    for k in _ALLOWED_KEYS:
        if k in data:
            current[k] = data[k]
    try:
        with open(path, "w") as f:
            json.dump(current, f, indent=2)
    except IOError as e:
        logger.error("Failed to save session config for %s: %s", session_id, e)
    return current
