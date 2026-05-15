"""SessionStore — list and read past sessions from disk, provider-agnostic.

Scans two locations:
- ``context/*.jsonl`` — Claude Code sessions (flat in root)
- ``context/chats/*.jsonl`` — Qwen Code sessions (in chats/ subdir)

Uses provider adapters (``claude_adapter``, ``qwen_adapter``) to parse each
file's native JSONL format into normalized messages. Provider detection
happens automatically for existing sessions; new sessions should write a
``.provider`` marker file alongside the JSONL.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.paths import (
    get_chats_dir,
    get_sessions_dir,
    get_trash_dir,
)

from .index_utils import remove_session_from_index
from .protocol import (
    ProviderAdapter,
    detect_provider,
    ensure_all_registered,
    extract_text as _extract_text,  # backward-compat re-export
)
from .types import MessagePreview, SessionDetail, SessionInfo


__all__ = ["SessionStore", "_extract_text", "_parse_timestamp"]


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp from JSONL."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


class SessionStore:
    """Reads session storage to list past sessions from disk.

    Sessions are stored as JSONL files in two locations:
    - ``context/<session-id>.jsonl`` (Claude)
    - ``context/chats/<session-id>.jsonl`` (Qwen)

    Each line is a JSON object with a ``type`` field. Provider adapters
    translate native formats into normalized messages.
    """

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir).resolve()
        self._sessions_dir = self._resolve_sessions_dir()
        self._chats_dir = self._resolve_chats_dir()
        # Per-file cache: session_id → (mtime_ns, size, SessionInfo).
        self._info_cache: dict[str, tuple[int, int, SessionInfo]] = {}
        # Provider cache: session_id → ProviderAdapter
        self._provider_cache: dict[str, ProviderAdapter] = {}
        # Ensure all adapters are registered (lazy import to avoid circular deps)
        ensure_all_registered()

    def _resolve_sessions_dir(self) -> Path:
        """Get the Claude sessions directory."""
        project_context = self._project_dir / "context"
        if project_context.is_dir():
            return project_context
        return get_sessions_dir()

    def _resolve_chats_dir(self) -> Path:
        """Get the Qwen chats directory."""
        project_context = self._project_dir / "context"
        if project_context.is_dir():
            return project_context / "chats"
        return get_chats_dir()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[SessionInfo]:
        """List all sessions for this project, sorted by most recent first.

        Scans both ``context/*.jsonl`` (Claude) and ``context/chats/*.jsonl``
        (Qwen). JSONL files that can't be parsed are skipped.

        Uses a per-file (mtime_ns, size) cache: files that haven't changed
        since the last call are not re-read.
        """
        titles = self._load_titles()
        sessions: list[SessionInfo] = []
        seen_ids: set[str] = set()

        # Scan Claude sessions (root level)
        if self._sessions_dir.is_dir():
            for jsonl_path in self._sessions_dir.glob("*.jsonl"):
                if ".sync-conflict-" in jsonl_path.name:
                    continue
                session_id = jsonl_path.stem
                seen_ids.add(session_id)
                info = self._scan_file(jsonl_path, session_id, titles)
                if info is not None:
                    sessions.append(info)

        # Scan Qwen sessions (chats/ subdir)
        if self._chats_dir.is_dir():
            for jsonl_path in self._chats_dir.glob("*.jsonl"):
                if ".sync-conflict-" in jsonl_path.name:
                    continue
                session_id = jsonl_path.stem
                seen_ids.add(session_id)
                info = self._scan_file(jsonl_path, session_id, titles)
                if info is not None:
                    sessions.append(info)

        # Drop cache entries for files that no longer exist
        for stale_id in set(self._info_cache) - seen_ids:
            del self._info_cache[stale_id]

        sessions.sort(key=lambda s: s.last_activity, reverse=True)
        return sessions

    def get_session(self, session_id: str) -> SessionDetail | None:
        """Get full metadata for a specific session."""
        # Try Claude location first, then Qwen
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            jsonl_path = self._chats_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            return None

        adapter = self._resolve_adapter(jsonl_path)
        if adapter is None:
            return None

        messages_raw = adapter.read_messages(jsonl_path)
        if not messages_raw:
            return None

        provider_name = adapter.provider_name
        previews = adapter.to_previews(messages_raw)
        # Attach provider name to previews
        previews = [
            dataclasses.replace(p, provider=provider_name)
            for p in previews
        ]

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
            is_orchestrator=False,  # Qwen doesn't have orchestrator concept
            provider=provider_name,
        )

    def get_preview(
        self, session_id: str, max_messages: int | None = 5
    ) -> list[MessagePreview]:
        """Get the most recent messages from a session."""
        detail = self.get_session(session_id)
        if detail is None:
            return []
        if max_messages is None:
            return detail.messages
        return detail.messages[-max_messages:]

    def get_messages_paginated(
        self,
        session_id: str,
        limit: int = 50,
        before_index: int | None = None,
    ) -> tuple[list[MessagePreview], int, bool]:
        """Get paginated messages from a session."""
        # Try Claude location first, then Qwen
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            jsonl_path = self._chats_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            return [], 0, False

        adapter = self._resolve_adapter(jsonl_path)
        if adapter is None:
            return [], 0, False

        messages_raw = adapter.read_messages(jsonl_path)
        if not messages_raw:
            return [], 0, False

        provider_name = adapter.provider_name
        previews = adapter.to_previews(messages_raw)
        previews = [
            dataclasses.replace(p, provider=provider_name)
            for p in previews
        ]

        total_count = len(previews)

        if before_index is None:
            start_idx = max(0, total_count - limit)
            end_idx = total_count
        else:
            end_idx = min(before_index, total_count)
            start_idx = max(0, end_idx - limit)

        has_more = start_idx > 0
        return previews[start_idx:end_idx], total_count, has_more

    def get_session_info(self, session_id: str) -> SessionInfo | None:
        """Get lightweight summary info for a single session."""
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            jsonl_path = self._chats_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            return None
        return self._parse_session_info(jsonl_path, session_id)

    def rename_session(self, session_id: str, title: str) -> bool:
        """Store a custom title for a session. Returns True if the session exists."""
        # Check both locations
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            jsonl_path = self._chats_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            return False
        titles = self._load_titles()
        titles[session_id] = title.strip()
        self._save_titles(titles)
        return True

    def delete_session(self, session_id: str) -> bool:
        """Soft-delete a session: move its JSONL into context/trash/."""
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            jsonl_path = self._chats_dir / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            return False

        trash_dir = get_trash_dir()
        trash_dir.mkdir(parents=True, exist_ok=True)

        target = trash_dir / jsonl_path.name
        if target.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            target = trash_dir / f"{jsonl_path.stem}.{ts}.jsonl"

        jsonl_path.rename(target)

        titles = self._load_titles()
        if session_id in titles:
            del titles[session_id]
            self._save_titles(titles)
        remove_session_from_index(session_id, collection_name="history")
        return True

    @property
    def sessions_dir(self) -> Path:
        """Path to the Claude sessions directory."""
        return self._sessions_dir

    @property
    def chats_dir(self) -> Path:
        """Path to the Qwen chats directory."""
        return self._chats_dir

    def _titles_path(self) -> Path:
        """Get the titles file path (in context/ root, shared across providers)."""
        return self._sessions_dir / ".titles.json"

    def _load_titles(self) -> dict[str, str]:
        try:
            with open(self._titles_path()) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_titles(self, titles: dict[str, str]) -> None:
        try:
            with open(self._titles_path(), "w") as f:
                json.dump(titles, f)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scan_file(
        self,
        jsonl_path: Path,
        session_id: str,
        titles: dict[str, str],
    ) -> SessionInfo | None:
        """Scan a single JSONL file, using cache if available."""
        try:
            st = jsonl_path.stat()
        except OSError:
            return None

        cached = self._info_cache.get(session_id)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            info = cached[2]
            title = titles.get(session_id) or info.title
            if title != info.title:
                info = dataclasses.replace(info, title=title)
            return info

        info = self._parse_session_info(jsonl_path, session_id, titles)
        if info is not None:
            self._info_cache[session_id] = (st.st_mtime_ns, st.st_size, info)
        return info

    def _parse_session_info(
        self,
        jsonl_path: Path,
        session_id: str,
        titles: dict[str, str] | None = None,
    ) -> SessionInfo | None:
        """Extract summary metadata from a JSONL file using the right adapter."""
        adapter = self._resolve_adapter(jsonl_path)
        if adapter is None:
            return None
        if titles is None:
            titles = self._load_titles()
        info = adapter.parse_session_info(jsonl_path, session_id, titles)
        if info is None:
            return None
        # Attach provider name
        return dataclasses.replace(info, provider=adapter.provider_name)

    def _resolve_adapter(self, jsonl_path: Path) -> ProviderAdapter | None:
        """Resolve the correct provider adapter for a JSONL file.

        Checks the provider cache first, then tries detection.
        """
        session_id = jsonl_path.stem
        if session_id in self._provider_cache:
            return self._provider_cache[session_id]

        # Try detection
        adapter = detect_provider(jsonl_path)
        if adapter is not None:
            self._provider_cache[session_id] = adapter
            return adapter

        return None

    @staticmethod
    def _first_user_text(messages: list[dict]) -> str:
        """Extract the text of the first user message."""
        for msg in messages:
            if msg.get("type") == "user":
                return _extract_text(msg)
        return ""
