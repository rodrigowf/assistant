"""Manager — Python wrapper for agent sessions (Claude Code / Qwen Code).

Usage::

    from manager import SessionManager, SessionStore, ManagerConfig

    # Start a new session (defaults to Claude)
    async with SessionManager() as sm:
        async for event in sm.send("Hello!"):
            print(event)

    # List past sessions (both providers)
    store = SessionStore(".")
    for session in store.list_sessions():
        print(session.session_id, session.provider, session.title)
"""

from .auth import AuthManager
from .base_session import BaseSessionManager
from .claude_session import ClaudeSessionManager, SessionAbandoned
from .config import ManagerConfig
from .qwen_session import QwenAbandoned, QwenSessionManager
from .store import SessionStore
from .types import (
    CompactComplete,
    Event,
    MessagePreview,
    PermissionRequest,
    PermissionResolved,
    SessionDetail,
    SessionInfo,
    SessionStalled,
    SessionStatus,
    TextComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)

# Backward-compat alias — historical name for the Claude implementation.
SessionManager = ClaudeSessionManager

__all__ = [
    # Core classes
    "BaseSessionManager",
    "ClaudeSessionManager",
    "QwenSessionManager",
    "SessionManager",  # alias for ClaudeSessionManager
    "SessionStore",
    "AuthManager",
    "ManagerConfig",
    # Errors
    "SessionAbandoned",
    "QwenAbandoned",
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
    "PermissionRequest",
    "PermissionResolved",
    "SessionStalled",
    # Data types
    "SessionInfo",
    "SessionDetail",
    "MessagePreview",
    "SessionStatus",
]
