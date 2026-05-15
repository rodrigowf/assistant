"""Claude Code harness — session manager + JSONL adapter.

Public surface re-exported here for callers that still import from
``manager.claude``; the canonical dispatch path is through
:mod:`manager.registry`.
"""

from .adapter import ClaudeAdapter
from .session import (
    ClaudeSessionManager,
    SessionAbandoned,
    kill_claude_subprocess,
)

__all__ = [
    "ClaudeAdapter",
    "ClaudeSessionManager",
    "SessionAbandoned",
    "kill_claude_subprocess",
]
