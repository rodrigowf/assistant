"""Google Gemini CLI JSONL adapter.

Gemini stores per-session JSONL files at::

    ~/.gemini/tmp/<project-label>/chats/session-<short-iso>-<uuid-prefix>.jsonl

where ``<project-label>`` is the value the CLI assigns to the current
working directory in ``~/.gemini/projects.json``, and ``<uuid-prefix>``
is the first 8 chars of the session UUID (the file name does NOT carry
the full session id — we have to peek at the header line to learn it).

JSONL line shapes
-----------------

The on-disk JSONL is a heterogeneous append-only log; lines fall into a
few categories::

    {"sessionId": "...", "projectHash": "...",
     "startTime": "...", "lastUpdated": "...", "kind": "main"}
        — header line, written once per session start (also re-written
        when the same session id is resumed across a fresh CLI launch).

    {"id": "...", "timestamp": "...",
     "type": "user",
     "content": [{"text": "<prompt>"}]}
        — user prompt.  Note ``content`` is a list of objects with a
        ``text`` key (vs. Claude's plain-string user content).

    {"id": "...", "timestamp": "...",
     "type": "gemini",
     "content": "<assistant reply as a plain string>",
     "thoughts": [{"subject": "...", "description": "...", "timestamp": "..."}],
     "tokens": {...},
     "model": "..."}
        — assistant turn.  ``type`` is ``"gemini"`` (not ``"assistant"``),
        ``content`` is a plain string, and ``thoughts`` is a top-level
        array of {subject, description, timestamp} objects (NOT a
        ``thinking`` block inside content).

    {"$set": {"lastUpdated": "..."}}
        — bookkeeping line emitted after every meaningful change.
        Adapter must filter these out.

Tool calls
----------

The on-disk JSONL doesn't (currently) record tool calls in a separate
shape from the assistant text — they appear within the streamed
events instead, captured live by :class:`GeminiSessionManager`.  The
adapter therefore only sees user/assistant messages when re-reading
disk JSONL.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..protocol import ProviderAdapter, _parse_timestamp, extract_text, register_provider
from ..registry import HarnessSpec, register_harness
from ..types import SessionInfo


# ``$set`` lines aren't messages — adapter skips them.
def _is_metadata_line(obj: dict) -> bool:
    return "$set" in obj and "type" not in obj


# Map Gemini's ``type: "gemini"`` to the normalized ``assistant`` role.
def _normalize_type(t: str) -> str | None:
    if t == "user":
        return "user"
    if t == "gemini":
        return "assistant"
    return None


def _normalize_message(obj: dict) -> dict | None:
    """Translate a raw Gemini JSONL message line to the common shape.

    Returns None if the line isn't a user/assistant message.  Matches
    the contract documented on :class:`~manager.protocol.ProviderAdapter`:
    output has ``type`` ∈ {user, assistant}, ``timestamp``, and
    ``message.content`` as either a string (user) or a list of blocks
    (assistant — text blocks plus optional thinking blocks).
    """
    raw_type = obj.get("type")
    role = _normalize_type(raw_type) if isinstance(raw_type, str) else None
    if role is None:
        return None

    timestamp = obj.get("timestamp")

    if role == "user":
        # User content is a list of {text} dicts — join.
        raw_content = obj.get("content")
        if isinstance(raw_content, list):
            parts = [
                p.get("text", "")
                for p in raw_content
                if isinstance(p, dict) and p.get("text")
            ]
            content = "\n".join(parts)
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            content = ""
        return {
            "type": "user",
            "timestamp": timestamp,
            "message": {"role": "user", "content": content},
        }

    # Assistant message: build content blocks.  Start with thoughts
    # (each becomes a thinking block), then the main text body.
    blocks: list[dict] = []
    thoughts = obj.get("thoughts")
    if isinstance(thoughts, list):
        for thought in thoughts:
            if not isinstance(thought, dict):
                continue
            # Gemini thoughts carry both subject + description; concatenate
            # so the normalized view doesn't lose the structure.
            subj = thought.get("subject", "")
            desc = thought.get("description", "")
            text = f"{subj}\n{desc}".strip() if subj or desc else ""
            if text:
                blocks.append({"type": "thinking", "text": text})

    raw_content = obj.get("content")
    if isinstance(raw_content, str) and raw_content:
        blocks.append({"type": "text", "text": raw_content})

    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": blocks},
    }


class GeminiAdapter(ProviderAdapter):
    """Adapter for Google Gemini CLI's native JSONL format."""

    @property
    def provider_name(self) -> str:
        return "gemini"

    def detect_provider(self, jsonl_path: Path) -> bool:
        """Detect Gemini format by looking for its characteristic header line
        (``sessionId`` + ``projectHash`` + ``kind`` field, present on the
        first non-empty line of every Gemini session JSONL) or the
        ``type: "gemini"`` assistant marker that no other harness uses."""
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

                    # Most reliable signature — present on line 1 of every
                    # Gemini session file.  Claude has no "projectHash"
                    # field and Qwen has no "kind" field.
                    if (
                        isinstance(obj, dict)
                        and "sessionId" in obj
                        and "projectHash" in obj
                        and "kind" in obj
                    ):
                        return True

                    # Fallback for sessions where the header line is
                    # somehow missing or malformed: the assistant role
                    # ``"gemini"`` is unique to this harness.
                    if obj.get("type") == "gemini":
                        return True
        except (OSError, PermissionError):
            pass
        return False

    def read_messages(self, jsonl_path: Path) -> list[dict]:
        """Read user/assistant messages from a Gemini JSONL file, normalized.

        Skips the header line and ``$set`` bookkeeping markers; converts
        each remaining user/gemini line to the common message shape.
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
                    if _is_metadata_line(obj):
                        continue
                    msg = _normalize_message(obj)
                    if msg is not None:
                        messages.append(msg)
        except (OSError, PermissionError):
            pass
        return messages

    def parse_session_info(
        self,
        jsonl_path: Path,
        session_id: str,
        titles: dict[str, str] | None = None,
    ) -> SessionInfo | None:
        """Extract summary metadata from a Gemini JSONL file.

        Reads the header line for start time and the first user message
        for the title; counts user+gemini lines for the message count.
        """
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

                    # Header line: take startTime as the canonical first
                    # timestamp and lastUpdated as the running tail.
                    if (
                        isinstance(obj, dict)
                        and "sessionId" in obj
                        and "startTime" in obj
                    ):
                        if first_timestamp is None:
                            first_timestamp = obj.get("startTime")
                        last = obj.get("lastUpdated") or obj.get("startTime")
                        if last:
                            last_timestamp = last
                        continue

                    # $set lines: just bump the last_timestamp.
                    if _is_metadata_line(obj):
                        new_last = obj.get("$set", {}).get("lastUpdated")
                        if new_last:
                            last_timestamp = new_last
                        continue

                    ts = obj.get("timestamp")
                    if ts:
                        if first_timestamp is None:
                            first_timestamp = ts
                        last_timestamp = ts

                    role = _normalize_type(obj.get("type", ""))
                    if role in ("user", "assistant"):
                        message_count += 1
                        if role == "user" and not first_user_text:
                            normalized = _normalize_message(obj)
                            if normalized is not None:
                                first_user_text = extract_text(normalized)
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
        )


_adapter = GeminiAdapter()
register_provider(_adapter)


# ---------------------------------------------------------------------------
# HarnessSpec registration
# ---------------------------------------------------------------------------


def _load_gemini_session_class():
    from .session import GeminiSessionManager
    return GeminiSessionManager


def _load_gemini_kill_helper():
    # Gemini runs as Node.js (same as Qwen), so /proc/<pid>/comm shows up
    # as ``node``.  See the orphan reaper note in :mod:`manager.registry`
    # — the registry dispatches by spec name from ``_tracked_pids``, so
    # sharing a comm prefix with Qwen doesn't cause misdirected kills.
    from .._proc import kill_subprocess

    def kill_gemini_subprocess(pid: int, *, sigterm_grace_s: float = 0.5) -> bool:
        return kill_subprocess(pid, comm_prefix="node", sigterm_grace_s=sigterm_grace_s)

    return kill_gemini_subprocess


def _gemini_home() -> Path:
    """Resolve the Gemini CLI's storage directory.

    Honors ``GEMINI_HOME`` if set; falls back to ``~/.gemini`` (the CLI's
    own default).  Used by both the JSONL path resolver and the
    list-models endpoint.
    """
    explicit = os.environ.get("GEMINI_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".gemini"


def _gemini_project_label(project_dir: str | None = None) -> str | None:
    """Return the Gemini CLI's per-project subdirectory name for *project_dir*.

    Gemini maintains ``~/.gemini/projects.json`` mapping absolute cwd
    strings to short labels (e.g. ``/home/rodrigo/assistant`` → ``assistant``).
    Session JSONL files live under ``~/.gemini/tmp/<label>/chats/``, so
    we need this lookup to find the right directory.

    Returns None if the project hasn't been registered yet (i.e. the
    user hasn't run ``gemini`` from that directory).  The session
    manager handles this case by deriving the label from the cwd basename
    instead (which is what the CLI itself does on first run).
    """
    projects_file = _gemini_home() / "projects.json"
    if not projects_file.is_file():
        return None
    try:
        data = json.loads(projects_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    projects = data.get("projects", {}) if isinstance(data, dict) else {}
    if not isinstance(projects, dict):
        return None
    # If a project_dir was given, look it up; otherwise return None.
    if project_dir is None:
        return None
    label = projects.get(project_dir)
    return label if isinstance(label, str) else None


def _gemini_jsonl_candidates(session_id: str) -> list[Path]:
    """Return candidate JSONL paths for *session_id*.

    Gemini's file name format is ``session-<short-iso>-<uuid-prefix>.jsonl``
    where ``<uuid-prefix>`` is the first 8 chars of the session UUID.
    The session id alone isn't enough to construct a deterministic path,
    so we glob every project's chats directory looking for a matching
    file.  This is O(projects × files) but both numbers are tiny in
    practice (single-digit projects, low double-digit chats each).

    Returns a list (possibly empty) of existing JSONL paths whose name
    contains the session-id's first 8 chars.  Caller is_file()-checks
    each as usual.
    """
    short = session_id[:8]
    if not short:
        return []
    tmp_root = _gemini_home() / "tmp"
    if not tmp_root.is_dir():
        return []
    out: list[Path] = []
    # Walk every project's chats/ subdir.  Defensive try/except — a
    # corrupted or permission-denied subdir shouldn't blow up the resume
    # sniffer for the whole UI.
    try:
        for project_dir in tmp_root.iterdir():
            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue
            try:
                for f in chats_dir.glob(f"*-{short}.jsonl"):
                    if f.is_file():
                        out.append(f)
            except OSError:
                continue
    except OSError:
        return []
    return out


def _gemini_discover_sessions(project_dir: str):
    """Yield ``(session_id, jsonl_path)`` for Gemini sessions in *project_dir*.

    Gemini's storage is global (``~/.gemini/tmp/<label>/chats/``) but each
    label corresponds to a specific cwd via ``projects.json``.  We resolve
    the label for *project_dir* and only walk that one chats directory,
    so the store stays scoped to the project the user is actually viewing
    (tests with isolated cwds get an empty result instead of leaking the
    developer's real Gemini history).

    The full session id is NOT in the file name (only the first 8 chars
    are), so we peek at the header line to recover it.  Falls back to
    skipping a file with a malformed header rather than crashing.
    """
    label = _gemini_project_label(project_dir)
    if label is None:
        # No record of this cwd in projects.json → no Gemini sessions for it.
        return
    chats_dir = _gemini_home() / "tmp" / label / "chats"
    if not chats_dir.is_dir():
        return
    try:
        jsonl_files = list(chats_dir.glob("session-*.jsonl"))
    except OSError:
        return
    for jsonl_path in jsonl_files:
        if not jsonl_path.is_file():
            continue
        session_id = _read_gemini_session_id(jsonl_path)
        if session_id:
            yield session_id, jsonl_path


def _read_gemini_session_id(jsonl_path: Path) -> str | None:
    """Read the header line of a Gemini JSONL and return its ``sessionId``."""
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    return None
                if isinstance(obj, dict):
                    sid = obj.get("sessionId")
                    if isinstance(sid, str) and sid:
                        return sid
                return None
    except OSError:
        return None
    return None


register_harness(HarnessSpec(
    name="gemini",
    label="Gemini CLI",
    description="Google's Gemini CLI — Node-based, OAuth via Google account or GEMINI_API_KEY.",
    session_class_loader=_load_gemini_session_class,
    adapter_loader=lambda: _adapter,
    # Same as Qwen — Node-based; the spec name is what the reaper uses
    # to dispatch, not the comm prefix alone.
    comm_prefix="node",
    kill_helper_loader=_load_gemini_kill_helper,
    ssh_control_path_prefix="gemini",
    jsonl_path_resolver=_gemini_jsonl_candidates,
    session_discoverer=_gemini_discover_sessions,
    requirements_file=None,  # external Node CLI; no Python deps
    npm_package="@google/gemini-cli",
    cli_binary="gemini",
    env_keys=(),  # OAuth-first; GEMINI_API_KEY is optional
))
