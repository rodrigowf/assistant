"""Anthropic model provider — direct API calls via the anthropic package."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from orchestrator.types import (
    OrchestratorEvent,
    TextDelta,
    TextComplete,
    ToolUseStart,
    TurnComplete,
    ErrorEvent,
)

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """Model provider using the Anthropic Messages API with streaming.

    Reads ANTHROPIC_API_KEY from environment (set in .env file).
    """

    def __init__(self, model: str = "claude-sonnet-4-5-20250929", max_tokens: int = 8192) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic()

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Stream a model response, yielding orchestrator events.

        Handles text streaming, tool use accumulation, and usage tracking.
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        # Track state for content block accumulation
        current_block_type: str | None = None
        current_text = ""
        current_tool_id = ""
        current_tool_name = ""
        current_tool_input_json = ""
        input_tokens = 0
        output_tokens = 0

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    event_type = event.type

                    if event_type == "message_start":
                        usage = getattr(event.message, "usage", None)
                        if usage:
                            input_tokens = getattr(usage, "input_tokens", 0)

                    elif event_type == "content_block_start":
                        block = event.content_block
                        if block.type == "text":
                            current_block_type = "text"
                            current_text = ""
                        elif block.type == "tool_use":
                            current_block_type = "tool_use"
                            current_tool_id = block.id
                            current_tool_name = block.name
                            current_tool_input_json = ""

                    elif event_type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            current_text += delta.text
                            yield TextDelta(text=delta.text)
                        elif delta.type == "input_json_delta":
                            current_tool_input_json += delta.partial_json

                    elif event_type == "content_block_stop":
                        if current_block_type == "text" and current_text:
                            yield TextComplete(text=current_text)
                        elif current_block_type == "tool_use":
                            try:
                                tool_input = json.loads(current_tool_input_json) if current_tool_input_json else {}
                            except json.JSONDecodeError:
                                tool_input = {}
                            yield ToolUseStart(
                                tool_call_id=current_tool_id,
                                tool_name=current_tool_name,
                                tool_input=tool_input,
                            )
                        current_block_type = None

                    elif event_type == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage:
                            output_tokens = getattr(usage, "output_tokens", 0)

            yield TurnComplete(input_tokens=input_tokens, output_tokens=output_tokens)

        except anthropic.APIError as e:
            logger.exception("Anthropic API error")
            yield ErrorEvent(error="api_error", detail=str(e))
        except Exception as e:
            logger.exception("Unexpected error in Anthropic provider")
            yield ErrorEvent(error="provider_error", detail=str(e))
