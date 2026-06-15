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
    summary: str = ""  # The summary text generated during compaction


@dataclass(frozen=True, slots=True)
class PermissionRequest(Event):
    """The SDK is asking us whether a tool may run.

    Emitted when the bundled CLI's permission gate fires (e.g. ``ExitPlanMode``).
    The wrapper resolves these via :meth:`SessionManager.resolve_permission`;
    until then the SDK is blocked waiting for our reply.
    """

    request_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PermissionResolved(Event):
    """A pending permission request was answered — emitted so subscribers can
    close any open UI / orchestrator state. ``decision`` is "allow" or "deny";
    ``responder`` identifies who answered ("user" | "orchestrator")."""

    request_id: str
    decision: str
    responder: str
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SessionStalled(Event):
    """Emitted when the SDK has produced no message for an extended period.

    The underlying stream is *not* aborted — the watchdog is purely advisory
    so the UI can surface a "this looks stuck" banner with an interrupt
    affordance.  Repeated emissions while the stall persists carry the
    cumulative ``elapsed_seconds`` since the last received message.
    """

    elapsed_seconds: float
    last_tool_name: str | None = None
    last_tool_use_id: str | None = None


class TerminationReason(str, Enum):
    """Why a session was removed from the pool.

    Surfaced to clients in the :class:`SessionTerminated` event so they
    can render context-appropriate UI and decide whether to offer an
    auto-resume affordance.
    """

    #: The SDK receive loop raised a fatal exception (e.g. the bundled
    #: ``claude`` subprocess exited unexpectedly, SSH transport died,
    #: SDK parse error).  Almost always recoverable by opening a new
    #: session that resumes from the same SDK session id — the JSONL on
    #: disk is intact.
    SUBPROCESS_CRASHED = "subprocess_crashed"

    #: The receive loop drained ``receive_messages()`` without an
    #: exception, but no terminal ``ResultMessage`` arrived first.  The
    #: subprocess shut itself down cleanly mid-turn — typically because
    #: the SSH connection closed.  Same recovery as ``SUBPROCESS_CRASHED``.
    SUBPROCESS_LOST = "subprocess_lost"

    #: An explicit ``pool.close(session_id)`` call — usually because the
    #: user clicked Close or the orchestrator decided to retire it.
    CLOSED_BY_USER = "closed_by_user"

    #: A new session with the same ``local_id`` was created.  The old
    #: instance was retired so the new one could take its place.
    REPLACED = "replaced"

    #: The pre-start reachability check failed (e.g. SSH host
    #: unreachable).  No subprocess was ever spawned.  Recovery
    #: requires bringing the host back online.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class SessionTerminated(Event):
    """Emitted exactly once when a session leaves the pool.

    Replaces the legacy generic ``error: send_failed`` for the
    session-death case.  Carries enough metadata for the client to:

    * tell the user *why* the session ended,
    * decide whether automatic recovery is appropriate (it almost
      always is for ``SUBPROCESS_*`` reasons — the JSONL is intact
      and a fresh session can resume from the same ``sdk_session_id``),
    * suppress the optimistic "retry" affordances that would only
      result in the same failure.

    Symmetric counterpart to ``session_started`` — exactly one of each
    per session lifecycle, no exceptions.
    """

    reason: TerminationReason
    detail: str | None = None
    #: SDK session id at termination time, if known.  The client uses
    #: this to open a fresh local session resuming from the on-disk
    #: JSONL (the canonical record survives the in-memory crash).
    sdk_session_id: str | None = None


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
    is_orchestrator: bool = False
    provider: str = "claude"  # registered harness id — detected or from marker


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
    provider: str = "claude"  # registered harness id — inherited from session


@dataclass(slots=True)
class SessionDetail:
    """Full metadata for a session (extends SessionInfo with preview)."""

    session_id: str
    started_at: datetime
    last_activity: datetime
    title: str
    message_count: int
    messages: list[MessagePreview] = field(default_factory=list)
    is_orchestrator: bool = False
    provider: str = "claude"  # registered harness id


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
