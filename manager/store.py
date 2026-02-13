"""SessionStore — list and read past Claude Code sessions from disk."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import ContentBlock, MessagePreview, SessionDetail, SessionInfo


def _mangle_path(project_path: str) -> str:
    """Convert an absolute path to Claude Code's mangled directory name.

    ``/home/rodrigo/Projects/assistant`` becomes
    ``-home-rodrigo-Projects-assistant``.
    """
    return project_path.rstrip("/").replace("/", "-")


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp from Claude Code JSONL."""
    # Handles both "2026-02-05T01:48:05.911Z" and similar formats
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def _extract_text(message: dict) -> str:
    """Extract plain text from a JSONL message dict."""
    msg = message.get("message", {})
    content = msg.get("content", "")

    if isinstance(content, str):
        return content

    # content is a list of blocks
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "\n".join(parts)


def _extract_blocks(message: dict) -> list[ContentBlock]:
    """Extract all content blocks from a JSONL message dict."""
    msg = message.get("message", {})
    content = msg.get("content", "")
    blocks: list[ContentBlock] = []

    # Simple string content
    if isinstance(content, str):
        if content:
            blocks.append(ContentBlock(type="text", text=content))
        return blocks

    # Content is a list of blocks
    for block in content:
        if not isinstance(block, dict):
            continue

        btype = block.get("type", "")

        if btype == "text":
            text = block.get("text", "")
            if text:
                blocks.append(ContentBlock(type="text", text=text))

        elif btype == "tool_use":
            blocks.append(ContentBlock(
                type="tool_use",
                tool_use_id=block.get("id"),
                tool_name=block.get("name"),
                tool_input=block.get("input", {}),
            ))

        elif btype == "tool_result":
            # tool_result content can be string or list
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                # Extract text from list of content blocks
                result_parts = []
                for item in result_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        result_parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        result_parts.append(item)
                result_content = "\n".join(result_parts)

            blocks.append(ContentBlock(
                type="tool_result",
                tool_use_id=block.get("tool_use_id"),
                output=str(result_content) if result_content else "",
                is_error=block.get("is_error", False),
            ))

    return blocks


class SessionStore:
    """Reads Claude Code's local session storage to list past sessions.

    Sessions are stored as JSONL files at::

        ~/.claude/projects/<mangled-project-path>/<session-id>.jsonl

    Each line is a JSON object with a ``type`` field: ``user``, ``assistant``,
    ``system``, ``progress``, ``file-history-snapshot``, ``queue-operation``.
    """

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = str(Path(project_dir).resolve())
        self._sessions_dir = self._resolve_sessions_dir()

    def _resolve_sessions_dir(self) -> Path:
        """Compute the path to Claude Code's session directory for this project.

        Respects CLAUDE_CONFIG_DIR if set, otherwise uses ~/.claude.
        """
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            base = Path(config_dir)
        else:
            base = Path.home() / ".claude"
        mangled = _mangle_path(self._project_dir)
        return base / "projects" / mangled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[SessionInfo]:
        """List all sessions for this project, sorted by most recent first."""
        if not self._sessions_dir.is_dir():
            return []

        sessions: list[SessionInfo] = []
        for jsonl_path in self._sessions_dir.glob("*.jsonl"):
            session_id = jsonl_path.stem
            info = self._parse_session_info(jsonl_path, session_id)
            if info is not None:
                sessions.append(info)

        sessions.sort(key=lambda s: s.last_activity, reverse=True)
        return sessions

    def get_session(self, session_id: str) -> SessionDetail | None:
        """Get full metadata for a specific session."""
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            return None

        messages_raw = self._read_messages(jsonl_path)
        if not messages_raw:
            return None

        previews = self._to_previews(messages_raw)
        first_user = self._first_user_text(messages_raw)
        timestamps = [m.get("timestamp") for m in messages_raw if m.get("timestamp")]

        started = _parse_timestamp(timestamps[0]) if timestamps else datetime.now(timezone.utc)
        last = _parse_timestamp(timestamps[-1]) if timestamps else started

        return SessionDetail(
            session_id=session_id,
            started_at=started,
            last_activity=last,
            title=first_user[:100] if first_user else "(empty session)",
            message_count=len([m for m in messages_raw if m["type"] in ("user", "assistant")]),
            messages=previews,
        )

    def get_preview(self, session_id: str, max_messages: int = 5) -> list[MessagePreview]:
        """Get a preview of the most recent messages from a session."""
        detail = self.get_session(session_id)
        if detail is None:
            return []
        return detail.messages[-max_messages:]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session's JSONL file. Returns True if deleted."""
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if jsonl_path.is_file():
            jsonl_path.unlink()
            return True
        return False

    @property
    def sessions_dir(self) -> Path:
        """Path to the sessions directory."""
        return self._sessions_dir

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_messages(self, jsonl_path: Path) -> list[dict]:
        """Read all user/assistant/system message lines from a JSONL file."""
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

    def _parse_session_info(self, jsonl_path: Path, session_id: str) -> SessionInfo | None:
        """Extract summary metadata from a JSONL file without loading all messages."""
        first_user_text: str = ""
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        message_count = 0

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

                    if ts:
                        if first_timestamp is None:
                            first_timestamp = ts
                        last_timestamp = ts

                    if msg_type in ("user", "assistant"):
                        message_count += 1
                        if msg_type == "user" and not first_user_text:
                            first_user_text = _extract_text(obj)
        except (OSError, PermissionError):
            return None

        if first_timestamp is None:
            return None

        return SessionInfo(
            session_id=session_id,
            started_at=_parse_timestamp(first_timestamp),
            last_activity=_parse_timestamp(last_timestamp or first_timestamp),
            title=first_user_text[:100] if first_user_text else "(empty session)",
            message_count=message_count,
        )

    def _first_user_text(self, messages: list[dict]) -> str:
        """Extract the text of the first user message."""
        for msg in messages:
            if msg.get("type") == "user":
                return _extract_text(msg)
        return ""

    def _to_previews(self, messages: list[dict]) -> list[MessagePreview]:
        """Convert raw messages to MessagePreview objects."""
        previews: list[MessagePreview] = []
        for msg in messages:
            msg_type = msg.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            role = msg_type
            text = _extract_text(msg)
            blocks = _extract_blocks(msg)
            ts = msg.get("timestamp")
            timestamp = _parse_timestamp(ts) if ts else None

            previews.append(MessagePreview(
                role=role,
                text=text[:500],
                blocks=blocks,
                timestamp=timestamp,
            ))
        return previews
