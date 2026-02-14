"""Orchestrator agent — the main agent loop."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from orchestrator.config import OrchestratorConfig
from orchestrator.prompt import build_system_prompt
from orchestrator.providers.base import ModelProvider
from orchestrator.tools import ToolRegistry
from orchestrator.types import (
    ErrorEvent,
    OrchestratorEvent,
    TextComplete,
    TextDelta,
    ToolResultEvent,
    ToolUseStart,
    TurnComplete,
)

logger = logging.getLogger(__name__)

MAX_TOOL_LOOPS = 20  # Safety limit to prevent infinite tool loops


class OrchestratorAgent:
    """Agent loop that calls a model provider and executes tools.

    Usage::

        agent = OrchestratorAgent(config, registry, provider, context)
        async for event in agent.run("Hello"):
            if isinstance(event, TextDelta):
                print(event.text, end="")
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        registry: ToolRegistry,
        provider: ModelProvider,
        context: dict[str, Any],
    ) -> None:
        self._config = config
        self._registry = registry
        self._provider = provider
        self._context = context
        self._history: list[dict[str, Any]] = []
        self._interrupted = False
        self._current_task: asyncio.Task | None = None

    @property
    def history(self) -> list[dict[str, Any]]:
        return self._history

    @history.setter
    def history(self, value: list[dict[str, Any]]) -> None:
        self._history = value

    async def run(self, prompt: str) -> AsyncIterator[OrchestratorEvent]:
        """Run one user turn through the agent loop.

        Yields events as the model streams its response. If the model
        requests tool calls, executes them and loops back for the next
        model response.
        """
        self._interrupted = False

        # Add user message to history
        self._history.append({"role": "user", "content": prompt})

        system = build_system_prompt(self._config, self._context)
        tools = self._registry.get_definitions()

        total_input_tokens = 0
        total_output_tokens = 0

        for loop_idx in range(MAX_TOOL_LOOPS):
            if self._interrupted:
                yield ErrorEvent(error="interrupted", detail="Agent was interrupted")
                return

            # Collect events from the provider
            assistant_content: list[dict[str, Any]] = []
            tool_calls: list[ToolUseStart] = []
            current_text = ""

            async for event in self._provider.create_message(
                messages=self._history,
                tools=tools,
                system=system,
            ):
                if self._interrupted:
                    yield ErrorEvent(error="interrupted", detail="Agent was interrupted")
                    return

                if isinstance(event, TextDelta):
                    yield event

                elif isinstance(event, TextComplete):
                    current_text = event.text
                    assistant_content.append({"type": "text", "text": event.text})
                    yield event

                elif isinstance(event, ToolUseStart):
                    tool_calls.append(event)
                    assistant_content.append({
                        "type": "tool_use",
                        "id": event.tool_call_id,
                        "name": event.tool_name,
                        "input": event.tool_input,
                    })
                    yield event

                elif isinstance(event, TurnComplete):
                    total_input_tokens += event.input_tokens
                    total_output_tokens += event.output_tokens

                elif isinstance(event, ErrorEvent):
                    yield event
                    return

            # Add assistant message to history
            if assistant_content:
                self._history.append({"role": "assistant", "content": assistant_content})

            # If no tool calls, we're done
            if not tool_calls:
                break

            # Execute tool calls and collect results
            tool_results: list[dict[str, Any]] = []
            for tc in tool_calls:
                if self._interrupted:
                    yield ErrorEvent(error="interrupted", detail="Agent was interrupted during tool execution")
                    return

                result = await self._registry.execute(
                    tc.tool_name, tc.tool_input, self._context
                )

                is_error = False
                try:
                    parsed = json.loads(result)
                    is_error = "error" in parsed
                except (json.JSONDecodeError, TypeError):
                    pass

                yield ToolResultEvent(
                    tool_call_id=tc.tool_call_id,
                    output=result,
                    is_error=is_error,
                )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.tool_call_id,
                    "content": result,
                    **({"is_error": True} if is_error else {}),
                })

            # Add tool results to history
            self._history.append({"role": "user", "content": tool_results})

        yield TurnComplete(
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )

    async def interrupt(self) -> None:
        """Interrupt the current agent loop."""
        self._interrupted = True
