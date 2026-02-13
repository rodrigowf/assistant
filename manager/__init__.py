"""Manager — Python wrapper for Claude Code sessions.

Usage::

    from manager import SessionManager, SessionStore, ManagerConfig

    # Start a new session
    async with SessionManager() as sm:
        async for event in sm.send("Hello!"):
            print(event)

    # List past sessions
    store = SessionStore(".")
    for session in store.list_sessions():
        print(session.session_id, session.title)
"""

from .auth import AuthManager
from .config import ManagerConfig
from .session import SessionManager
from .store import SessionStore
from .types import (
    CompactComplete,
    Event,
    MessagePreview,
    SessionDetail,
    SessionInfo,
    SessionStatus,
    TextComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)

__all__ = [
    # Core classes
    "SessionManager",
    "SessionStore",
    "AuthManager",
    "ManagerConfig",
    # Event types
    "Event",
    "TextDelta",
    "TextComplete",
    "ThinkingDelta",
    "ThinkingComplete",
    "ToolUse",
    "ToolResult",
    "TurnComplete",
    "CompactComplete",
    # Data types
    "SessionInfo",
    "SessionDetail",
    "MessagePreview",
    "SessionStatus",
]
