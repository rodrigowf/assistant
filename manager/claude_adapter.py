"""Claude Code JSONL adapter — Claude's native format is already the
normalized shape, so this adapter is mostly identity on the message content.

Claude JSONL characteristics:
- ``type`` field: "user", "assistant", "system", and a handful of internal
  event types ("queue-operation", "ai-title", "attachment", "last-prompt",
  "file-history-snapshot")
- User messages: ``message.content`` is a plain string
- Assistant messages: ``message.content`` is a list of content blocks
- Tool calls/results are embedded as content blocks within messages
"""

from __future__ import annotations

import json
from pathlib import Path

from .protocol import ProviderAdapter, _parse_timestamp, extract_text, register_provider
from .types import SessionInfo


# Backward-compat alias: tests and SessionStore import this name.
_extract_text = extract_text


_CLAUDE_INTERNAL_TYPES = frozenset({
    "queue-operation", "ai-title", "attachment",
    "file-history-snapshot", "last-prompt",
})


class ClaudeAdapter(ProviderAdapter):
    """Adapter for Claude Code's native JSONL format."""

    @property
    def provider_name(self) -> str:
        return "claude"

    def detect_provider(self, jsonl_path: Path) -> bool:
        """Detect Claude format by looking for Claude-specific event types
        or the ``message.content`` shape (vs Qwen's ``message.parts``)."""
        try:
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if obj.get("type") in _CLAUDE_INTERNAL_TYPES:
                        return True

                    msg = obj.get("message")
                    if isinstance(msg, dict) and "content" in msg and "parts" not in msg:
                        return True
        except (OSError, PermissionError):
            pass
        return False

    def read_messages(self, jsonl_path: Path) -> list[dict]:
        """Read user/assistant/system messages from a Claude JSONL file.

        Claude's native format already matches the normalized shape, so
        we just filter to the relevant event types.
        """
        messages: list[dict] = []
        try:
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") in ("user", "assistant", "system"):
                        messages.append(obj)
        except (OSError, PermissionError):
            pass
        return messages

    def parse_session_info(
        self,
        jsonl_path: Path,
        session_id: str,
        titles: dict[str, str] | None = None,
    ) -> SessionInfo | None:
        """Extract summary metadata from a Claude JSONL file."""
        first_user_text: str = ""
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        message_count = 0
        is_orchestrator = False

        try:
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type")
                    ts = obj.get("timestamp")

                    if msg_type == "orchestrator_meta" and obj.get("orchestrator"):
                        is_orchestrator = True

                    if ts:
                        if first_timestamp is None:
                            first_timestamp = ts
                        last_timestamp = ts

                    if msg_type in ("user", "assistant"):
                        message_count += 1
                        if msg_type == "user" and not first_user_text:
                            first_user_text = extract_text(obj)
        except (OSError, PermissionError):
            return None

        if first_timestamp is None:
            return None

        title = (titles or {}).get(session_id) or (
            first_user_text[:100] if first_user_text else "(empty session)"
        )
        return SessionInfo(
            session_id=session_id,
            started_at=_parse_timestamp(first_timestamp),
            last_activity=_parse_timestamp(last_timestamp or first_timestamp),
            title=title,
            message_count=message_count,
            is_orchestrator=is_orchestrator,
        )


register_provider(ClaudeAdapter())
