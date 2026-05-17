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
import shutil
import uuid as _uuid
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
    is_visible_message_default,
)
from .types import MessagePreview, SessionDetail, SessionInfo


__all__ = ["SessionStore", "_extract_text", "_parse_timestamp"]


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp from JSONL."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


class SessionStore:
    """Reads session storage to list past sessions from disk.

    Sessions are stored as JSONL files in:
    - ``context/<session-id>.jsonl`` (Claude)
    - ``context/chats/<session-id>.jsonl`` (Qwen)
    - Anywhere a registered harness's ``session_discoverer`` reports
      (e.g. Gemini at ``~/.gemini/tmp/<label>/chats/session-*.jsonl``).

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
        # External-JSONL lookup populated by every list_sessions() pass.
        # Harnesses whose JSONL lives outside context/ (Gemini) register
        # a ``session_discoverer`` on their HarnessSpec; the store walks
        # those on listing and remembers each session_id → path mapping
        # so per-session methods (get_messages_paginated, rename, etc.)
        # can locate the file without re-walking the whole external tree.
        self._external_paths: dict[str, Path] = {}
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

    def _locate_jsonl(self, session_id: str) -> Path | None:
        """Find the JSONL for *session_id* across all known storage layouts.

        Checks (in order) the Claude root, the Qwen ``chats/`` subdir, the
        cached external-path map populated by :meth:`list_sessions`, and
        finally every harness's :attr:`HarnessSpec.jsonl_path_resolver`
        (so a fresh session id we've never listed still resolves).
        """
        candidate = self._sessions_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
        candidate = self._chats_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
        cached = self._external_paths.get(session_id)
        if cached is not None and cached.is_file():
            return cached
        # Last resort: ask each harness where its JSONL would live.
        from .registry import get_registry
        for spec in get_registry().all().values():
            try:
                for path in spec.jsonl_path_resolver(session_id):
                    if path.is_file():
                        self._external_paths[session_id] = path
                        return path
            except Exception:
                # A buggy resolver shouldn't blow up the whole lookup.
                continue
        return None

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

        # First pass: harness discoverers.  Run before the default scans so
        # discoverer-claimed files (Gemini's ``session-<iso>-<prefix>.jsonl``
        # in context/chats/) are bound to their real session id from the
        # header line, NOT the misleading ``path.stem``.  The default scan
        # below then skips any file the discoverer already claimed.
        from .registry import get_registry
        fresh_external: dict[str, Path] = {}
        claimed_paths: set[Path] = set()
        for spec in get_registry().all().values():
            if spec.session_discoverer is None:
                continue
            try:
                for sid, jsonl_path in spec.session_discoverer(str(self._project_dir)):
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    fresh_external[sid] = jsonl_path
                    claimed_paths.add(jsonl_path)
                    info = self._scan_file(jsonl_path, sid, titles)
                    if info is not None:
                        sessions.append(info)
            except Exception:
                # A buggy discoverer shouldn't blank the whole session list.
                continue
        # Replace the cache wholesale so deleted external files drop out.
        self._external_paths = fresh_external

        # Scan Claude sessions (root level)
        if self._sessions_dir.is_dir():
            for jsonl_path in self._sessions_dir.glob("*.jsonl"):
                if ".sync-conflict-" in jsonl_path.name:
                    continue
                if jsonl_path in claimed_paths:
                    continue
                session_id = jsonl_path.stem
                seen_ids.add(session_id)
                info = self._scan_file(jsonl_path, session_id, titles)
                if info is not None:
                    sessions.append(info)

        # Scan Qwen sessions (chats/ subdir).  Gemini's chats live here too
        # (via the install.sh symlink to ~/.gemini/tmp/<label>) but were
        # already picked up by the discoverer above with their canonical
        # session id; claimed_paths plus the ``session-*`` filename skip
        # keep us from double-counting them.  The latter is a belt to
        # claimed_paths' suspenders: a header-less Gemini file the
        # discoverer rejects would otherwise leak into this scan with its
        # path stem (``session-<iso>-<prefix>``) as a bogus session id.
        if self._chats_dir.is_dir():
            for jsonl_path in self._chats_dir.glob("*.jsonl"):
                if ".sync-conflict-" in jsonl_path.name:
                    continue
                if jsonl_path in claimed_paths:
                    continue
                if jsonl_path.name.startswith("session-"):
                    # Discoverer-owned naming convention — skip.
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
        jsonl_path = self._locate_jsonl(session_id)
        if jsonl_path is None:
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
        jsonl_path = self._locate_jsonl(session_id)
        if jsonl_path is None:
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
        jsonl_path = self._locate_jsonl(session_id)
        if jsonl_path is None:
            return None
        return self._parse_session_info(jsonl_path, session_id)

    def rename_session(self, session_id: str, title: str) -> bool:
        """Store a custom title for a session. Returns True if the session exists."""
        if self._locate_jsonl(session_id) is None:
            return False
        titles = self._load_titles()
        titles[session_id] = title.strip()
        self._save_titles(titles)
        return True

    def delete_session(self, session_id: str, *, skip_index_cleanup: bool = False) -> bool:
        """Soft-delete a session: move its JSONL into context/trash/.

        The vector-index cleanup spawns a chromadb subprocess (multi-second
        cold start) and is the slow part of deletion. Callers that want to
        return to the user immediately can pass ``skip_index_cleanup=True``
        and schedule :func:`remove_session_from_index` themselves (e.g. via
        ``asyncio.to_thread``).  Cleanup is best-effort either way — a
        leftover index chunk is harmless until the next re-index pass.
        """
        jsonl_path = self._locate_jsonl(session_id)
        if jsonl_path is None:
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
        if not skip_index_cleanup:
            remove_session_from_index(session_id, collection_name="history")
        return True

    def duplicate_session(self, session_id: str) -> str | None:
        """Copy a session's JSONL (and title entry) under a fresh UUID.

        Returns the new session_id, or None if the source doesn't exist.
        The copy lives in the same directory as the original (Claude root
        or Qwen chats/), so provider detection works automatically.
        """
        jsonl_path = self._locate_jsonl(session_id)
        if jsonl_path is None:
            return None

        new_id = str(_uuid.uuid4())
        new_path = jsonl_path.with_name(f"{new_id}.jsonl")
        shutil.copy2(str(jsonl_path), str(new_path))

        titles = self._load_titles()
        src_title = titles.get(session_id)
        if src_title:
            titles[new_id] = f"{src_title} (copy)"
            self._save_titles(titles)

        self._info_cache.pop(new_id, None)
        self._provider_cache.pop(new_id, None)
        return new_id

    def truncate_session(self, session_id: str, drop_last_n: int) -> bool:
        """Drop the last ``drop_last_n`` visible messages from a session.

        The cutoff is counted **from the end** of the conversation, which makes
        it pagination-safe: the frontend only needs to know the index of the
        clicked message relative to the bottom of the loaded view, not the
        absolute position in the full JSONL.

        Visible messages = the user/assistant entries the user actually sees,
        which on Claude / Qwen excludes "user" lines whose entire content is
        tool_result blocks (those are protocol wrappers, not real turns).
        Internal events (queue-operation, file-history-snapshot,
        orchestrator_meta, tool_use, tool_result, voice_interrupted, etc.)
        are kept as long as they fall before the cutoff.

        Trailing tool_use / tool_result / system lines that come *after* the
        last kept visible message are also dropped — keeping them around
        would leave dangling tool calls with no follow-up assistant turn.

        Returns True on success, False if the session doesn't exist or
        ``drop_last_n`` is negative or larger than the visible-message count.
        """
        jsonl_path = self._locate_jsonl(session_id)
        if jsonl_path is None:
            return False
        if drop_last_n < 0:
            return False

        # First pass: read every line and tag whether it is a visible message.
        # We need the index of the last visible message we want to keep, so
        # this can't be done in a single forward streaming pass without
        # buffering everything anyway.
        try:
            with open(jsonl_path) as f:
                raw_lines = [raw.rstrip("\n") for raw in f]
        except OSError:
            return False

        # Visibility classification is provider-specific (Gemini's raw shape
        # differs from Claude / Qwen).  Delegate to the adapter when we can
        # detect one; fall back to the protocol default for unrecognized
        # files so a missing detector doesn't silently zero out the count.
        adapter = self._resolve_adapter(jsonl_path)

        def is_visible_message(line: str) -> bool:
            if not line.strip():
                return False
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return False
            if not isinstance(obj, dict):
                return False
            if adapter is not None:
                return adapter.is_visible_message(obj)
            return is_visible_message_default(obj)

        visible_line_indices = [
            i for i, line in enumerate(raw_lines) if is_visible_message(line)
        ]
        total_visible = len(visible_line_indices)

        if drop_last_n == 0:
            # No-op truncate. Still report success so the frontend can refresh.
            return True
        if drop_last_n > total_visible:
            return False

        # Index of the last visible message we want to keep. Trailing
        # non-visible lines (tool_use / tool_result / system events) that
        # come after it would be dangling — drop them too.
        last_keep_pos = visible_line_indices[total_visible - drop_last_n - 1]
        kept_lines = raw_lines[: last_keep_pos + 1]

        tmp_path = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                if kept_lines:
                    f.write("\n".join(kept_lines) + "\n")
            tmp_path.replace(jsonl_path)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

        # Invalidate caches and the vector index for this session.
        self._info_cache.pop(session_id, None)
        self._provider_cache.pop(session_id, None)
        try:
            remove_session_from_index(session_id, collection_name="history")
        except Exception:
            pass
        return True

    def fork_session(self, session_id: str, drop_last_n: int) -> str | None:
        """Duplicate, then truncate the copy by ``drop_last_n`` from the end.

        Returns the new session_id, or None on failure. If truncation fails,
        the duplicate is cleaned up.
        """
        new_id = self.duplicate_session(session_id)
        if new_id is None:
            return None
        if not self.truncate_session(new_id, drop_last_n):
            # Roll back the duplicate so we don't leave orphan JSONLs around.
            # The copy was never added to the vector index, so skip the
            # chromadb cleanup — it's a multi-second cold start on the
            # Jetson and would otherwise dominate the failure response time.
            try:
                self.delete_session(new_id, skip_index_cleanup=True)
            except Exception:
                pass
            return None
        return new_id

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
