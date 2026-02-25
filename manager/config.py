"""Manager configuration — loads from file, env vars, or direct construction."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent.parent


# Type alias for MCP server configuration
McpServerConfig = dict  # Can be McpStdioServerConfig, McpSSEServerConfig, etc.


@dataclass
class ManagerConfig:
    """Configuration for the manager library."""

    project_dir: str = str(_DEFAULT_PROJECT_DIR)
    model: str | None = None
    permission_mode: str = "default"
    max_budget_usd: float | None = None
    max_turns: int | None = None
    mcp_servers: dict[str, McpServerConfig] | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> ManagerConfig:
        """Load config from a JSON file, falling back to env vars and defaults.

        Lookup order for each field:
        1. JSON file value (if file exists)
        2. Environment variable (MANAGER_PROJECT_DIR, MANAGER_MODEL, etc.)
        3. Dataclass default
        """
        data: dict = {}

        # Try loading from file
        if path is None:
            path = Path(_DEFAULT_PROJECT_DIR) / ".manager.json"
        else:
            path = Path(path)

        if path.is_file():
            with open(path) as f:
                data = json.load(f)

        # Overlay env vars (env takes precedence over file)
        env_map = {
            "project_dir": "MANAGER_PROJECT_DIR",
            "model": "MANAGER_MODEL",
            "permission_mode": "MANAGER_PERMISSION_MODE",
            "max_budget_usd": "MANAGER_MAX_BUDGET_USD",
            "max_turns": "MANAGER_MAX_TURNS",
        }

        for field_name, env_key in env_map.items():
            env_val = os.environ.get(env_key)
            if env_val is not None:
                data[field_name] = env_val

        # Coerce types
        if "max_budget_usd" in data and data["max_budget_usd"] is not None:
            data["max_budget_usd"] = float(data["max_budget_usd"])
        if "max_turns" in data and data["max_turns"] is not None:
            data["max_turns"] = int(data["max_turns"])

        # Filter to known fields only
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}

        return cls(**filtered)
