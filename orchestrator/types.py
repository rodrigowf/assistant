"""Types for the orchestrator agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Conversation messages
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Message:
    """A message in the orchestrator conversation history."""

    role: str  # "user" | "assistant" | "tool"
    content: str | list[dict[str, Any]] = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_api_dict(self) -> dict[str, Any]:
        """Convert to Anthropic Messages API format."""
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        return d


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call requested by the model."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of executing a tool."""

    tool_use_id: str
    output: str
    is_error: bool = False


# ---------------------------------------------------------------------------
# Orchestrator events — yielded during agent loop
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrchestratorEvent:
    """Base class for orchestrator events."""


@dataclass(frozen=True, slots=True)
class TextDelta(OrchestratorEvent):
    """A streaming text token from the orchestrator."""

    text: str


@dataclass(frozen=True, slots=True)
class TextComplete(OrchestratorEvent):
    """Complete text response from the orchestrator."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolUseStart(OrchestratorEvent):
    """The orchestrator is invoking a tool."""

    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultEvent(OrchestratorEvent):
    """Result from a tool execution."""

    tool_call_id: str
    output: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class TurnComplete(OrchestratorEvent):
    """End of a complete orchestrator turn."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ErrorEvent(OrchestratorEvent):
    """An error occurred during the orchestrator turn."""

    error: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class VoiceInterrupted(OrchestratorEvent):
    """The user interrupted the assistant's voice response (barge-in)."""

    partial_text: str = ""
