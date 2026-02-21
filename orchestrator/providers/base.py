"""Abstract model provider protocol for the orchestrator agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from orchestrator.types import OrchestratorEvent


@runtime_checkable
class ModelProvider(Protocol):
    """Protocol for model providers.

    Implementations must yield OrchestratorEvent instances as the model
    generates its response. This abstraction allows swapping between
    Anthropic, OpenAI, local models, or future WebRTC voice providers.
    """

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Stream a model response as orchestrator events.

        Args:
            messages: Conversation history in API format.
            tools: Tool definitions in Anthropic format.
            system: System prompt string.

        Yields:
            OrchestratorEvent instances (TextDelta, ToolUseStart, TurnComplete, etc.)
        """
        ...
