"""Serialize manager Event objects to JSON-compatible dicts."""

from __future__ import annotations

from typing import Any

from manager.types import (
    CompactComplete,
    Event,
    TextComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)


def serialize_event(event: Event) -> dict[str, Any]:
    """Convert a manager Event to a dict suitable for JSON WebSocket frames."""
    if isinstance(event, TextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, TextComplete):
        return {"type": "text_complete", "text": event.text}
    if isinstance(event, ThinkingDelta):
        return {"type": "thinking_delta", "text": event.text}
    if isinstance(event, ThinkingComplete):
        return {"type": "thinking_complete", "text": event.text}
    if isinstance(event, ToolUse):
        return {
            "type": "tool_use",
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "tool_input": event.tool_input,
        }
    if isinstance(event, ToolResult):
        return {
            "type": "tool_result",
            "tool_use_id": event.tool_use_id,
            "output": event.output,
            "is_error": event.is_error,
        }
    if isinstance(event, TurnComplete):
        return {
            "type": "turn_complete",
            "cost": event.cost,
            "usage": event.usage,
            "num_turns": event.num_turns,
            "session_id": event.session_id,
            "is_error": event.is_error,
            "result": event.result,
        }
    if isinstance(event, CompactComplete):
        return {"type": "compact_complete", "trigger": event.trigger}
    return {"type": "unknown"}


def serialize_orchestrator_event(event: object) -> dict[str, Any]:
    """Convert an orchestrator OrchestratorEvent to a JSON-compatible dict."""
    from orchestrator.types import (
        TextDelta as OTextDelta,
        TextComplete as OTextComplete,
        ToolUseStart,
        ToolResultEvent,
        TurnComplete as OTurnComplete,
        ErrorEvent,
    )

    if isinstance(event, OTextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, OTextComplete):
        return {"type": "text_complete", "text": event.text}
    if isinstance(event, ToolUseStart):
        return {
            "type": "tool_use",
            "tool_use_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "tool_input": event.tool_input,
        }
    if isinstance(event, ToolResultEvent):
        return {
            "type": "tool_result",
            "tool_use_id": event.tool_call_id,
            "output": event.output,
            "is_error": event.is_error,
        }
    if isinstance(event, OTurnComplete):
        return {
            "type": "turn_complete",
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
        }
    if isinstance(event, ErrorEvent):
        return {"type": "error", "error": event.error, "detail": event.detail}
    return {"type": "unknown"}
