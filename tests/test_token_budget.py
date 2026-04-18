"""Tests for the orchestrator's token-budget helpers and voice history flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.token_budget import (
    RECENT_VERBATIM_TOKENS,
    TOOL_RESULT_TRUNCATE_CHARS,
    TOOL_RESULT_TRUNCATE_SUFFIX,
    estimate_message_tokens,
    estimate_tokens,
    scale_summary_max_tokens,
    split_by_token_budget,
    truncate_tool_results,
)


class TestEstimate:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_short_string(self):
        # 7 chars / 3.5 = 2 tokens
        assert estimate_tokens("seven c") == 2

    def test_message_string_content(self):
        msg = {"role": "user", "content": "hello world!!"}
        # 13 chars / 3.5 = 3 + 4 overhead = 7
        assert estimate_message_tokens(msg) == 7

    def test_message_with_tool_blocks(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "name": "read", "input": {"path": "/a.txt"}},
            ],
        }
        t = estimate_message_tokens(msg)
        assert t > 8  # overhead + text + tool_use fields


class TestTruncateToolResults:
    def test_small_result_unchanged(self):
        msgs = [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "short"}],
        }]
        out = truncate_tool_results(msgs)
        assert out[0]["content"][0]["content"] == "short"

    def test_large_result_clipped(self):
        big = "A" * (TOOL_RESULT_TRUNCATE_CHARS + 500)
        msgs = [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": big}],
        }]
        out = truncate_tool_results(msgs)
        result = out[0]["content"][0]["content"]
        assert result.startswith("A" * TOOL_RESULT_TRUNCATE_CHARS)
        assert result.endswith(TOOL_RESULT_TRUNCATE_SUFFIX)
        assert len(result) < len(big)

    def test_does_not_mutate_input(self):
        big = "B" * (TOOL_RESULT_TRUNCATE_CHARS + 100)
        msgs = [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": big}],
        }]
        truncate_tool_results(msgs)
        # Original is untouched
        assert msgs[0]["content"][0]["content"] == big

    def test_list_form_tool_result(self):
        big_text = "C" * (TOOL_RESULT_TRUNCATE_CHARS + 50)
        msgs = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "x",
                "content": [{"type": "text", "text": big_text}],
            }],
        }]
        out = truncate_tool_results(msgs)
        # List form is coerced to a clipped string
        assert isinstance(out[0]["content"][0]["content"], str)
        assert out[0]["content"][0]["content"].endswith(TOOL_RESULT_TRUNCATE_SUFFIX)

    def test_text_blocks_preserved(self):
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "read", "id": "x", "input": {}},
            ],
        }]
        out = truncate_tool_results(msgs)
        assert out == msgs  # no tool_result, nothing to change


class TestSplitByBudget:
    def test_empty(self):
        older, recent = split_by_token_budget([], 1000)
        assert older == [] and recent == []

    def test_all_fit(self):
        msgs = [{"role": "user", "content": "hi"} for _ in range(5)]
        older, recent = split_by_token_budget(msgs, 1000)
        assert older == []
        assert len(recent) == 5

    def test_partial_fit_keeps_newest(self):
        # 20 messages of ~30 tokens each (~content = 100 chars → 28 + 4 overhead)
        msgs = [{"role": "user", "content": "x" * 100} for _ in range(20)]
        older, recent = split_by_token_budget(msgs, 100)  # very small budget
        assert older + recent == msgs  # no loss
        assert len(recent) >= 1  # at least one verbatim
        assert len(older) > 0  # some go to summary

    def test_always_keeps_at_least_one(self):
        huge = {"role": "user", "content": "x" * 10000}
        older, recent = split_by_token_budget([huge], 1)
        assert older == []
        assert recent == [huge]


class TestScaleSummary:
    def test_zero_messages(self):
        assert scale_summary_max_tokens(0, 0) == 0

    def test_tiny_prefix_gets_floor(self):
        assert scale_summary_max_tokens(3, 100) == 256

    def test_large_prefix_scales(self):
        # 50k prefix tokens / 10 = 5000, capped at SUMMARY_MAX_TOKENS
        from orchestrator.token_budget import SUMMARY_MAX_TOKENS
        assert scale_summary_max_tokens(300, 50_000) == SUMMARY_MAX_TOKENS


class TestBuildHistoryForPrompt:
    """Integration-ish tests for OrchestratorSession._build_history_for_prompt."""

    @pytest.fixture
    def session_with_jsonl(self, tmp_path, monkeypatch):
        from orchestrator.session import OrchestratorSession
        from orchestrator.config import OrchestratorConfig

        # Point sessions dir at tmp_path so the session writes/reads there
        monkeypatch.setattr(
            "orchestrator.session.get_sessions_dir",
            lambda: tmp_path,
        )

        cfg = OrchestratorConfig(
            project_dir=str(tmp_path),
            memory_path=str(tmp_path / "memory.md"),
        )
        sess = OrchestratorSession(config=cfg, context={}, voice=True)
        return sess, tmp_path

    @pytest.mark.asyncio
    async def test_empty_jsonl_returns_empty(self, session_with_jsonl):
        sess, tmp_path = session_with_jsonl
        sess._jsonl_path = tmp_path / "missing.jsonl"
        recent, summary = await sess._build_history_for_prompt()
        assert recent == [] and summary is None

    @pytest.mark.asyncio
    async def test_short_history_no_summary(self, session_with_jsonl, monkeypatch):
        sess, tmp_path = session_with_jsonl
        jsonl = tmp_path / "s.jsonl"
        jsonl.write_text(
            '{"type":"user","message":{"role":"user","content":"hi"}}\n'
            '{"type":"assistant","message":{"role":"assistant","content":"hello"}}\n'
        )
        sess._jsonl_path = jsonl

        recent, summary = await sess._build_history_for_prompt()
        assert len(recent) == 2
        assert summary is None  # short enough, no summary needed

    @pytest.mark.asyncio
    async def test_long_history_triggers_summary(self, session_with_jsonl, monkeypatch):
        sess, tmp_path = session_with_jsonl
        jsonl = tmp_path / "s.jsonl"

        # Pile of messages big enough to blow the recent-verbatim budget
        lines = []
        big_text = "x" * 5000  # ~1400 tokens each
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            lines.append(
                f'{{"type":"{role}","message":{{"role":"{role}","content":"{big_text}-{i}"}}}}\n'
            )
        jsonl.write_text("".join(lines))
        sess._jsonl_path = jsonl

        mock_summarize = AsyncMock(return_value="SUMMARY-TEXT")
        monkeypatch.setattr(sess, "_summarize_history", mock_summarize)

        recent, summary = await sess._build_history_for_prompt()

        assert summary == "SUMMARY-TEXT"
        assert mock_summarize.called
        # Some messages should go to summary, some to recent
        assert len(recent) > 0
        assert len(recent) < 20

    @pytest.mark.asyncio
    async def test_tool_results_are_clipped_before_budgeting(
        self, session_with_jsonl, monkeypatch
    ):
        sess, tmp_path = session_with_jsonl
        jsonl = tmp_path / "s.jsonl"

        # One user msg + tool_use + massive tool_result + assistant reply
        huge = "Z" * 20000
        jsonl.write_text(
            '{"type":"user","message":{"role":"user","content":"do the thing"}}\n'
            '{"type":"tool_use","tool_call_id":"t1","tool_name":"read","tool_input":{}}\n'
            f'{{"type":"tool_result","tool_call_id":"t1","output":"{huge}"}}\n'
            '{"type":"assistant","message":{"role":"assistant","content":"done"}}\n'
        )
        sess._jsonl_path = jsonl

        mock_summarize = AsyncMock(return_value="SUM")
        monkeypatch.setattr(sess, "_summarize_history", mock_summarize)

        recent, _summary = await sess._build_history_for_prompt()

        # Find the tool_result in recent and confirm it was clipped
        found = False
        for msg in recent:
            if not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    assert TOOL_RESULT_TRUNCATE_SUFFIX in block["content"]
                    assert len(block["content"]) < len(huge)
                    found = True
        assert found, "Expected at least one clipped tool_result in recent"


class TestVoiceSessionUpdateReloadsHistory:
    """Regression test for the stale-history bug: the second voice start must
    see messages written to JSONL after the first start()."""

    @pytest.mark.asyncio
    async def test_second_get_session_update_sees_new_jsonl_entries(
        self, tmp_path, monkeypatch
    ):
        from orchestrator.session import OrchestratorSession
        from orchestrator.config import OrchestratorConfig

        monkeypatch.setattr(
            "orchestrator.session.get_sessions_dir",
            lambda: tmp_path,
        )

        cfg = OrchestratorConfig(
            project_dir=str(tmp_path),
            memory_path=str(tmp_path / "memory.md"),
        )

        # Seed JSONL with a resume id
        jsonl = tmp_path / "res1.jsonl"
        jsonl.write_text(
            '{"type":"orchestrator_meta","session_id":"res1"}\n'
            '{"type":"user","message":{"role":"user","content":"original"}}\n'
        )

        sess = OrchestratorSession(
            config=cfg, context={}, session_id="res1", voice=True,
        )

        # Stub out the voice provider so start() doesn't need OpenAI
        fake_provider = MagicMock()
        fake_provider.get_session_update_payload = MagicMock(
            side_effect=lambda system, tools: {"system": system}
        )

        async def fake_start():
            # Mirror what start() does without the network call
            from orchestrator.agent import OrchestratorAgent
            from orchestrator.tools import registry
            from orchestrator.persistence import HistoryWriter
            sess._voice_provider = fake_provider
            sess._current_provider = fake_provider
            sess._agent = OrchestratorAgent(
                config=sess._config,
                registry=registry,
                provider=fake_provider,
                context=sess._context,
            )
            sess._jsonl_path = jsonl
            sess._writer = HistoryWriter(jsonl)
            from orchestrator.persistence import HistoryLoader
            sess._agent.history = HistoryLoader(jsonl).load()
            return sess._local_id

        monkeypatch.setattr(sess, "start", fake_start)

        await sess.start()

        # First session.update — should see only "original"
        update1 = await sess.get_session_update()
        assert "original" in update1["system"]

        # Simulate a voice turn writing to JSONL (as process_voice_event does)
        with open(jsonl, "a") as f:
            f.write(
                '{"type":"user","message":{"role":"user","content":"'
                'NEWLY_SPOKEN_MESSAGE"},"source":"voice_transcription"}\n'
            )

        # Second session.update — must include the new message
        update2 = await sess.get_session_update()
        assert "NEWLY_SPOKEN_MESSAGE" in update2["system"], (
            "Second get_session_update must reload history from JSONL"
        )
