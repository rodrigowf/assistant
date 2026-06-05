"""Persistent cache of the orchestrator's history-summary digest.

Why this exists:

When an orchestrator session has accumulated enough history that the
"older" prefix needs to be summarised to fit the voice-mode system
prompt, the summariser (``OrchestratorSession._summarize_history``)
makes a synchronous LLM call. On long-running sessions (hours of
voice + text turns) that call takes 15-25s, and it runs inside
``get_session_update`` which sits on the critical path between
``voice_start`` and ``session_started`` — i.e. between the user
saying the wake word and the mic going live. Tested 2026-06-04: the
gap was 22.6s after the phone had been idle long enough that
whatever in-memory caching the session had was cold.

Strategy:

- Compute the summary at conversation END (voice stop, history
  reopen, etc.) when the user is no longer waiting on it.
- Persist it to ``<session_jsonl>.summary.json`` so the next cold
  start finds it ready.
- ``get_session_update`` reads the cache first; if it's stale or
  missing it falls back to the synchronous path (no behaviour
  regression) AND writes the result back so the next call is fast.

Freshness key:

We use ``(jsonl_size, jsonl_mtime_ns)`` rather than hashing the
last message uuid because (a) it's a single stat() call (no read),
and (b) JSONL is append-only so size+mtime are reliable. If either
moved since the cache was written, the cache is stale.

We also include ``input_message_count`` and
``summary_target_words`` in the cache because the *input* to the
summariser is the "older" half after a token-budget split, and that
boundary can shift if any messages in the recent window get
truncated for size. The session-side splitter is deterministic
given the file content, so if size+mtime match, the split must
match too — but storing the inputs lets us sanity-check.

Schema (``<jsonl_stem>.summary.json`` next to the JSONL):

    {
        "schema_version": 1,
        "jsonl_size": 12345678,
        "jsonl_mtime_ns": 1717480000000000000,
        "input_message_count": 187,
        "summary_target_words": [200, 400],
        "summarizer_model": "claude-sonnet-4-6",
        "summary_text": "...",
        "generated_at": "2026-06-04T01:35:00Z"
    }

Not in cache (intentional):
- ``recent_verbatim_messages``: those come from re-reading the JSONL
  and are cheap (no LLM call). The expensive bit is just the
  summary of the OLDER prefix.
- A hash of the input transcript: redundant with size+mtime for
  append-only files, and computing it costs another full read.

Triggers (in order of how the cache stays warm):

1. On voice session stop — the cheapest moment, conversation just
   ended. ``OrchestratorSession.stop`` schedules a background
   refresh via ``refresh_summary_cache_if_stale``.
2. On chat WS start (= history reopen / app foreground reconnect) —
   ``_handle_start`` schedules a background refresh for non-voice
   sessions so subsequent wake-word voice_start finds the cache
   warm.
3. Read trigger fallback — ``_build_history_for_prompt`` itself
   reads the cache, falls back to synchronous compute on miss, and
   writes back so the next call is fast.

Not implemented (and deliberately so):

- Boot-time scan / warmup. The above three triggers cover every
  realistic path. The only window the boot warmup would add is
  "backend restarted between a chat WS start refresh kicking off
  and the user firing wake-word", and the synchronous read-trigger
  fallback handles that case correctly (slowly, but correctly).
  Revisit if we see that window matter in practice.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump if the cache schema changes in a way that older files can't be
# safely re-used (e.g. different summariser prompt → different
# semantics). Reading a stale-schema file is treated as a cache miss.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CachedSummary:
    """In-memory view of a parsed cache file."""

    summary_text: str
    input_message_count: int
    summary_target_words: tuple[int, int] | None
    summarizer_model: str | None
    generated_at: str


def cache_path_for(jsonl_path: Path) -> Path:
    """Where the cache file lives for a given JSONL.

    Sibling file with the same stem + ``.summary.json``. For
    ``/context/abc-1234.jsonl`` → ``/context/abc-1234.summary.json``.
    Sibling so it ships through the same ``context-sync`` pipeline as
    the JSONL itself and so per-file conflicts stay local.
    """
    return jsonl_path.with_suffix(".summary.json")


def _stat_key(jsonl_path: Path) -> tuple[int, int] | None:
    """Return ``(size, mtime_ns)`` for the JSONL, or None if it doesn't
    exist. Single stat call, no file read."""
    try:
        st = os.stat(jsonl_path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def read(jsonl_path: Path) -> CachedSummary | None:
    """Read a fresh cached summary for ``jsonl_path``, or None if the
    cache is missing, malformed, schema-stale, or stale relative to
    the JSONL.

    Never raises — any failure path returns None and the caller
    falls back to recomputing.
    """
    return _read(jsonl_path, require_fresh=True)


def read_any(jsonl_path: Path) -> CachedSummary | None:
    """Read the cached summary regardless of freshness vs JSONL.

    Returns the cached summary even if the JSONL has grown since the
    cache was written. Caller is responsible for checking whether the
    cache's ``input_message_count`` is still semantically valid for
    the current older-prefix slice (this matters when an append-only
    JSONL grew with new turns that stayed inside the recent-verbatim
    window — the older prefix is byte-identical, so the same summary
    still describes it).
    """
    return _read(jsonl_path, require_fresh=False)


def _read(jsonl_path: Path, *, require_fresh: bool) -> CachedSummary | None:
    cp = cache_path_for(jsonl_path)
    if not cp.is_file():
        return None

    key = _stat_key(jsonl_path)
    if key is None:
        return None

    try:
        with open(cp, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.info("summary_cache read failed for %s: %s", cp.name, e)
        return None

    if raw.get("schema_version") != SCHEMA_VERSION:
        return None

    if require_fresh:
        cached_size = raw.get("jsonl_size")
        cached_mtime = raw.get("jsonl_mtime_ns")
        if cached_size != key[0] or cached_mtime != key[1]:
            # JSONL grew (new turn appended) or was modified — stale.
            return None

    summary_text = raw.get("summary_text")
    if not isinstance(summary_text, str):
        return None

    tw = raw.get("summary_target_words")
    target_words: tuple[int, int] | None = None
    if isinstance(tw, list) and len(tw) == 2:
        try:
            target_words = (int(tw[0]), int(tw[1]))
        except (TypeError, ValueError):
            target_words = None

    return CachedSummary(
        summary_text=summary_text,
        input_message_count=int(raw.get("input_message_count", 0)),
        summary_target_words=target_words,
        summarizer_model=raw.get("summarizer_model"),
        generated_at=str(raw.get("generated_at", "")),
    )


def write(
    jsonl_path: Path,
    *,
    summary_text: str,
    input_message_count: int,
    summary_target_words: tuple[int, int] | None,
    summarizer_model: str | None,
) -> None:
    """Atomically write the summary cache next to ``jsonl_path``.

    Uses a temp file + rename so a crash mid-write can't leave a
    truncated cache file that future reads then trip over. The
    freshness key is captured *after* writing the temp file but
    before the rename, which means a crash during the LLM call (or
    the JSONL changing under us) results in a no-op rather than a
    stale cache.

    Never raises — failure is logged and the next read just sees a
    cache miss.
    """
    cp = cache_path_for(jsonl_path)
    key = _stat_key(jsonl_path)
    if key is None:
        logger.warning(
            "summary_cache write skipped: JSONL %s vanished", jsonl_path
        )
        return

    payload = {
        "schema_version": SCHEMA_VERSION,
        "jsonl_size": key[0],
        "jsonl_mtime_ns": key[1],
        "input_message_count": input_message_count,
        "summary_target_words": list(summary_target_words) if summary_target_words else None,
        "summarizer_model": summarizer_model,
        "summary_text": summary_text,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory so the os.replace
        # is atomic (same filesystem).
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cp.parent,
            prefix=cp.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, cp)
        logger.info(
            "summary_cache wrote %s (%d input messages, %d summary chars)",
            cp.name, input_message_count, len(summary_text),
        )
    except OSError as e:
        logger.warning("summary_cache write failed for %s: %s", cp.name, e)


def is_fresh(jsonl_path: Path) -> bool:
    """Quick freshness check without reading the summary text.

    Useful for "do we need to schedule a background refresh?" decisions
    where we don't need the summary itself.
    """
    cp = cache_path_for(jsonl_path)
    if not cp.is_file():
        return False
    key = _stat_key(jsonl_path)
    if key is None:
        return False
    try:
        with open(cp, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if raw.get("schema_version") != SCHEMA_VERSION:
        return False
    return raw.get("jsonl_size") == key[0] and raw.get("jsonl_mtime_ns") == key[1]
