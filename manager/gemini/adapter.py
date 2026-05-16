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

Tool calls are written *inline* on the same JSONL line as the assistant
turn, under a top-level ``toolCalls`` array (NOT as separate lines like
Qwen/Claude do).  Each entry has the shape::

    {"id": "...", "name": "...", "args": {...},
     "result": [{"functionResponse": {"id": "...", "name": "...",
                                       "response": {"output": "..."}}}],
     "status": "success" | "error",
     "resultDisplay": "<markdown rendered output>",
     "timestamp": "...",
     ...}

The adapter splits one such line into TWO normalized messages: the
assistant turn (with a ``tool_use`` block per ``toolCalls`` entry) and
a synthetic user message carrying matched ``tool_result`` blocks.  This
mirrors the Anthropic shape the frontend pairs via ``tool_use_id``.
Without this, re-opened Gemini conversations show only the text replies
and the tool calls vanish from the UI.
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


def _extract_tool_result_text(call: dict) -> str:
    """Pull the human-visible output for one ``toolCalls`` entry.

    Prefers ``resultDisplay`` (the markdown the CLI renders to its own
    UI) and falls back to digging through ``result[].functionResponse.
    response.output`` or ``.error``.
    """
    display = call.get("resultDisplay")
    if isinstance(display, str) and display.strip():
        return display
    result = call.get("result")
    if isinstance(result, list):
        parts: list[str] = []
        for entry in result:
            if not isinstance(entry, dict):
                continue
            resp = entry.get("functionResponse")
            if not isinstance(resp, dict):
                continue
            inner = resp.get("response")
            if not isinstance(inner, dict):
                continue
            if "output" in inner and inner.get("output") is not None:
                parts.append(str(inner.get("output")))
            elif "error" in inner and inner.get("error") is not None:
                parts.append(str(inner.get("error")))
        if parts:
            return "\n".join(parts)
    return ""


def _normalize_message(obj: dict) -> list[dict]:
    """Translate a raw Gemini JSONL message line to one or more normalized
    messages.

    Returns an empty list if the line isn't a user/assistant message.
    Most lines produce a single message; a ``type: "gemini"`` line that
    also carries ``toolCalls`` produces TWO messages — the assistant
    turn (with ``tool_use`` blocks) followed by a synthetic user message
    carrying the matched ``tool_result`` blocks, so the frontend can
    pair them via ``tool_use_id``.

    Output shape matches the contract on
    :class:`~manager.protocol.ProviderAdapter`: each entry has ``type``
    ∈ {user, assistant}, ``timestamp``, and ``message.content`` as
    either a string (plain user text) or a list of content blocks.
    """
    raw_type = obj.get("type")
    role = _normalize_type(raw_type) if isinstance(raw_type, str) else None
    if role is None:
        return []

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
        return [{
            "type": "user",
            "timestamp": timestamp,
            "message": {"role": "user", "content": content},
        }]

    # Assistant message: build content blocks.  Start with thoughts
    # (each becomes a thinking block), then the main text body, then any
    # tool_use blocks from ``toolCalls``.
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

    tool_result_blocks: list[dict] = []
    raw_calls = obj.get("toolCalls")
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            tool_id = call.get("id")
            tool_name = call.get("name", "")
            args = call.get("args", {})
            if not isinstance(args, dict):
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": tool_id,
                "name": tool_name,
                "input": args,
            })
            # Synthesize a tool_result block — but only if the call
            # actually completed (status field present).  An in-flight
            # call would have no result yet; the live event stream
            # handles that path.
            status = call.get("status")
            if status is None and "result" not in call:
                continue
            output = _extract_tool_result_text(call)
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": output,
                "is_error": status == "error",
            })

    out: list[dict] = [{
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": blocks},
    }]
    if tool_result_blocks:
        out.append({
            "type": "user",
            "timestamp": timestamp,
            "message": {"role": "user", "content": tool_result_blocks},
        })
    return out


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
        each remaining user/gemini line to one or more normalized
        messages (an assistant turn that used tools fans out into the
        assistant message plus a synthetic tool-result user message).
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
                    messages.extend(_normalize_message(obj))
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
                            normalized_list = _normalize_message(obj)
                            if normalized_list:
                                first_user_text = extract_text(normalized_list[0])
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


def _gemini_chats_dir(project_dir: str) -> Path:
    """Where Gemini JSONLs live for *project_dir*.

    install.sh symlinks ``~/.gemini/tmp/<label>`` → ``<project_dir>/context``
    (see ``install/setup-gemini-storage.sh``), so the Gemini CLI writes its
    session files into the same ``context/chats/`` directory Qwen uses.
    Same path resolution as :meth:`SessionStore._resolve_chats_dir`.
    """
    return Path(project_dir) / "context" / "chats"


def _gemini_jsonl_candidates(session_id: str) -> list[Path]:
    """Return candidate JSONL paths for *session_id*.

    Gemini's file name is ``session-<short-iso>-<uuid-prefix>.jsonl`` where
    ``<uuid-prefix>`` is the first 8 chars of the session UUID, so the full
    id alone isn't enough to construct a deterministic path — we glob by
    prefix instead.  Scans both the live ``context/chats/`` (where the CLI
    writes now, via the symlink) and the legacy ``~/.gemini/tmp/<label>/chats/``
    layout so pre-migration sessions stay reachable on hosts where the
    symlink hasn't been set up yet.
    """
    short = session_id[:8]
    if not short:
        return []
    out: list[Path] = []
    # Live location: context/chats/.  Project dir comes from ManagerConfig,
    # which we don't have here — fall back to the default project dir, since
    # the JSONLs are project-scoped and there's only one ``context/`` per
    # install.
    try:
        from manager.config import ManagerConfig
        project_dir = ManagerConfig.load().project_dir
    except Exception:
        project_dir = str(Path(__file__).resolve().parent.parent.parent)
    chats_dir = _gemini_chats_dir(project_dir)
    if chats_dir.is_dir():
        try:
            out.extend(
                f for f in chats_dir.glob(f"session-*-{short}.jsonl") if f.is_file()
            )
        except OSError:
            pass
    # Legacy location: ~/.gemini/tmp/<label>/chats/.  Only relevant on hosts
    # that haven't run install/setup-gemini-storage.sh yet; once the symlink
    # is in place the chats_dir resolves to the same path via two routes.
    tmp_root = _gemini_home() / "tmp"
    if tmp_root.is_dir():
        try:
            for label_dir in tmp_root.iterdir():
                legacy_chats = label_dir / "chats"
                # Skip the symlinked label — we already scanned that path above.
                try:
                    if legacy_chats.resolve() == chats_dir.resolve():
                        continue
                except OSError:
                    pass
                if not legacy_chats.is_dir():
                    continue
                try:
                    out.extend(
                        f for f in legacy_chats.glob(f"session-*-{short}.jsonl")
                        if f.is_file()
                    )
                except OSError:
                    continue
        except OSError:
            pass
    return out


def _gemini_discover_sessions(project_dir: str):
    """Yield ``(session_id, jsonl_path)`` for Gemini sessions in *project_dir*.

    Scans the project's ``context/chats/`` directory for the
    ``session-<iso>-<uuid-prefix>.jsonl`` naming pattern the Gemini CLI
    uses (Qwen's JSONLs in the same directory are ``<full-uuid>.jsonl``
    and don't match the glob, so the two harnesses coexist cleanly).

    The full session id is NOT in the file name (only the first 8 chars
    are), so we peek at the header line to recover it.  Skips files with
    malformed headers rather than crashing.
    """
    chats_dir = _gemini_chats_dir(project_dir)
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
