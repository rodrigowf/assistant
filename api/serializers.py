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
