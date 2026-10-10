"""``truncate_voice_tool_output``: realtime voice caps function_call_output.

2026-10-08: an 18 KB ``search_history`` result was cut at 8 000 chars
mid-JSON — the model would have lost most sessions and the whole
``memory`` block. JSON objects are now shrunk structurally."""

from __future__ import annotations

import json

from orchestrator.providers.voice_base import (
    VOICE_TOOL_OUTPUT_MAX_CHARS,
    truncate_voice_tool_output,
)


def _search_like(n_sessions: int = 6, n_hits: int = 5) -> dict:
    return {
        "query": "dreams",
        "sessions": [
            {
                "session_id": f"sess-{i}",
                "title": f"Session {i}",
                "summary": "s" * 900,
                "hits": [{"turn": j, "text": "t" * 700} for j in range(n_hits)],
            }
            for i in range(n_sessions)
        ],
        "total_matching_sessions": n_sessions,
        "memory": [{"path": f"memory/note{i}.md", "excerpt": "m" * 900} for i in range(4)],
    }


def test_small_output_is_untouched():
    out = json.dumps({"ok": True})
    assert truncate_voice_tool_output(out) == out


def test_large_json_object_stays_valid_and_keeps_every_section():
    raw = json.dumps(_search_like())
    assert len(raw) > VOICE_TOOL_OUTPUT_MAX_CHARS

    out = truncate_voice_tool_output(raw)

    assert len(out) <= VOICE_TOOL_OUTPUT_MAX_CHARS
    parsed = json.loads(out)
    assert "_voice_truncated" in parsed
    assert parsed["query"] == "dreams"
    assert parsed["sessions"], "keeps the top sessions"
    assert parsed["sessions"][0]["session_id"] == "sess-0", "keeps order (best first)"
    assert parsed["memory"], "the trailing memory section survives"


def test_read_file_payload_keeps_the_resume_hint():
    raw = json.dumps({
        "path": "x.md", "start_line": 1, "end_line": 900, "total_lines": 2000,
        "content": "line\n" * 3000,
    })
    out = truncate_voice_tool_output(raw)
    assert "Call read_file with start_line=" in out


def test_non_json_output_falls_back_to_a_char_cut():
    raw = "x" * (VOICE_TOOL_OUTPUT_MAX_CHARS * 2)
    out = truncate_voice_tool_output(raw)
    assert out.startswith("x" * VOICE_TOOL_OUTPUT_MAX_CHARS)
    assert out.endswith("[output truncated — too large for realtime voice.]")
