"""Orchestrator agent — the main agent loop with non-blocking tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestrator.config import OrchestratorConfig
from orchestrator.prompt import build_system_prompt
from orchestrator.providers.base import ModelProvider
from orchestrator.tools import ToolRegistry
from orchestrator.types import (
    ErrorEvent,
    NestedSessionEvent,
    OrchestratorEvent,
    TextComplete,
    TextDelta,
    ToolExecutingEvent,
    ToolProgressEvent,
    ToolResultEvent,
    ToolUseStart,
    TurnComplete,
)

logger = logging.getLogger(__name__)

MAX_TOOL_LOOPS = 20  # Safety limit to prevent infinite tool loops
HEARTBEAT_INTERVAL = 5.0  # Seconds between progress updates for long-running tools


class OrchestratorAgent:
    """Agent loop that calls a model provider and executes tools.

    Tool execution is non-blocking: events are streamed via an async queue
    so the WebSocket can continue sending updates to the frontend during
    long-running tool operations.

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
        requests tool calls, executes them NON-BLOCKING and yields
        progress events during execution. This ensures the WebSocket
        never stalls during long-running tool operations.
        """
        self._interrupted = False

        # Add user message to history
        self._history.append({"role": "user", "content": prompt})

        # Build system prompt with conversation history for context continuity
        system = build_system_prompt(
            self._config,
            self._context,
            history=self._history,
        )
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

            if self._interrupted:
                yield ErrorEvent(error="interrupted", detail="Agent was interrupted during tool execution")
                return

            # Execute tools NON-BLOCKING with streaming progress updates
            async for event in self._execute_tools_streaming(tool_calls):
                yield event

                if isinstance(event, ErrorEvent) and event.error == "interrupted":
                    return

            # Collect tool results for history (they were already yielded)
            tool_results: list[dict[str, Any]] = []
            for tc in tool_calls:
                # Results are tracked in the streaming execution
                result = self._last_tool_results.get(tc.tool_call_id)
                if result:
                    is_error = False
                    try:
                        parsed = json.loads(result)
                        is_error = "error" in parsed
                    except (json.JSONDecodeError, TypeError):
                        pass
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.tool_call_id,
                        "content": result,
                        **({"is_error": True} if is_error else {}),
                    })

            # Add tool results to history
            if tool_results:
                self._history.append({"role": "user", "content": tool_results})

        yield TurnComplete(
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )

    async def _execute_tools_streaming(
        self, tool_calls: list[ToolUseStart]
    ) -> AsyncIterator[OrchestratorEvent]:
        """Execute tools with non-blocking streaming of progress events.

        Uses an async queue to decouple tool execution from event yielding.
        This allows the WebSocket to continue sending events (heartbeats,
        nested session events) while tools are executing.
        """
        # Track results for history
        self._last_tool_results: dict[str, str] = {}

        # Event queue for non-blocking streaming
        event_queue: asyncio.Queue[OrchestratorEvent | None] = asyncio.Queue()

        # Track which tools are still running
        pending_tools: dict[str, asyncio.Task] = {}
        start_times: dict[str, float] = {}

        async def execute_single_tool(tc: ToolUseStart) -> None:
            """Execute a single tool and put result in queue."""
            start_time = time.monotonic()
            start_times[tc.tool_call_id] = start_time

            # Signal that execution has started
            await event_queue.put(ToolExecutingEvent(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
            ))

            try:
                result = await self._registry.execute(
                    tc.tool_name, tc.tool_input, self._context
                )
                self._last_tool_results[tc.tool_call_id] = result

                is_error = False
                try:
                    parsed = json.loads(result)
                    is_error = "error" in parsed
                except (json.JSONDecodeError, TypeError):
                    pass

                await event_queue.put(ToolResultEvent(
                    tool_call_id=tc.tool_call_id,
                    output=result,
                    is_error=is_error,
                ))
            except asyncio.CancelledError:
                # Tool was cancelled (e.g., interrupt)
                await event_queue.put(ToolResultEvent(
                    tool_call_id=tc.tool_call_id,
                    output=json.dumps({"error": "Tool execution cancelled"}),
                    is_error=True,
                ))
            except Exception as e:
                logger.exception("Tool execution failed: %s", tc.tool_name)
                error_result = json.dumps({"error": str(e)})
                self._last_tool_results[tc.tool_call_id] = error_result
                await event_queue.put(ToolResultEvent(
                    tool_call_id=tc.tool_call_id,
                    output=error_result,
                    is_error=True,
                ))
            finally:
                pending_tools.pop(tc.tool_call_id, None)

        async def heartbeat_generator() -> None:
            """Generate periodic heartbeat events for long-running tools."""
            while pending_tools:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not pending_tools:
                    break

                for call_id, task in list(pending_tools.items()):
                    if task.done():
                        continue
                    # Find the tool name
                    tool_name = next(
                        (tc.tool_name for tc in tool_calls if tc.tool_call_id == call_id),
                        "unknown"
                    )
                    elapsed = time.monotonic() - start_times.get(call_id, time.monotonic())
                    await event_queue.put(ToolProgressEvent(
                        tool_call_id=call_id,
                        tool_name=tool_name,
                        elapsed_seconds=elapsed,
                        message=f"Still executing {tool_name}...",
                    ))

        # Start all tool executions concurrently
        for tc in tool_calls:
            task = asyncio.create_task(
                execute_single_tool(tc),
                name=f"tool-{tc.tool_name}-{tc.tool_call_id[:8]}",
            )
            pending_tools[tc.tool_call_id] = task

        # Start heartbeat generator
        heartbeat_task = asyncio.create_task(heartbeat_generator(), name="tool-heartbeat")

        # Yield events as they arrive (non-blocking)
        completed_count = 0
        total_tools = len(tool_calls)

        try:
            while completed_count < total_tools:
                if self._interrupted:
                    # Cancel all pending tools
                    for task in pending_tools.values():
                        task.cancel()
                    heartbeat_task.cancel()
                    yield ErrorEvent(error="interrupted", detail="Agent was interrupted during tool execution")
                    return

                try:
                    # Wait for next event with timeout to check for interrupts
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                    if event is not None:
                        yield event
                        if isinstance(event, ToolResultEvent):
                            completed_count += 1
                except asyncio.TimeoutError:
                    # No event ready, continue loop to check interrupt flag
                    continue

        finally:
            # Clean up heartbeat task
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def interrupt(self) -> None:
        """Interrupt the current agent loop."""
        self._interrupted = True
