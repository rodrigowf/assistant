"""Backward-compatibility shim.

Historically this module hosted ``SessionManager`` directly. After the
multi-provider refactor, the Claude implementation moved to
:mod:`manager.claude_session` and the Qwen implementation lives in
:mod:`manager.qwen_session`. This module re-exports the Claude session
manager and its surrounding helpers so existing imports
(``from manager.session import SessionManager``) keep working — including
test patches that target ``manager.session.ClaudeSDKClient`` etc.
"""

from __future__ import annotations

from .claude_session import (  # noqa: F401
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSessionManager,
    RemoteHostUnreachableError,
    SessionAbandoned,
    SessionManager,
    _DEFAULT_GATED_TOOLS,
    _extract_subprocess_pid,
    _looks_like_claude,
    _patch_sdk_message_parser,
    _PERMISSION_GATING_PROMPT,
    _probe_ssh_host_reachable,
    _process_alive,
    _process_comm,
    _REMOTE_CLAUDE_PATH_CACHE,
    _REMOTE_CLAUDE_PATH_LOCK,
    _STALL_FIRST_NOTICE_S,
    _STALL_REPEAT_INTERVAL_S,
    _TURN_ABANDON_S,
    clear_remote_claude_path_cache,
    kill_claude_subprocess,
    logger,
)

__all__ = [
    "SessionManager",
    "ClaudeSessionManager",
    "SessionAbandoned",
    "RemoteHostUnreachableError",
    "clear_remote_claude_path_cache",
    "kill_claude_subprocess",
]
