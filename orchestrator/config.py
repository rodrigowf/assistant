"""Configuration for the orchestrator agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from utils.paths import get_memory_dir


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

        # Use context/memory/ directly for the orchestrator memory file
        memory_path = str(get_memory_dir() / "ORCHESTRATOR_MEMORY.md")

        return cls(
            model=os.environ.get("ORCHESTRATOR_MODEL", "claude-sonnet-4-5-20250929"),
            provider=os.environ.get("ORCHESTRATOR_PROVIDER", "anthropic"),
            max_tokens=int(os.environ.get("ORCHESTRATOR_MAX_TOKENS", "8192")),
            project_dir=project_dir,
            memory_path=memory_path,
        )
