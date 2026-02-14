"""Orchestrator session — wraps OrchestratorAgent with JSONL persistence."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.agent import OrchestratorAgent
from orchestrator.config import OrchestratorConfig
from orchestrator.providers.anthropic import AnthropicProvider
from orchestrator.tools import registry
from orchestrator.types import (
    OrchestratorEvent,
    TextComplete,
    TurnComplete,
)

logger = logging.getLogger(__name__)


def _mangle_path(project_path: str) -> str:
    """Convert an absolute path to Claude Code's mangled directory name."""
    return project_path.rstrip("/").replace("/", "-")


class OrchestratorSession:
    """Manage a single orchestrator conversation with JSONL persistence.

    Usage::

        session = OrchestratorSession(config=config, context=context)
        session_id = await session.start()

        async for event in session.send("Hello"):
            ...

        await session.stop()
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        context: dict[str, Any],
        session_id: str | None = None,
    ) -> None:
        self._config = config
        self._context = context
        self._resume_id = session_id
        self._session_id: str | None = None
        self._agent: OrchestratorAgent | None = None
        self._jsonl_path: Path | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def start(self) -> str:
        """Initialize the session. Returns the session ID."""
        # Import tools to ensure they're registered
        import orchestrator.tools.agent_sessions  # noqa: F401
        import orchestrator.tools.search  # noqa: F401
        import orchestrator.tools.files  # noqa: F401

        # Create provider
        provider = AnthropicProvider(
            model=self._config.model,
            max_tokens=self._config.max_tokens,
        )

        # Create agent
        self._agent = OrchestratorAgent(
            config=self._config,
            registry=registry,
            provider=provider,
            context=self._context,
        )

        # Determine session ID and JSONL path
        if self._resume_id:
            self._session_id = self._resume_id
        else:
            self._session_id = str(uuid.uuid4())

        self._jsonl_path = self._get_jsonl_path()

        # If resuming, load history from JSONL
        if self._resume_id and self._jsonl_path.is_file():
            history = self._load_history()
            self._agent.history = history
        else:
            # Write orchestrator metadata as first line
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self._append_jsonl({
                "type": "orchestrator_meta",
                "orchestrator": True,
                "session_id": self._session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        return self._session_id

    async def send(self, prompt: str) -> AsyncIterator[OrchestratorEvent]:
        """Send a message and yield events. Persists to JSONL."""
        if self._agent is None:
            raise RuntimeError("Session not started")

        # Persist user message
        self._append_jsonl({
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Collect assistant text for persistence
        assistant_text_parts: list[str] = []

        async for event in self._agent.run(prompt):
            if isinstance(event, TextComplete):
                assistant_text_parts.append(event.text)
            yield event

        # Persist assistant response
        if assistant_text_parts:
            self._append_jsonl({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "\n".join(assistant_text_parts),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    async def stop(self) -> None:
        """Clean up the session."""
        self._agent = None

    async def interrupt(self) -> None:
        """Interrupt the current agent run."""
        if self._agent:
            await self._agent.interrupt()

    def _get_jsonl_path(self) -> Path:
        """Get the JSONL file path for this session."""
        import os

        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            base = Path(config_dir)
        else:
            base = Path.home() / ".claude"

        project_dir = str(Path(self._config.project_dir).resolve())
        mangled = _mangle_path(project_dir)
        sessions_dir = base / "projects" / mangled
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"{self._session_id}.jsonl"

    def _append_jsonl(self, data: dict[str, Any]) -> None:
        """Append a line to the session's JSONL file."""
        if self._jsonl_path is None:
            return
        try:
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(data) + "\n")
        except Exception as e:
            logger.warning("Failed to write to JSONL: %s", e)

    def _load_history(self) -> list[dict[str, Any]]:
        """Load conversation history from the JSONL file."""
        if self._jsonl_path is None or not self._jsonl_path.is_file():
            return []

        history: list[dict[str, Any]] = []
        try:
            with open(self._jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type")
                    if msg_type in ("user", "assistant"):
                        msg = obj.get("message", {})
                        history.append({
                            "role": msg.get("role", msg_type),
                            "content": msg.get("content", ""),
                        })
        except Exception as e:
            logger.warning("Failed to load history: %s", e)

        return history
