"""Tests for utils/history_index.py — conversation-history extraction, indexing and search.

The encoder is a deterministic bag-of-words hash (no model download), so "semantic"
similarity here is word overlap — enough to exercise ranking, fusion and filtering.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

from utils import history_index as hi


# ── helpers ──────────────────────────────────────────────────────────────────


def _vec(text: str) -> list[float]:
    v = np.zeros(hi.EMBED_DIM, dtype=np.float32)
    for w in re.findall(r"\w+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % hi.EMBED_DIM] += 1.0
    return v.tolist()


class CountingEncoder:
    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [_vec(t) for t in texts]


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    return path


def _claude(role: str, text, ts="2026-05-01T10:00:00Z", **extra) -> dict:
    content = text if isinstance(text, list) else [{"type": "text", "text": text}]
    return {"type": role, "message": {"role": role, "content": content}, "timestamp": ts, **extra}


def _orch_meta() -> dict:
    return {"type": "orchestrator_meta", "orchestrator": True, "session_id": "x"}


def _orch(role: str, text: str, ts="2026-06-01T10:00:00Z") -> dict:
    return {"type": role, "message": {"role": role, "content": text}, "timestamp": ts}


@pytest.fixture
def ctx(tmp_path: Path) -> Path:
    d = tmp_path / "context"
    (d / "chats").mkdir(parents=True)
    return d


@pytest.fixture
def conn(tmp_path: Path):
    c = hi.connect(tmp_path / "index" / "history.sqlite3")
    yield c
    c.close()


# ── extraction ───────────────────────────────────────────────────────────────


class TestExtraction:
    def test_claude_skips_noise_and_merges_blocks(self, ctx):
        path = _write(ctx / "c1.jsonl", [
            {"type": "queue-operation", "operation": "enqueue"},
            _claude("user", "Please fix the soundbar movie mode <system-reminder>secret</system-reminder>"),
            _claude("assistant", "Looking at the soundbar code now."),
            _claude("assistant", [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]),
            _claude("user", [{"type": "tool_result", "tool_use_id": "1", "content": "file body"}]),
            _claude("assistant", "Fixed the movie mode toggle."),
            _claude("user", "<task-notification><task-id>1</task-id></task-notification>"),
            _claude("user", "This session is being continued from a previous conversation that ran out"),
            _claude("user", "summary text", isCompactSummary=True),
            _claude("user", "meta text", isMeta=True),
            _claude("user", "[Request interrupted by user]"),
        ])
        doc = hi.extract_session(path)
        assert doc.harness == "claude"
        assert [(t.role, t.text) for t in doc.turns] == [
            ("user", "Please fix the soundbar movie mode"),
            ("assistant", "Looking at the soundbar code now.\n\nFixed the movie mode toggle."),
        ]
        assert doc.tool_calls == 1 and not doc.about_history

    def test_orchestrator_drops_narration_of_search_results(self, ctx):
        path = _write(ctx / "o1.jsonl", [
            _orch_meta(),
            _orch("user", "[voice] Do you remember the Shroud of Turin talk?"),
            _orch("assistant", "Let me check."),
            {"type": "tool_use", "tool_name": "search_history", "tool_input": {"query": "turin"}},
            {"type": "tool_result", "output": "{...}"},
            _orch("assistant", "I found the Shroud of Turin discussion about rituals."),
            _orch("user", "[audio:wav] (audio message)"),
            _orch("user", "[voice] Thanks, now tell me about lamps"),
            _orch("assistant", "Lamps are on the second floor."),
            _orch("assistant", "Lamps are on the second floor."),  # voice double-persist
        ])
        doc = hi.extract_session(path)
        assert doc.harness == "orchestrator"
        assert [(t.role, t.text) for t in doc.turns] == [
            ("user", "Do you remember the Shroud of Turin talk?"),
            ("assistant", "Let me check."),
            ("user", "Thanks, now tell me about lamps"),
            ("assistant", "Lamps are on the second floor."),
        ]

    def test_qwen_parts_without_thoughts(self, ctx):
        path = _write(ctx / "chats" / "q1.jsonl", [
            {"type": "user", "message": {"role": "user", "parts": [{"text": "What model are you?"}]}},
            {"type": "system", "subtype": "telemetry"},
            {"type": "assistant", "message": {"role": "model", "parts": [
                {"text": "thinking...", "thought": True}, {"text": "I am Qwen."}]}},
            {"type": "tool_result", "message": {"role": "user", "parts": [{"functionResponse": {}}]}},
        ])
        doc = hi.extract_session(path)
        assert doc.harness == "qwen"
        assert [(t.role, t.text) for t in doc.turns] == [
            ("user", "What model are you?"), ("assistant", "I am Qwen."),
        ]

    def test_gemini_format(self, ctx):
        path = _write(ctx / "chats" / "session-2026-05-16T14-48-abc.jsonl", [
            {"sessionId": "abc", "projectHash": "h", "startTime": "t", "kind": "main"},
            {"id": "1", "timestamp": "2026-05-16T14:48:12Z", "type": "user", "content": [{"text": "Hello Gemini"}]},
            {"$set": {"lastUpdated": "x"}},
            {"id": "2", "timestamp": "2026-05-16T14:48:18Z", "type": "gemini", "content": "Hello Rodrigo"},
        ])
        doc = hi.extract_session(path)
        assert doc.harness == "gemini"
        assert [t.text for t in doc.turns] == ["Hello Gemini", "Hello Rodrigo"]
        assert doc.started_at == "2026-05-16T14:48:12Z"

    def test_gemini_session_id_is_the_header_id(self, ctx):
        """The file name only carries id[:8]; SessionStore, titles and resume use the header id."""
        sid = "9901da9c-d9da-4f55-979f-cbd70b255708"
        path = _write(ctx / "chats" / "session-2026-10-08T19-49-9901da9c.jsonl", [
            {"sessionId": sid, "projectHash": "h", "startTime": "t", "kind": "main"},
            {"id": "1", "timestamp": "2026-10-08T19:49:16Z", "type": "user", "content": [{"text": "Hi"}]},
        ])
        assert hi.session_id_for(path) == sid
        assert hi.extract_session(path).session_id == sid
        # A header-less file keeps its file name as the id.
        bare = _write(ctx / "chats" / "session-2026-10-08T19-50-deadbeef.jsonl", [
            {"id": "1", "type": "user", "content": [{"text": "Hi"}]},
        ])
        assert hi.session_id_for(bare) == bare.stem

    def test_blobs_are_stripped(self, ctx):
        blob = "A" * 500
        doc = hi.extract_session(_write(ctx / "b.jsonl", [_claude("user", f"see image {blob} please")]))
        assert blob not in doc.turns[0].text

    def test_about_history_flag(self, ctx):
        reads = [
            _claude("assistant", [{"type": "tool_use", "name": "Bash",
                                   "input": {"command": f"grep -l shroud context/{i}.jsonl"}}])
            for i in range(5)
        ]
        doc = hi.extract_session(_write(ctx / "meta.jsonl", [_claude("user", "find old talks")] + reads))
        assert doc.history_tool_calls == 5 and doc.about_history


# ── chunking ─────────────────────────────────────────────────────────────────


class TestChunking:
    def test_short_text_is_one_window(self):
        assert hi.split_words("one two three") == ["one two three"]

    def test_long_text_overlaps_and_keeps_formatting(self):
        words = [f"w{i}" for i in range(250)]
        text = "\n".join(" ".join(words[i : i + 10]) for i in range(0, 250, 10))
        parts = hi.split_words(text, size=100, overlap=20)
        assert len(parts) == 3
        assert parts[0].split()[-20:] == parts[1].split()[:20]
        assert "\n" in parts[0]
        assert parts[-1].split()[-1] == "w249"

    def test_tiny_turns_are_skipped(self, ctx):
        doc = hi.extract_session(_write(ctx / "t.jsonl", [
            _claude("user", "Yeah."), _claude("assistant", "That works for the soundbar setup."),
        ]))
        chunks = hi.chunk_session(doc)
        assert [(c.turn, c.role) for c in chunks] == [(1, "assistant")]


# ── indexing ─────────────────────────────────────────────────────────────────


class TestIndexing:
    def test_incremental_reuses_embeddings(self, ctx, conn):
        path = _write(ctx / "s1.jsonl", [
            _claude("user", "Tell me about the Fire TV setup in the living room"),
            _claude("assistant", "The Fire TV is connected over ADB on the local network"),
        ])
        enc = CountingEncoder()
        assert hi.index_session(conn, path, enc) == 2
        lines = path.read_text() + json.dumps(_claude("user", "And how do I restart the ADB server?")) + "\n"
        path.write_text(lines)
        enc2 = CountingEncoder()
        assert hi.index_session(conn, path, enc2) == 1
        assert enc2.texts == ["And how do I restart the ADB server?"]
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 3

    def test_index_all_isolates_failures_and_removes_deleted(self, ctx, conn):
        good = _write(ctx / "good.jsonl", [_claude("user", "the good session about lamps")])
        bad = _write(ctx / "bad.jsonl", [_claude("user", "the bad session about lamps")])

        def enc(texts):
            if any("bad" in t for t in texts):
                raise TimeoutError("encode timed out")
            return [_vec(t) for t in texts]

        stats = hi.index_all(conn, [bad, good], enc, log=lambda m: None)
        assert stats.indexed == 1 and [n for n, _ in stats.failed] == ["bad.jsonl"]
        assert {r[0] for r in conn.execute("SELECT id FROM sessions")} == {"good"}

        stats = hi.index_all(conn, [good], enc, log=lambda m: None)
        assert stats.unchanged == 1

        good.unlink()
        stats = hi.index_all(conn, [], enc, log=lambda m: None)
        assert stats.removed == 1
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'lamps'").fetchone()[0] == 0

    def test_session_sources_cover_chats_newest_first(self, ctx):
        import os

        a = _write(ctx / "a.jsonl", [])
        b = _write(ctx / "chats" / "b.jsonl", [])
        os.utime(a, (1, 1))
        assert hi.session_sources(ctx, ctx / "chats") == [b, a]

    def test_lock_is_exclusive(self, tmp_path):
        db = tmp_path / "h.sqlite3"
        first, second = hi.IndexLock(db), hi.IndexLock(db)
        assert first.acquire()
        assert not second.acquire()
        first.release()
        assert second.acquire()
        second.release()


# ── search ───────────────────────────────────────────────────────────────────


def _build(ctx: Path, conn, sessions: dict[str, list[dict]], titles: dict | None = None):
    for name, lines in sessions.items():
        hi.index_session(conn, _write(ctx / f"{name}.jsonl", lines), CountingEncoder())
    (ctx / ".titles.json").write_text(json.dumps(titles or {}))
    return hi.HistorySearcher(Path(conn.execute("PRAGMA database_list").fetchone()[2]), ctx)


def _search(searcher, query, **kw):
    return searcher.search(query, _vec(query), **kw)


class TestSearch:
    def test_keyword_finds_rare_name_and_reports_session(self, ctx, conn):
        s = _build(ctx, conn, {
            "turin": [_claude("user", "I think the Shroud of Turin was an ancient photograph", ts="2026-04-01T00:00:00Z"),
                      _claude("assistant", "That is the proto-photography hypothesis about the shroud")],
            "lamps": [_claude("user", "Turn on the living room lamps please and dim them")],
        }, titles={"turin": "spiritual chat"})
        r = _search(s, "Shroud of Turin")
        assert [x["session_id"] for x in r["sessions"]] == ["turin"]
        top = r["sessions"][0]
        assert top["title"] == "spiritual chat" and top["relevance"] == "strong"
        assert top["started_at"] == "2026-04-01T00:00:00Z"
        assert top["hits"][0]["turn"] == 0 and "keyword" in top["hits"][0]["match"]

    def test_single_shared_common_word_is_not_a_match(self, ctx, conn):
        s = _build(ctx, conn, {
            "x": [_claude("user", "switch the editor into dark mode for the night")],
        })
        assert _search(s, "soundbar movie mode")["sessions"] == []

    def test_exclude_and_session_filters(self, ctx, conn):
        s = _build(ctx, conn, {
            "a": [_claude("user", "the wake word fired twice during the movie")],
            "b": [_claude("user", "the wake word never fired from the kitchen")],
        })
        ids = lambda r: sorted(x["session_id"] for x in r["sessions"])
        assert ids(_search(s, "wake word fired")) == ["a", "b"]
        assert ids(_search(s, "wake word fired", exclude_sessions=["a"])) == ["b"]
        assert ids(_search(s, "wake word fired", session_id="a")) == ["a"]

    def test_date_filters(self, ctx, conn):
        s = _build(ctx, conn, {
            "old": [_claude("user", "the jetson thermal fan profile tuning", ts="2026-03-01T00:00:00Z")],
            "new": [_claude("user", "the jetson thermal fan profile again", ts="2026-09-01T00:00:00Z")],
        })
        assert [x["session_id"] for x in _search(s, "jetson thermal fan", after="2026-06-01")["sessions"]] == ["new"]
        assert [x["session_id"] for x in _search(s, "jetson thermal fan", before="2026-06-01")["sessions"]] == ["old"]

    def test_copies_of_a_conversation_collapse(self, ctx, conn):
        lines = [_claude("user", "we tested the amygdala analogy for the voice agent"),
                 _claude("assistant", "the amygdala analogy maps to the fast interrupt path")]
        s = _build(ctx, conn, {"orig": lines, "copy": lines})
        r = _search(s, "amygdala analogy")
        assert len(r["sessions"]) == 1
        assert r["sessions"][0]["copies"] == [
            {"orig", "copy"}.difference({r["sessions"][0]["session_id"]}).pop()
        ]

    def test_about_history_sessions_are_demoted(self, ctx, conn):
        reads = [
            _claude("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": f"cat context/{i}.jsonl"}}])
            for i in range(6)
        ]
        s = _build(ctx, conn, {
            "meta": [_claude("user", "search history for bicameral mind bicameral mind rituals")] + reads,
            "real": [_claude("user", "the bicameral mind and ancient rituals hypothesis from Jaynes")],
        })
        r = _search(s, "bicameral mind rituals")
        assert [x["session_id"] for x in r["sessions"]] == ["real", "meta"]
        assert "note" in r["sessions"][1]

    def test_index_changes_are_picked_up_without_restart(self, ctx, conn):
        s = _build(ctx, conn, {"a": [_claude("user", "the tarot canvas layout uses a grid")]})
        assert len(_search(s, "tarot canvas")["sessions"]) == 1
        hi.index_session(conn, _write(ctx / "b.jsonl", [_claude("user", "tarot canvas colors look washed out")]),
                         CountingEncoder())
        assert len(_search(s, "tarot canvas")["sessions"]) == 2

    def test_min_terms_required(self):
        assert [hi.min_terms_required(n) for n in (1, 2, 3, 5, 6, 9)] == [1, 1, 2, 2, 2, 3]

    def test_missing_index_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            hi.HistorySearcher(tmp_path / "nope.sqlite3").search("x", None)


# ── stage 2: phrasings, kind, time window, session metadata ─────────────────


class TestSearchStage2:
    def test_extra_phrasings_are_fused(self, ctx, conn):
        s = _build(ctx, conn, {
            "pt": [_claude("user", "a gente configurou o roteamento de audio do OBS com o JACK ontem")],
            "en": [_claude("user", "the tarot canvas layout uses a grid of cards")],
        })
        alone = _search(s, "audio routing setup")
        both = s.search("audio routing setup", _vec("audio routing setup"),
                        extra_queries=[("roteamento de audio OBS JACK", _vec("roteamento de audio OBS JACK"))])
        assert [x["session_id"] for x in both["sessions"]][:1] == ["pt"]
        assert "pt" not in [x["session_id"] for x in alone["sessions"]]

    def test_kind_filter(self, ctx, conn):
        s = _build(ctx, conn, {
            "o1": [_orch_meta(), _orch("user", "the soundbar movie mode keeps resetting")],
            "c1": [_claude("user", "the soundbar movie mode keeps resetting after reboot")],
        })
        ids = lambda **kw: sorted(x["session_id"] for x in s.search("soundbar movie mode", _vec("soundbar movie mode"), **kw)["sessions"])
        assert ids(kind="orchestrator") == ["o1"]
        assert ids(kind="agent") == ["c1"]
        assert ids() == ["c1", "o1"]

    def test_window_filter_with_fallback(self, ctx, conn):
        s = _build(ctx, conn, {
            "june": [_claude("user", "the jetson fan curve was too loud", ts="2026-06-10T10:00:00Z")],
            "sept": [_claude("user", "the jetson fan curve again too loud", ts="2026-09-10T10:00:00Z")],
        })
        r = s.search("jetson fan curve", _vec("jetson fan curve"), window=("2026-06-01", "2026-07-01"))
        assert [x["session_id"] for x in r["sessions"]] == ["june"]
        r = s.search("jetson fan curve", _vec("jetson fan curve"), window=("2025-01-01", "2025-02-01"))
        assert {x["session_id"] for x in r["sessions"]} == {"june", "sept"} and "note" in r

    def test_results_say_how_to_open_and_where(self, ctx, conn):
        s = _build(ctx, conn, {
            "o1": [_orch_meta(), _orch("user", "remember the lamps automation with tuya scenes")],
            "c1": [_claude("user", "fix the tuya lamps automation script", cwd="/home/rodrigo/assistant")],
        })
        by_id = {x["session_id"]: x for x in _search(s, "tuya lamps automation")["sessions"]}
        assert "switch_conversation(session_id='o1')" in by_id["o1"]["open"]["how"]
        assert by_id["c1"]["open"]["can_resume"] is True and "resume_conversation(session_id='c1')" in by_id["c1"]["open"]["how"]
        assert by_id["c1"]["cwd"] == "/home/rodrigo/assistant" and by_id["c1"]["file"].endswith("c1.jsonl")
        assert by_id["c1"]["kind"] == "claude"


# ── Codex rollouts ────────────────────────────────────────────────────


def test_codex_rollout_is_extracted_with_thread_id():
    from pathlib import Path

    from utils import history_index as hi

    fixture = next((Path(__file__).parent / "fixtures" / "codex").glob("rollout-*.jsonl"))
    doc = hi.extract_session(fixture)
    assert doc.harness == "codex"
    assert doc.session_id == "01a118ca-7254-75a1-8888-c5a12eae0a9c"
    assert hi.session_id_for(fixture) == doc.session_id
    assert doc.turns and doc.turns[0].role == "user"
    # Codex's injected AGENTS.md / environment messages are not turns
    assert not any("AGENTS.md instructions" in t.text or "<environment_context>" in t.text for t in doc.turns)


def test_session_sources_includes_codex_rollouts(tmp_path, monkeypatch):
    from utils import history_index as hi

    rollout = tmp_path / "codex" / "2026" / "10" / "07" / "rollout-2026-10-07T20-54-43-abc.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    monkeypatch.setattr(hi, "_codex_sources", lambda project_dir=None: [rollout])
    (tmp_path / "ctx").mkdir()
    sources = hi.session_sources(tmp_path / "ctx", tmp_path / "ctx" / "chats")
    assert rollout in sources


def test_codex_sources_are_scoped_to_the_indexed_context(tmp_path):
    """A context/ other than this install's sees only its own context/codex/sessions —
    never this machine's CODEX_HOME rollouts."""
    from utils import history_index as hi

    ctx = tmp_path / "context"
    rollout = ctx / "codex" / "sessions" / "2026" / "10" / "08" / (
        "rollout-2026-10-08T16-53-54-01a11d14-5790-7c60-a0e4-f3bf113ee2df.jsonl")
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    assert hi._codex_sources(tmp_path) == [rollout]
    assert hi.session_sources(ctx, ctx / "chats") == [rollout]
