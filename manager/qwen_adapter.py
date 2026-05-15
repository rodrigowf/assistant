"""Qwen Code JSONL adapter — translates Qwen's native JSONL format into
the normalized message format used by SessionStore and the UI.

Qwen JSONL characteristics:
- ``type`` field: "user", "assistant", "system" (with ``subtype`` for
  "ui_telemetry", "attribution_snapshot", etc.)
- Messages: ``message.parts`` is a list of ``{text, thought?}`` /
  ``{functionCall: {id, name, args}}`` objects
- Qwen uses ``role: "model"`` instead of ``"assistant"``
- System events with subtypes (telemetry, attribution) are skipped for display
- Runtime metadata lives in ``<session-id>.runtime.json`` alongside the JSONL
"""

from __future__ import annotations

import json
from pathlib import Path

from .protocol import ProviderAdapter, _parse_timestamp, register_provider
from .types import SessionInfo


def _extract_text_from_parts(parts: list[dict]) -> str:
    """Extract user-facing text from Qwen's ``message.parts`` list.

    Skips thinking/thought blocks — those are not part of the visible text.
    """
    if not isinstance(parts, list):
        return ""
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text", "")
        if text and not part.get("thought", False):
            text_parts.append(text)
    return "\n".join(text_parts)


def _parts_to_content(parts: list[dict]) -> list[dict]:
    """Convert Qwen ``message.parts`` to normalized content blocks.

    Qwen parts can be one of:
    - ``{text, thought: true}``   → ``{type: "thinking", text}``
    - ``{text}``                  → ``{type: "text", text}``
    - ``{functionCall: {...}}``   → ``{type: "tool_use", id, name, input}``
    """
    content: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue

        if part.get("thought", False):
            text = part.get("text", "")
            if text:
                content.append({"type": "thinking", "text": text})
            continue

        if "text" in part and "functionCall" not in part:
            text = part.get("text", "")
            if text:
                content.append({"type": "text", "text": text})
            continue

        func_call = part.get("functionCall")
        if isinstance(func_call, dict):
            content.append({
                "type": "tool_use",
                "id": func_call.get("id"),
                "name": func_call.get("name"),
                "input": func_call.get("args", {}),
            })

    return content


class QwenAdapter(ProviderAdapter):
    """Adapter for Qwen Code's native JSONL format."""

    @property
    def provider_name(self) -> str:
        return "qwen"

    def detect_provider(self, jsonl_path: Path) -> bool:
        """Detect Qwen format: ``message.parts`` instead of ``content``,
        ``role: "model"`` for assistant, or system events with ``subtype``."""
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

                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        if "parts" in msg and "content" not in msg:
                            return True
                        if msg.get("role") == "model":
                            return True

                    if obj.get("type") == "system" and "subtype" in obj:
                        return True
        except (OSError, PermissionError):
            pass
        return False

    def read_messages(self, jsonl_path: Path) -> list[dict]:
        """Read user/assistant messages, normalized to the common shape.

        - "model" role → "assistant"
        - ``parts`` → ``content`` blocks
        - ``functionCall`` → ``tool_use`` block
        - System events (telemetry, attribution) are skipped
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

                    msg_type = obj.get("type")
                    if msg_type not in ("user", "assistant"):
                        continue

                    msg = obj.get("message", {})
                    parts = msg.get("parts", [])
                    role = msg.get("role", "")
                    if role == "model":
                        role = "assistant"

                    normalized: dict = {
                        "type": msg_type,
                        "uuid": obj.get("uuid"),
                        "parentUuid": obj.get("parentUuid"),
                        "timestamp": obj.get("timestamp"),
                        "sessionId": obj.get("sessionId"),
                        "message": {
                            "role": role,
                            "content": _parts_to_content(parts),
                        },
                    }
                    if "usageMetadata" in obj:
                        normalized["usageMetadata"] = obj["usageMetadata"]
                        normalized["model"] = obj.get("model", "")
                    messages.append(normalized)
        except (OSError, PermissionError):
            pass
        return messages

    def parse_session_info(
        self,
        jsonl_path: Path,
        session_id: str,
        titles: dict[str, str] | None = None,
    ) -> SessionInfo | None:
        """Extract summary metadata from a Qwen JSONL file."""
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
                    if msg_type == "system":
                        continue

                    ts = obj.get("timestamp")
                    if ts:
                        if first_timestamp is None:
                            first_timestamp = ts
                        last_timestamp = ts

                    if msg_type == "user":
                        message_count += 1
                        if not first_user_text:
                            parts = obj.get("message", {}).get("parts", [])
                            first_user_text = _extract_text_from_parts(parts)
                    elif msg_type == "assistant":
                        message_count += 1
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
            is_orchestrator=False,
        )


register_provider(QwenAdapter())
