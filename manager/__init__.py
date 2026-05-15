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

Lazy-loading note
-----------------
``ClaudeSessionManager`` (and its alias ``SessionManager``) lives behind
PEP 562 lazy attribute resolution because importing it eagerly pulls in
``claude-agent-sdk`` — a hard runtime dependency for Claude but pointless
for Qwen-only deployments.  ``QwenSessionManager`` is loaded the same way
for symmetry, even though its imports are cheap.

This means ``from manager import SessionManager`` still works (the lazy
hook resolves the symbol on first access) without the SDK being imported
at ``manager`` package load time — but if you never instantiate or
reference the Claude side, the SDK is never imported at all.
"""

from .auth import AuthManager
from .base_session import BaseSessionManager, TurnAbandoned
from .config import ManagerConfig
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


# Names exported lazily.  Keys map to (module_name, attribute_name).
# Listed under __all__ for IDE/linter awareness even though they aren't
# bound at module load.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ClaudeSessionManager": (".claude_session", "ClaudeSessionManager"),
    "SessionManager": (".claude_session", "ClaudeSessionManager"),  # historical alias
    "SessionAbandoned": (".claude_session", "SessionAbandoned"),
    "QwenSessionManager": (".qwen_session", "QwenSessionManager"),
    "QwenAbandoned": (".qwen_session", "QwenAbandoned"),
}


def __getattr__(name: str):
    """PEP 562 lazy attribute resolution.

    Only fires on attribute access that the normal lookup failed; the eager
    imports above (AuthManager, types, etc.) still take their fast path.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    module = import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value  # cache so future accesses are O(1) and don't re-import
    return value


def __dir__() -> list[str]:
    """Make ``dir(manager)`` advertise the lazy names too."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    # Core classes
    "BaseSessionManager",
    "ClaudeSessionManager",  # lazy
    "QwenSessionManager",  # lazy
    "SessionManager",  # lazy (alias for ClaudeSessionManager)
    "SessionStore",
    "AuthManager",
    "ManagerConfig",
    # Errors
    "TurnAbandoned",  # shared base — catch this to handle both providers
    "SessionAbandoned",  # lazy: Claude-specific subclass
    "QwenAbandoned",  # lazy: Qwen-specific subclass
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
