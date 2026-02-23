"""Configuration for the orchestrator agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _mangle_path(project_path: str) -> str:
    """Convert an absolute path to Claude Code's mangled directory name."""
    return project_path.rstrip("/").replace("/", "-")


@dataclass(slots=True)
class OrchestratorConfig:
    """Configuration for an orchestrator agent session."""

    model: str = "claude-sonnet-4-5-20250929"
    provider: str = "anthropic"
    max_tokens: int = 8192
    project_dir: str = ""
    memory_path: str = ""

    @classmethod
    def load(cls) -> OrchestratorConfig:
        """Load config from environment variables and defaults."""
        project_dir = os.environ.get(
            "ORCHESTRATOR_PROJECT_DIR",
            str(Path(__file__).resolve().parent.parent),
        )

        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            base = Path(config_dir)
        else:
            base = Path.home() / ".claude"

        mangled = _mangle_path(str(Path(project_dir).resolve()))
        memory_path = str(base / "projects" / mangled / "memory" / "ORCHESTRATOR_MEMORY.md")

        return cls(
            model=os.environ.get("ORCHESTRATOR_MODEL", "claude-sonnet-4-5-20250929"),
            provider=os.environ.get("ORCHESTRATOR_PROVIDER", "anthropic"),
            max_tokens=int(os.environ.get("ORCHESTRATOR_MAX_TOKENS", "8192")),
            project_dir=project_dir,
            memory_path=memory_path,
        )
