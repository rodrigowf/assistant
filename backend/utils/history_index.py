"""Conversation-history index: hybrid (keyword + semantic) search over past sessions.

Replaces the old chroma ``history`` collection, which (a) aborted the whole indexing run
whenever one large session timed out in ``encode_many`` (so every session sorting after it
was never indexed), (b) got memory chunks mixed in through the WAL-replay repair tier, and
(c) chunked by 10 raw markdown lines, so chunks were cut mid-message and truncated by the
embedding model.

Storage is one SQLite file (``index/history.sqlite3``, WAL mode):

- ``sessions``  one row per source JSONL (harness, dates, source size/mtime for change detection)
- ``chunks``    one row per window of one message: role, turn index, timestamp, text, and the
                float32 embedding (unit-normalised) as a BLOB
- ``chunks_fts`` FTS5 index over ``chunks.text`` (porter stemming, diacritics folded) for
                keyword/BM25 matching of names and exact terms, which embeddings miss
- ``meta``      schema version, model name, and a ``generation`` counter bumped on every write
                so readers know when to reload the embedding matrix

Search fuses BM25 and cosine rankings with reciprocal-rank fusion, then groups hits by session.
The vector side is brute force in numpy: ~20k x 384 floats is ~30 MB and a matrix-vector
product is milliseconds even on the Jetson, with no HNSW segment to corrupt.

The file is self-contained (no chroma versions involved), so it can be built on the laptop and
copied to the Jetson: both machines use the same install path and the same JSONL file names.

Everything here is import-light (stdlib + numpy); the embedding model lives in the caller
(the warm search-server, or a local SentenceTransformer in scripts).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from utils.paths import get_chats_dir, get_context_dir, get_index_dir

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Bump when extraction/chunking changes so every session is re-derived on the next run.
CHUNKER_VERSION = 4
# Default model for new index files. Multilingual: Rodrigo speaks English and Portuguese, and
# the English-only all-MiniLM-L6-v2 clustered Portuguese text by language instead of topic
# (dev eval: Portuguese hit@5 0.46 → 0.89). Every index records its own model in meta.model.
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
LEGACY_MODEL = "all-MiniLM-L6-v2"
EMBED_DIM = 384
# Models that were trained with role prefixes (query vs passage) need them at both ends.
MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
}


def query_prefix(model: str) -> str:
    return MODEL_PREFIXES.get(model, ("", ""))[0]


def passage_prefix(model: str) -> str:
    return MODEL_PREFIXES.get(model, ("", ""))[1]


def index_model(conn: sqlite3.Connection) -> str:
    """The embedding model an index file was built with (meta.model)."""
    row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    return row[0] if row else LEGACY_MODEL  # files from before meta.model existed

# Chunking: all-MiniLM-L6-v2 truncates at 256 word-pieces and was trained on 128, so windows of
# ~110 words (~150 word-pieces) keep the whole chunk inside what the model actually sees.
CHUNK_WORDS = 110
CHUNK_OVERLAP = 25
MIN_TURN_WORDS = 3          # "Yeah." / "Ok" turns carry nothing searchable
MAX_CHUNKS_PER_TURN = 40    # a pasted 20k-word log should not dominate the index
ENCODE_BATCH = 32           # ~12 s per batch on the Jetson; far below any socket timeout

# Orchestrator tools whose results the assistant then narrates ("I found ..."). That narration is
# derived from other sessions, so indexing it makes every later search hit the search itself.
SEARCH_TOOL_NAMES = frozenset({
    "search_history", "search_memory", "read_conversation", "list_conversations", "grep_conversation",
})
# A Claude Code session whose tool calls mostly read conversation history or the search index
# (digests, "find that conversation" agents, search debugging) quotes other sessions' topics all
# over. Such sessions are demoted in results, not dropped: they are still findable as themselves.
_HISTORY_TOOL_RE = re.compile(
    r"\.jsonl|search\.py|search_history|search_memory|history_index|index-memory|\.titles\.json|/recall|chroma|search-server"
)
ABOUT_HISTORY_MIN_CALLS = 5
ABOUT_HISTORY_MIN_SHARE = 0.35
ABOUT_HISTORY_DEMOTION = 0.35

Encoder = Callable[[list[str]], list[list[float]]]


def get_history_db_path() -> Path:
    return get_index_dir() / "history.sqlite3"


# ── Extraction ────────────────────────────────────────────────────────────────


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str
    ts: str | None = None


@dataclass
class SessionDoc:
    session_id: str
    path: Path
    harness: str  # "claude" | "orchestrator" | "qwen" | "gemini"
    turns: list[Turn] = field(default_factory=list)
    tool_calls: int = 0
    history_tool_calls: int = 0
    cwd: str | None = None

    @property
    def first_prompt(self) -> str | None:
        """The first thing the user said (a fallback title for untitled sessions)."""
        first = next((t.text for t in self.turns if t.role == "user"), None)
        if not first:
            return None
        first = " ".join(first.split())
        return first if len(first) <= 160 else first[:160] + "…"

    @property
    def about_history(self) -> bool:
        return (
            self.history_tool_calls >= ABOUT_HISTORY_MIN_CALLS
            and self.history_tool_calls >= ABOUT_HISTORY_MIN_SHARE * self.tool_calls
        )

    @property
    def started_at(self) -> str | None:
        return next((t.ts for t in self.turns if t.ts), None)

    @property
    def ended_at(self) -> str | None:
        return next((t.ts for t in reversed(self.turns) if t.ts), None)


_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# Base64 blobs, minified bundles, long hashes: no searchable meaning, and they wreck tokenisation.
_BLOB_RE = re.compile(r"\S{200,}")
# User-side entries the Claude Code CLI writes that are not things the user said.
_CLAUDE_NOISE_PREFIXES = (
    "<task-notification",
    "<local-command",
    "<command-name",
    "<command-message",
    "<bash-input",
    "<bash-stdout",
    "[Request interrupted",
    "This session is being continued from a previous conversation",
    "Caveat: The messages below",
)
_NON_SPEECH_RE = re.compile(r"^\[audio:[a-z0-9]+\]\s*\(audio message\)\s*$", re.IGNORECASE)
_VOICE_PREFIX_RE = re.compile(r"^\[voice\]\s*")


def _clean(text: str) -> str:
    text = _SYSTEM_REMINDER_RE.sub(" ", text)
    text = _BLOB_RE.sub(" ", text)
    return text.strip()


def _blocks_text(content, *, text_key: str = "text", skip_thoughts: bool = False) -> str:
    """Join the text blocks of a message content (str, or a list of blocks/parts)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if skip_thoughts and block.get("thought"):
            continue
        btype = block.get("type")
        if btype not in (None, "text"):
            continue  # tool_use / tool_result / image / thinking
        val = block.get(text_key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    return "\n".join(parts)


def _detect_harness(first: dict) -> str:
    if first.get("type") == "orchestrator_meta" or first.get("orchestrator") is True:
        return "orchestrator"
    if "projectHash" in first and "kind" in first:
        return "gemini"
    msg = first.get("message")
    if isinstance(msg, dict) and "parts" in msg:
        return "qwen"
    return "claude"


def _iter_json_lines(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def session_id_for(path: Path) -> str:
    """The session id a conversation file belongs to.

    The file name for every harness except Codex, whose rollouts are named
    ``rollout-<timestamp>-<thread-id>.jsonl``, and Gemini, whose
    ``session-<iso-minute>-<id[:8]>.jsonl`` names only carry an id prefix: its
    header line holds the real ``sessionId`` — the id SessionStore, the titles
    and ``resume_conversation`` use.
    """
    path = Path(path)
    if path.name.startswith("rollout-"):
        from manager.codex.adapter import session_id_from_path

        return session_id_from_path(path) or path.stem
    if path.name.startswith("session-"):
        return _gemini_header_id(path) or path.stem
    return path.stem


def _gemini_header_id(path: Path) -> str | None:
    """``sessionId`` from a Gemini JSONL header (``{sessionId, projectHash, kind, …}``)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                if isinstance(obj, dict) and "projectHash" in obj:
                    sid = obj.get("sessionId")
                    return sid if isinstance(sid, str) and sid else None
                return None
    except (OSError, ValueError):
        return None
    return None


def _extract_codex(path: Path) -> SessionDoc:
    """Codex rollouts: reuse the harness adapter, which already hides the
    injected instructions/environment messages and normalizes tool calls."""
    from manager.codex.adapter import CodexAdapter

    doc = SessionDoc(session_id=session_id_for(path), path=path, harness="codex")
    for msg in CodexAdapter().read_messages(path):
        role = msg.get("type")
        if role not in ("user", "assistant"):
            continue
        content = (msg.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    doc.tool_calls += 1
                    if _HISTORY_TOOL_RE.search(json.dumps(block.get("input"), ensure_ascii=False)):
                        doc.history_tool_calls += 1
        text = _clean(_blocks_text(content))
        if not text:
            continue
        ts = msg.get("timestamp") if isinstance(msg.get("timestamp"), str) else None
        last = doc.turns[-1] if doc.turns else None
        if last is not None and last.role == role:
            if text != last.text and not last.text.endswith(text):
                last.text = f"{last.text}\n\n{text}"
            continue
        doc.turns.append(Turn(role=role, text=text, ts=ts))
    return doc


def extract_session(path: Path) -> SessionDoc:
    """Parse a session JSONL from any harness into clean user/assistant turns.

    Consecutive same-role messages are merged into one turn (Claude Code writes one line per
    content block, and voice sessions sometimes persist the same reply twice).
    """
    path = Path(path)
    if path.name.startswith("rollout-"):
        return _extract_codex(path)
    doc = SessionDoc(session_id=session_id_for(path), path=path, harness="claude")
    harness: str | None = None
    skip_assistant_until_user = False

    def add(role: str, text: str, ts) -> None:
        text = _clean(text)
        if not text:
            return
        ts = ts if isinstance(ts, str) and ts else None
        last = doc.turns[-1] if doc.turns else None
        if last is not None and last.role == role:
            if text == last.text or last.text.endswith(text):
                return  # duplicate persistence
            last.text = f"{last.text}\n\n{text}"
            return
        doc.turns.append(Turn(role=role, text=text, ts=ts))

    for obj in _iter_json_lines(path):
        if harness is None:
            harness = _detect_harness(obj)
            doc.harness = harness
        typ = obj.get("type")
        ts = obj.get("timestamp")
        if doc.cwd is None and isinstance(obj.get("cwd"), str):
            doc.cwd = obj["cwd"]

        if harness == "orchestrator":
            if typ == "tool_use" and obj.get("tool_name") in SEARCH_TOOL_NAMES:
                skip_assistant_until_user = True
                continue
            if typ not in ("user", "assistant"):
                continue
            msg = obj.get("message") or {}
            text = _blocks_text(msg.get("content"))
            if typ == "user":
                text = _VOICE_PREFIX_RE.sub("", text.strip())
                if _NON_SPEECH_RE.match(text):
                    continue
                skip_assistant_until_user = False
                add("user", text, ts)
            elif not skip_assistant_until_user:
                add("assistant", text, ts)

        elif harness == "claude":
            if typ not in ("user", "assistant"):
                continue
            if obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("isSidechain"):
                continue
            msg = obj.get("message") or {}
            if typ == "assistant" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        doc.tool_calls += 1
                        if _HISTORY_TOOL_RE.search(json.dumps(block.get("input"), ensure_ascii=False)):
                            doc.history_tool_calls += 1
            text = _blocks_text(msg.get("content"))
            stripped = _clean(text)
            if typ == "user" and stripped.startswith(_CLAUDE_NOISE_PREFIXES):
                continue
            add(typ, text, ts)

        elif harness == "qwen":
            if typ not in ("user", "assistant"):
                continue  # "system" telemetry, "tool_result"
            msg = obj.get("message") or {}
            text = _blocks_text(msg.get("parts"), skip_thoughts=True)
            add("user" if typ == "user" else "assistant", text, ts)

        elif harness == "gemini":
            if typ == "user":
                add("user", _blocks_text(obj.get("content")), ts)
            elif typ == "gemini":
                add("assistant", _blocks_text(obj.get("content")), ts)

    return doc


# ── Chunking ──────────────────────────────────────────────────────────────────


@dataclass
class Chunk:
    turn: int
    part: int
    role: str
    ts: str | None
    text: str
    context: str = ""  # embedded in front of the text (not keyword-indexed); see CONTEXT_MODE

    @property
    def embed_text(self) -> str:
        return f"{self.context}\n{self.text}" if self.context else self.text

    @property
    def text_hash(self) -> str:
        return hashlib.sha1(self.embed_text.encode("utf-8")).hexdigest()


# Contextual chunk headers (embedding side only): "none", "title" (session title · date ·
# speaker) or "title+prev" (also the gist of the turn being answered/continued).
CONTEXT_MODE = "none"
_PREV_WORDS = 20


def add_context(doc: SessionDoc, chunks: list["Chunk"], title: str | None, mode: str | None = None) -> None:
    mode = mode or CONTEXT_MODE
    if mode == "none":
        return
    for c in chunks:
        if c.turn < 0:
            continue
        head = " · ".join(x for x in (title or "", (c.ts or "")[:10], "Rodrigo" if c.role == "user" else "Archie") if x)
        if mode == "title+prev" and c.turn > 0:
            prev = doc.turns[c.turn - 1].text.split()
            gist = " ".join(prev[:_PREV_WORDS]) + (" …" if len(prev) > _PREV_WORDS else "")
            head = f"{head} — replying to: {gist}"
        c.context = f"[{head}]"


_WORD_RE = re.compile(r"\S+")


def split_words(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Overlapping word windows that keep the original formatting inside each window."""
    spans = [m.span() for m in _WORD_RE.finditer(text)]
    if not spans:
        return []
    if len(spans) <= size:
        return [text.strip()]
    out: list[str] = []
    step = size - overlap
    i = 0
    while i < len(spans):
        window = spans[i : i + size]
        out.append(text[window[0][0] : window[-1][1]])
        if i + size >= len(spans):
            break
        i += step
    return out


def chunk_session(doc: SessionDoc) -> list[Chunk]:
    chunks: list[Chunk] = []
    for turn_idx, turn in enumerate(doc.turns):
        if len(_WORD_RE.findall(turn.text)) < MIN_TURN_WORDS:
            continue
        for part, piece in enumerate(split_words(turn.text)[:MAX_CHUNKS_PER_TURN]):
            chunks.append(Chunk(turn=turn_idx, part=part, role=turn.role, ts=turn.ts, text=piece))
    return chunks


# ── Store ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    harness TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    chunker_version INTEGER NOT NULL,
    n_turns INTEGER NOT NULL,
    n_chunks INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    about_history INTEGER NOT NULL DEFAULT 0,
    cwd TEXT,
    first_prompt TEXT,
    auto_title TEXT,
    summary TEXT,
    summary_hash TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    part INTEGER NOT NULL,
    role TEXT NOT NULL,
    ts TEXT,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_session ON chunks(session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def connect(db_path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path or get_history_db_path())
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        # Files built by older versions: add the columns they lack (CHUNKER_VERSION bumps refill them).
        for col, decl in (("about_history", "INTEGER NOT NULL DEFAULT 0"), ("cwd", "TEXT"), ("first_prompt", "TEXT"),
                          ("auto_title", "TEXT"), ("summary", "TEXT"), ("summary_hash", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {decl}")
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?), ('model', ?), ('generation', '0')",
            (str(SCHEMA_VERSION), MODEL_NAME),
        )
        conn.commit()
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
    return int(row[0]) if row else 0


def _bump_generation(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='generation'")


def _to_blob(vec: Sequence[float]) -> bytes:
    import numpy as np

    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = arr / norm
    return arr.astype(np.float32).tobytes()


def delete_session(conn: sqlite3.Connection, session_id: str) -> int:
    """Drop a session's chunks and row. Returns the number of chunks removed."""
    with conn:
        n = conn.execute("DELETE FROM chunks WHERE session_id=?", (session_id,)).rowcount
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        if n:
            _bump_generation(conn)
    return n


def index_session(conn: sqlite3.Connection, path: Path, encode: Encoder, summary: dict | None = None) -> int:
    """(Re)index one session file atomically. Unchanged chunk texts reuse their stored embedding,
    so an appended conversation only embeds its new messages. Returns chunks newly embedded."""
    path = Path(path)
    st = path.stat()
    doc = extract_session(path)
    chunks = chunk_session(doc)
    if summary:
        from utils.session_summaries import summary_text
        # The session summary is one more searchable chunk (turn -1): it scores the session but
        # is never shown as an excerpt.
        chunks.append(Chunk(turn=-1, part=0, role="summary", ts=doc.started_at, text=summary_text(summary)))
    add_context(doc, chunks, _load_titles().get(doc.session_id) or (summary or {}).get("title"))

    existing: dict[str, bytes] = {
        h: emb
        for h, emb in conn.execute(
            "SELECT text_hash, embedding FROM chunks WHERE session_id=?", (doc.session_id,)
        )
    }
    missing = [c for c in chunks if c.text_hash not in existing]
    # Dedupe identical texts inside the batch so they're embedded once.
    to_embed: dict[str, str] = {}
    for c in missing:
        to_embed.setdefault(c.text_hash, c.embed_text)
    hashes = list(to_embed)
    for i in range(0, len(hashes), ENCODE_BATCH):
        batch = hashes[i : i + ENCODE_BATCH]
        vecs = encode([passage_prefix(index_model(conn)) + to_embed[h] for h in batch])
        if len(vecs) != len(batch):
            raise RuntimeError(f"encoder returned {len(vecs)} vectors for {len(batch)} texts")
        for h, v in zip(batch, vecs):
            existing[h] = _to_blob(v)

    now = datetime.now(timezone.utc).isoformat()
    with conn:  # one transaction: readers see the old or the new session, never half of it
        conn.execute("DELETE FROM chunks WHERE session_id=?", (doc.session_id,))
        conn.executemany(
            "INSERT INTO chunks(session_id, turn, part, role, ts, text, text_hash, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (doc.session_id, c.turn, c.part, c.role, c.ts, c.text, c.text_hash, existing[c.text_hash])
                for c in chunks
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO sessions(id, path, harness, started_at, ended_at, source_size, "
            "source_mtime_ns, chunker_version, n_turns, n_chunks, indexed_at, about_history, cwd, first_prompt, "
            "auto_title, summary, summary_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc.session_id, str(path), doc.harness, doc.started_at, doc.ended_at,
                st.st_size, st.st_mtime_ns, CHUNKER_VERSION, len(doc.turns), len(chunks), now,
                int(doc.about_history), doc.cwd, doc.first_prompt,
                (summary or {}).get("title"), (summary or {}).get("summary"), _summary_hash(summary),
            ),
        )
        _bump_generation(conn)
    return len(hashes)


def _summary_hash(summary: dict | None) -> str:
    if not summary:
        return ""
    from utils.session_summaries import summary_hash
    return summary_hash(summary)


def session_sources(context_dir: Path | None = None, chats_dir: Path | None = None) -> list[Path]:
    """Every conversation JSONL from every harness, newest first (so a long first build makes
    recent conversations searchable before old ones)."""
    context_dir = Path(context_dir or get_context_dir())
    chats_dir = Path(chats_dir or get_chats_dir())
    paths = list(context_dir.glob("*.jsonl"))
    if chats_dir.is_dir():
        paths.extend(chats_dir.glob("*.jsonl"))
    paths.extend(_codex_sources(context_dir.parent))

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(paths, key=mtime, reverse=True)


def _codex_sources(project_dir: Path | None = None) -> list[Path]:
    """Archie's Codex rollouts (``context/codex/sessions/YYYY/MM/DD/`` and the
    harness's other session roots), via the harness's own discoverer.

    *project_dir* is the repo whose ``context/`` is being indexed (default:
    this install's); the CODEX_HOME roots are only searched for this install."""
    try:
        from manager.codex.adapter import _codex_discover_sessions

        root = Path(project_dir) if project_dir else get_context_dir().parent
        return [p for _sid, p in _codex_discover_sessions(str(root))]
    except Exception:  # noqa: BLE001 — a missing/broken harness must not stop indexing
        logger.debug("Codex session discovery failed", exc_info=True)
        return []


@dataclass
class IndexStats:
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    embedded_chunks: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped_early: bool = False


def index_all(
    conn: sqlite3.Connection,
    sources: Iterable[Path],
    encode: Encoder,
    *,
    time_budget_s: float | None = None,
    log: Callable[[str], None] = print,
    summaries: dict[str, dict] | None = None,
) -> IndexStats:
    """Bring the index in line with ``sources``. A failure on one session is logged and skipped;
    it never stops the others. Sessions whose file disappeared are removed. ``summaries``
    (session id → summary) adds each session's summary as a searchable chunk; a new or changed
    summary re-indexes its session."""
    stats = IndexStats()
    sources = list(sources)
    summaries = summaries or {}
    known = {
        sid: (size, mtime_ns, ver, sh or "")
        for sid, size, mtime_ns, ver, sh in conn.execute(
            "SELECT id, source_size, source_mtime_ns, chunker_version, summary_hash FROM sessions"
        )
    }
    started = time.monotonic()
    seen: set[str] = set()
    for path in sources:
        sid = session_id_for(path)
        seen.add(sid)
        try:
            st = path.stat()
        except OSError:
            continue
        summary = summaries.get(sid)
        if known.get(sid) == (st.st_size, st.st_mtime_ns, CHUNKER_VERSION, _summary_hash(summary)):
            stats.unchanged += 1
            continue
        if time_budget_s is not None and time.monotonic() - started > time_budget_s:
            stats.stopped_early = True
            break
        try:
            n = index_session(conn, path, encode, summary)
            stats.indexed += 1
            stats.embedded_chunks += n
            log(f"  indexed {path.name} ({n} new chunks embedded)")
        except Exception as e:  # noqa: BLE001 — isolate per-session failures
            stats.failed.append((path.name, f"{type(e).__name__}: {e}"))
            log(f"  FAILED {path.name}: {type(e).__name__}: {e}")

    if not stats.stopped_early:
        for sid in set(known) - seen:
            delete_session(conn, sid)
            stats.removed += 1
    return stats


class IndexLock:
    """Non-blocking exclusive lock so two indexer runs never write concurrently."""

    def __init__(self, db_path: Path | None = None):
        self._path = Path(db_path or get_history_db_path()).with_suffix(".lock")
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# ── Search ────────────────────────────────────────────────────────────────────

# Words that carry no topic in English or Portuguese queries. FTS ORs the rest together.
_STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if in
    into is it its me my no not of on or our so that the their them then there these they this
    to was we were what when where which who why will with would you your about any some talk
    talked talking conversation conversations discuss discussed remember previous past
    o os as um uma uns umas e de do da dos das em no na nos nas por para com que se não nao
    eu você voce ele ela nós nos eles elas meu minha seu sua isso isto aquilo foi era ser estar
    sobre conversa conversamos falamos lembra""".split()
)
_QUERY_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

RRF_K = 60
CANDIDATES = 200
MIN_VECTOR_SIM = 0.30       # a chunk below this similarity never counts as a semantic match
SEMANTIC_ONLY_SIM = 0.42    # ... and without any keyword match it needs at least this
# What counts as "strong" evidence, per embedding model (each model has its own similarity
# scale): (similarity strong on its own, keyword share strong on its own, (share, similarity)
# strong together). Calibrated on the dev eval, 2026-10-06: never-discussed topics stay below,
# ~90% of correct sessions found are above.
STRENGTH = {
    LEGACY_MODEL: (0.66, 0.60, (0.40, 0.55)),
    "paraphrase-multilingual-MiniLM-L12-v2": (0.62, 0.60, (0.40, 0.50)),
}
SNIPPET_CHARS = 700
DUPLICATE_OVERLAP = 0.5     # sessions sharing this share of their top hits are copies
SESSION_W_NEXT = 0.0        # session score = best chunk + W_NEXT·(next two) + W_TAIL·(next seven);
                            # best-chunk-only won on dev (hit@1 .50→.57): sums favoured long, rambling sessions
SESSION_W_TAIL = 0.0
KIND_BOOST = 1.5            # kind_mode="prefer": sessions of the requested kind rank higher
WINDOW_BOOST = 1.6          # window_mode="boost": score factor for chunks inside the remembered time


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for tok in _QUERY_TOKEN_RE.findall(query.lower()):
        if len(tok) < 2 or tok in _STOPWORDS or tok in terms:
            continue
        terms.append(tok)
    return terms


def fts_query(query: str) -> str | None:
    terms = query_terms(query)
    return " OR ".join(f'"{t}"' for t in terms) if terms else None


def min_terms_required(n_terms: int) -> int:
    """How many distinct query terms a keyword-only chunk must contain. One shared common word
    ("mode", "test") between a 4-word query and a chunk is not a match."""
    if n_terms <= 2:
        return 1
    return max(2, -(-n_terms // 3))  # ceil(n/3), at least 2


def hybrid_rank(
    conn: sqlite3.Connection,
    query: str,
    query_vec,
    allowed,
    *,
    ids,
    groups,
    ts,
    matrix,
    chunk_table: str = "chunks",
    fts_table: str = "chunks_fts",
    group_col: str = "session_id",
) -> dict:
    """One phrasing → per-chunk RRF score plus the evidence behind it.

    Keyword side: chunks containing enough distinct query terms (min_terms_required), ordered
    by terms matched then BM25. Semantic side: cosine similarity against ``matrix`` (rows
    aligned with ``ids``/``groups``/``ts``). ``allowed(group, ts)`` filters both sides. Shared by
    the history and memory indexes, which store chunks the same way.
    """
    import numpy as np

    vec_sim: dict[int, float] = {}
    vec_rank: dict[int, int] = {}
    if query_vec is not None and len(ids):
        q = np.asarray(query_vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        sims = matrix @ q
        for idx in np.argsort(-sims):
            if len(vec_rank) >= CANDIDATES:
                break
            sim = float(sims[idx])
            if sim < MIN_VECTOR_SIM:
                break
            if not allowed(groups[idx], ts[idx]):
                continue
            cid = int(ids[idx])
            vec_sim[cid] = sim
            vec_rank[cid] = len(vec_rank)

    terms = query_terms(query)
    coverage: dict[int, int] = {}
    kw_rank: dict[int, int] = {}
    if terms:
        for term in terms:
            for (rowid,) in conn.execute(f"SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?", (f'"{term}"',)):
                coverage[rowid] = coverage.get(rowid, 0) + 1
        need = min_terms_required(len(terms))
        bm25 = dict(conn.execute(
            f"SELECT rowid, bm25({fts_table}) FROM {fts_table} WHERE {fts_table} MATCH ?",
            (" OR ".join(f'"{t}"' for t in terms),),
        ))
        candidates = [cid for cid, k in coverage.items() if k >= need or cid in vec_sim]
        candidates.sort(key=lambda c: (-coverage[c], bm25.get(c, 0.0)))
        meta: dict[int, tuple[str, str]] = {}
        for i in range(0, len(candidates), 900):
            part = candidates[i : i + 900]
            meta.update({
                r[0]: (r[1], r[2] or "")
                for r in conn.execute(
                    f"SELECT id, {group_col}, ts FROM {chunk_table} WHERE id IN ({','.join('?' * len(part))})", part
                )
            })
        for cid in candidates:
            if len(kw_rank) >= CANDIDATES:
                break
            if cid in meta and allowed(*meta[cid]):
                kw_rank[cid] = len(kw_rank)

    scores: dict[int, float] = {}
    share: dict[int, float] = {}
    for cid in set(vec_rank) | set(kw_rank):
        in_kw, in_vec = cid in kw_rank, cid in vec_rank
        if in_vec and not in_kw and vec_sim[cid] < SEMANTIC_ONLY_SIM:
            continue
        score = 1.0 / (RRF_K + vec_rank[cid]) if in_vec else 0.0
        if in_kw:
            share[cid] = coverage.get(cid, 0) / len(terms)
            score += (0.5 + share[cid]) / (RRF_K + kw_rank[cid])
        scores[cid] = score
    return {"scores": scores, "vec_sim": vec_sim, "share": share, "kw": set(kw_rank), "n_terms": len(terms)}


def is_strong(sim: float, share: float, model: str = MODEL_NAME) -> bool:
    """Evidence a chunk really is about the query (see STRENGTH)."""
    sim_alone, share_alone, (mix_share, mix_sim) = STRENGTH.get(model, STRENGTH[MODEL_NAME])
    return sim >= sim_alone or share >= share_alone or (share >= mix_share and sim >= mix_sim)


def _kind_matches(kind: str, harness: str | None) -> bool:
    if kind == "agent":
        return harness != "orchestrator"
    return harness == kind


def _load_titles(context_dir: Path | None = None) -> dict[str, str]:
    path = Path(context_dir or get_context_dir()) / ".titles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class HistorySearcher:
    """Read side. Keeps the embedding matrix in memory and reloads it when the indexer bumps
    the ``generation`` counter. One instance per long-lived process (the warm search-server)."""

    def __init__(self, db_path: Path | None = None, context_dir: Path | None = None):
        self._db_path = Path(db_path or get_history_db_path())
        self._context_dir = context_dir
        self._generation: int | None = None
        self._ids = None
        self._sessions = None
        self._ts = None
        self._matrix = None

    def _conn(self) -> sqlite3.Connection:
        if not self._db_path.exists():
            raise FileNotFoundError(f"history index not built yet: {self._db_path}")
        return connect(self._db_path, readonly=True)

    def _ensure_loaded(self, conn: sqlite3.Connection) -> None:
        import numpy as np

        gen = get_generation(conn)
        if gen == self._generation and self._matrix is not None:
            return
        rows = conn.execute("SELECT id, session_id, ts, embedding FROM chunks ORDER BY id").fetchall()
        self._ids = np.array([r[0] for r in rows], dtype=np.int64)
        self._sessions = np.array([r[1] for r in rows], dtype=object)
        self._ts = np.array([r[2] or "" for r in rows], dtype=object)
        if rows:
            self._matrix = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        else:
            self._matrix = np.zeros((0, EMBED_DIM), dtype=np.float32)
        self._harness = dict(conn.execute("SELECT id, harness FROM sessions"))
        self._model = index_model(conn)
        self._generation = gen

    def _rank_one(self, conn, query: str, query_vec, allowed) -> dict:
        return hybrid_rank(
            conn, query, query_vec, allowed,
            ids=self._ids, groups=self._sessions, ts=self._ts, matrix=self._matrix,
        )

    def search(
        self,
        query: str,
        query_vec: Sequence[float] | None,
        *,
        extra_queries: Sequence[tuple[str, Sequence[float] | None]] = (),
        max_sessions: int = 5,
        hits_per_session: int = 3,
        exclude_sessions: Iterable[str] = (),
        session_id: str | None = None,
        after: str | None = None,
        before: str | None = None,
        kind: str | None = None,
        window: tuple[str, str] | None = None,
        window_mode: str = "filter",
        kind_mode: str = "filter",
        debug: bool = False,
    ) -> dict:
        """Rank past conversations for one or more phrasings of a request.

        Each phrasing is ranked on its own — keyword side: chunks with enough distinct query
        terms (min_terms_required), by terms matched then BM25; semantic side: cosine
        similarity — the two fused with reciprocal-rank fusion; the phrasings' chunk scores are
        then summed, so a chunk found by several phrasings rises. Chunks are grouped by session
        and copies of one conversation collapse into one result.

        Filters: ``after``/``before`` (hard, ISO), ``session_id``, ``exclude_sessions``,
        ``kind`` ("orchestrator", "agent" = anything resumable, or a harness name).
        ``window`` (ISO after, before) is a remembered time: with ``window_mode="filter"`` only
        chunks inside it count, falling back to all time (with a note) when nothing is found
        there; with ``"boost"`` chunks inside it score higher.
        """
        from utils import history_nav

        if window and window_mode == "prefer":
            # Remembered dates are often off by weeks: sessions inside the window first, then
            # strong matches from other times (flagged), so a misremembered date can't hide it.
            common = dict(extra_queries=extra_queries, hits_per_session=hits_per_session,
                          exclude_sessions=exclude_sessions, session_id=session_id, after=after,
                          before=before, kind=kind, kind_mode=kind_mode, debug=debug)
            inside = self.search(query, query_vec, max_sessions=max_sessions, window=window,
                                 window_mode="strict", **common)
            outside = self.search(query, query_vec, max_sessions=max_sessions, **common)
            have = {s["session_id"] for s in inside["sessions"]} | {c for s in inside["sessions"] for c in s.get("copies", [])}
            merged = list(inside["sessions"])
            for s in outside["sessions"]:
                if len(merged) >= max_sessions:
                    break
                if s["session_id"] in have or s.get("relevance") != "strong":
                    continue
                s["outside_time_window"] = True
                merged.append(s)
            return {"sessions": merged, "total_sessions": max(inside["total_sessions"], len(merged))}

        exclude = {s for s in exclude_sessions if s}
        conn = self._conn()
        try:
            self._ensure_loaded(conn)

            def allowed_fn(win: tuple[str, str] | None):
                def allowed(sid: str, ts: str) -> bool:
                    if sid in exclude or (session_id and sid != session_id):
                        return False
                    if kind and kind_mode == "filter":
                        h = self._harness.get(sid)
                        if kind == "agent":
                            if h == "orchestrator":
                                return False
                        elif h != kind:
                            return False
                    if after and ts and ts < after:
                        return False
                    if before and ts and ts >= before:
                        return False
                    if win and ts and not (win[0] <= ts < win[1]):
                        return False
                    return True
                return allowed

            phrasings = [(query, query_vec), *extra_queries]

            def rank_all(win):
                allowed = allowed_fn(win)
                return [self._rank_one(conn, q, v, allowed) for q, v in phrasings if (q or "").strip()]

            note = None
            ranked_runs = rank_all(window if window and window_mode in ("filter", "strict") else None)
            if window and window_mode == "filter" and not any(r["scores"] for r in ranked_runs):
                note = f"nothing found between {window[0]} and {window[1]}; showing matches from any time"
                ranked_runs = rank_all(None)

            scores: dict[int, float] = {}
            vec_sim: dict[int, float] = {}
            share: dict[int, float] = {}
            kw: set[int] = set()
            single_term = False
            for r in ranked_runs:
                for cid, sc in r["scores"].items():
                    scores[cid] = scores.get(cid, 0.0) + sc
                for cid, v in r["vec_sim"].items():
                    vec_sim[cid] = max(v, vec_sim.get(cid, 0.0))
                for cid, v in r["share"].items():
                    share[cid] = max(v, share.get(cid, 0.0))
                kw |= r["kw"]
                single_term = single_term or r["n_terms"] == 1
            if window and window_mode == "boost":
                ts_of = dict(zip(self._ids.tolist(), self._ts.tolist())) if scores else {}
                for cid in scores:
                    ts = ts_of.get(cid, "")
                    if ts and window[0] <= ts < window[1]:
                        scores[cid] *= WINDOW_BOOST
            if not scores:
                out: dict = {"sessions": [], "total_sessions": 0}
                if note:
                    out["note"] = note
                return out

            top_ids = sorted(scores, key=scores.get, reverse=True)[:CANDIDATES]
            info = {
                r[0]: r[1:]
                for r in conn.execute(
                    f"SELECT id, session_id, turn, role, ts, text, text_hash FROM chunks "
                    f"WHERE id IN ({','.join('?' * len(top_ids))})",
                    top_ids,
                )
            }
            by_session: dict[str, list[int]] = {}
            for cid in top_ids:
                if cid in info:
                    by_session.setdefault(info[cid][0], []).append(cid)

            conn.row_factory = sqlite3.Row
            sess_rows = {
                r["id"]: dict(r)
                for r in conn.execute(
                    f"SELECT * FROM sessions WHERE id IN ({','.join('?' * len(by_session))})", list(by_session)
                )
            }
            conn.row_factory = None

            def session_score(sid: str, cids: list[int]) -> float:
                best = [scores[c] for c in cids]  # top_ids order is already best-first
                score = best[0] + SESSION_W_NEXT * sum(best[1:3]) + SESSION_W_TAIL * sum(best[3:10])
                if sess_rows.get(sid, {}).get("about_history"):
                    score *= ABOUT_HISTORY_DEMOTION
                if kind and kind_mode == "prefer" and _kind_matches(kind, sess_rows.get(sid, {}).get("harness")):
                    score *= KIND_BOOST
                return score

            ranked = sorted(by_session.items(), key=lambda kv: session_score(*kv), reverse=True)
        finally:
            conn.close()

        def chunk_strength(cid: int) -> bool:
            sim = vec_sim.get(cid, 0.0)
            sh = share.get(cid, 0.0) if cid in kw else 0.0
            if single_term and sh:
                return True  # a one-word query (a name) found verbatim
            return is_strong(sim, sh, self._model)

        titles = _load_titles(self._context_dir)
        out_sessions: list[dict] = []
        shown_hashes: list[set[str]] = []
        for sid, cids in ranked:
            if len(out_sessions) >= max_sessions:
                break
            hashes = {info[c][5] for c in cids[:10]}
            dup_of = next(
                (i for i, h in enumerate(shown_hashes) if len(hashes & h) >= DUPLICATE_OVERLAP * len(hashes)),
                None,
            )
            if dup_of is not None:
                out_sessions[dup_of].setdefault("copies", []).append(sid)
                continue
            shown_hashes.append(hashes)
            row = sess_rows.get(sid) or {"id": sid}
            hits = []
            seen_turns: set[int] = set()
            for cid in cids:
                _, turn, role, ts, text, _h = info[cid]
                if turn < 0:  # the session summary: it ranked the session, the entry shows it
                    continue
                if turn in seen_turns:
                    continue
                seen_turns.add(turn)
                kinds = [k for k, hit in (("keyword", cid in kw), ("semantic", cid in vec_sim)) if hit]
                hits.append({
                    "turn": turn,
                    "role": role,
                    "date": ts,
                    "match": "+".join(kinds),
                    "text": text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS] + " …",
                })
                if len(hits) >= hits_per_session:
                    break
            hits.sort(key=lambda h: h["turn"])
            entry = history_nav.session_entry(row, titles)
            entry["relevance"] = "strong" if any(chunk_strength(c) for c in cids[:5]) else "weak"
            if debug:
                entry["evidence"] = {
                    "sim": round(max((vec_sim.get(c, 0.0) for c in cids[:5]), default=0.0), 3),
                    "share": round(max((share.get(c, 0.0) for c in cids[:5] if c in kw), default=0.0), 3),
                    "chunks": len(cids), "score": round(session_score(sid, cids), 5),
                }
            entry["matching_chunks"] = len(cids)
            entry["hits"] = hits
            if row.get("about_history"):
                entry["note"] = "this session was mostly about searching/reading other conversations"
            out_sessions.append(entry)
        out = {"sessions": out_sessions, "total_sessions": len(ranked)}
        if note:
            out["note"] = note
        return out


# ── Locating a conversation file (reading: utils/history_nav.py) ──────────────────


def find_session_file(session_id: str, context_dir: Path | None = None) -> Path | None:
    context_dir = Path(context_dir or get_context_dir())
    for p in (context_dir / f"{session_id}.jsonl", context_dir / "chats" / f"{session_id}.jsonl"):
        if p.is_file():
            return p
    try:
        from manager.codex.adapter import _codex_jsonl_candidates

        for p in _codex_jsonl_candidates(session_id):
            return p
    except Exception:  # noqa: BLE001
        logger.debug("Codex session lookup failed", exc_info=True)
    return None
