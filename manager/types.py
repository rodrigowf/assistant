"""Shared types for the manager package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Session status
# ---------------------------------------------------------------------------

class SessionStatus(str, Enum):
    """Current state of a SessionManager."""

    IDLE = "idle"
    STREAMING = "streaming"
    TOOL_USE = "tool_use"
    THINKING = "thinking"
    INTERRUPTED = "interrupted"
    DISCONNECTED = "disconnected"


# ---------------------------------------------------------------------------
# Events — yielded by SessionManager.send()
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all session events."""


@dataclass(frozen=True, slots=True)
class TextDelta(Event):
    """A streaming text token."""

    text: str


@dataclass(frozen=True, slots=True)
class TextComplete(Event):
    """A complete assistant text block (after streaming finishes)."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta(Event):
    """A streaming thinking token."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingComplete(Event):
    """A complete thinking block."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolUse(Event):
    """Claude invoked a tool."""

    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult(Event):
    """Result returned from a tool."""

    tool_use_id: str
    output: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class TurnComplete(Event):
    """End of a complete turn (one send→response cycle)."""

    cost: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    num_turns: int = 0
    session_id: str = ""
    is_error: bool = False
    result: str | None = None


@dataclass(frozen=True, slots=True)
class CompactComplete(Event):
    """Compaction completed — conversation was summarized."""

    trigger: str = "manual"  # "manual" or "auto"


# ---------------------------------------------------------------------------
# Session metadata — used by SessionStore
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SessionInfo:
    """Summary metadata for a past session."""

    session_id: str
    started_at: datetime
    last_activity: datetime
    title: str  # first user prompt, truncated
    message_count: int


@dataclass(slots=True)
class ContentBlock:
    """A content block within a message (text, tool_use, or tool_result)."""

    type: str  # "text" | "tool_use" | "tool_result"
    text: str | None = None  # for text blocks
    tool_use_id: str | None = None  # for tool_use and tool_result
    tool_name: str | None = None  # for tool_use
    tool_input: dict[str, Any] | None = None  # for tool_use
    output: str | None = None  # for tool_result
    is_error: bool = False  # for tool_result


@dataclass(slots=True)
class MessagePreview:
    """A single message in a session preview."""

    role: str  # "user" | "assistant" | "system"
    text: str  # primary text content (for backwards compat / display)
    blocks: list[ContentBlock] = field(default_factory=list)
    timestamp: datetime | None = None


@dataclass(slots=True)
class SessionDetail:
    """Full metadata for a session (extends SessionInfo with preview)."""

    session_id: str
    started_at: datetime
    last_activity: datetime
    title: str
    message_count: int
    messages: list[MessagePreview] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Search result — used by HistoryBridge
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SearchResult:
    """A single result from an embedding search."""

    text: str
    file_path: str
    start_line: int
    end_line: int
    file_name: str
    distance: float
