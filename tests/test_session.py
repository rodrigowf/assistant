"""Tests for manager/session.py — mocked SDK, no real Claude Code."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manager.config import ManagerConfig
from manager.session import SessionManager
from manager.types import (
    CompactComplete,
    SessionStatus,
    TextComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)


# ---------------------------------------------------------------------------
# Helpers — async generator wrappers for mocking
# ---------------------------------------------------------------------------

def _mock_result(session_id="test-session-123", cost=0.01, num_turns=1, is_error=False):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=num_turns,
        session_id=session_id,
        total_cost_usd=cost,
        usage={"input_tokens": 100, "output_tokens": 50},
        result="done",
        structured_output=None,
    )


def _make_mock_client(init_messages=None, response_messages_fn=None, server_info=None):
    """Create a mock ClaudeSDKClient.

    receive_messages and receive_response must return async iterators directly
    (not coroutines), so we use plain functions that return async generators.

    Args:
        init_messages: Messages to yield from receive_messages (legacy, unused now)
        response_messages_fn: Function returning async iterator for receive_response
        server_info: Dict to return from get_server_info (default: {"session_id": "test-session"})
    """
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.interrupt = MagicMock()

    # get_server_info returns initialization data after connect()
    if server_info is None:
        server_info = {"session_id": "test-session"}
    client.get_server_info = AsyncMock(return_value=server_info)

    async def _receive_messages():
        if init_messages:
            for msg in init_messages:
                yield msg

    client.receive_messages = _receive_messages

    if response_messages_fn is not None:
        client.receive_response = response_messages_fn

    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionManagerLifecycle:
    @pytest.mark.asyncio
    async def test_start_returns_session_id(self):
        client = _make_mock_client(server_info={"session_id": "abc-123"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            sid = await sm.start()

            assert sid == "abc-123"
            assert sm.session_id == "abc-123"
            assert sm.status == SessionStatus.IDLE

    @pytest.mark.asyncio
    async def test_stop_disconnects(self):
        client = _make_mock_client(server_info={"session_id": "abc"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()
            await sm.stop()

            assert sm.status == SessionStatus.DISCONNECTED
            client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        client = _make_mock_client(server_info={"session_id": "ctx"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            async with SessionManager() as sm:
                assert sm.session_id == "ctx"

            client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_uses_provided_session_id(self):
        """When resuming a session, start() returns the provided session_id."""
        # server_info doesn't include session_id (realistic scenario)
        client = _make_mock_client(server_info={"commands": [], "output_style": "plain"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager(session_id="existing-session-abc")
            sid = await sm.start()

            # Should use the resume_id, not generate a new UUID
            assert sid == "existing-session-abc"
            assert sm.session_id == "existing-session-abc"


class TestSessionManagerSend:
    @pytest.mark.asyncio
    async def test_send_without_start_raises(self):
        sm = SessionManager()
        with pytest.raises(RuntimeError, match="not connected"):
            async for _ in sm.send("hello"):
                pass

    @pytest.mark.asyncio
    async def test_text_streaming(self):
        """StreamEvent text deltas yield TextDelta events."""
        from claude_agent_sdk import SystemMessage
        from claude_agent_sdk.types import StreamEvent

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        stream1 = StreamEvent(
            uuid="u1", session_id="s1",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
            parent_tool_use_id=None,
        )
        stream2 = StreamEvent(
            uuid="u2", session_id="s1",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": " world"}},
            parent_tool_use_id=None,
        )
        result = _mock_result(session_id="s1")

        async def fake_response():
            yield stream1
            yield stream2
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.send("hi"):
                events.append(event)

        text_deltas = [e for e in events if isinstance(e, TextDelta)]
        assert len(text_deltas) == 2
        assert text_deltas[0].text == "Hello"
        assert text_deltas[1].text == " world"
        assert isinstance(events[-1], TurnComplete)

    @pytest.mark.asyncio
    async def test_assistant_text_block(self):
        """AssistantMessage with TextBlock yields TextComplete."""
        from claude_agent_sdk import AssistantMessage, SystemMessage, TextBlock

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        assistant_msg = AssistantMessage(
            content=[TextBlock(text="Full response")],
            model="test",
            parent_tool_use_id=None,
            error=None,
        )
        result = _mock_result()

        async def fake_response():
            yield assistant_msg
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.send("hi"):
                events.append(event)

        text_completes = [e for e in events if isinstance(e, TextComplete)]
        assert len(text_completes) == 1
        assert text_completes[0].text == "Full response"

    @pytest.mark.asyncio
    async def test_tool_use_events(self):
        """AssistantMessage with ToolUseBlock yields ToolUse."""
        from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        assistant_msg = AssistantMessage(
            content=[ToolUseBlock(id="tool1", name="Bash", input={"command": "ls"})],
            model="test",
            parent_tool_use_id=None,
            error=None,
        )
        result = _mock_result()

        async def fake_response():
            yield assistant_msg
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.send("list files"):
                events.append(event)

        tool_uses = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_uses) == 1
        assert tool_uses[0].tool_name == "Bash"
        assert tool_uses[0].tool_input == {"command": "ls"}

    @pytest.mark.asyncio
    async def test_thinking_events(self):
        """ThinkingBlock yields ThinkingComplete."""
        from claude_agent_sdk import AssistantMessage, SystemMessage, ThinkingBlock

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        assistant_msg = AssistantMessage(
            content=[ThinkingBlock(thinking="Let me think...", signature="sig")],
            model="test",
            parent_tool_use_id=None,
            error=None,
        )
        result = _mock_result()

        async def fake_response():
            yield assistant_msg
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.send("think about this"):
                events.append(event)

        thinking = [e for e in events if isinstance(e, ThinkingComplete)]
        assert len(thinking) == 1
        assert thinking[0].text == "Let me think..."

    @pytest.mark.asyncio
    async def test_thinking_delta_streaming(self):
        """StreamEvent thinking deltas yield ThinkingDelta events."""
        from claude_agent_sdk import SystemMessage
        from claude_agent_sdk.types import StreamEvent

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        stream = StreamEvent(
            uuid="u1", session_id="s1",
            event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
            parent_tool_use_id=None,
        )
        result = _mock_result()

        async def fake_response():
            yield stream
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.send("think"):
                events.append(event)

        thinking_deltas = [e for e in events if isinstance(e, ThinkingDelta)]
        assert len(thinking_deltas) == 1
        assert thinking_deltas[0].text == "hmm"

    @pytest.mark.asyncio
    async def test_tool_result_from_user_message(self):
        """UserMessage with tool_use_result yields ToolResult."""
        from claude_agent_sdk import SystemMessage, UserMessage

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        user_msg = UserMessage(
            content="",
            uuid="u1",
            parent_tool_use_id="tool1",
            tool_use_result={
                "tool_use_id": "tool1",
                "content": "command output",
                "is_error": False,
            },
        )
        result = _mock_result()

        async def fake_response():
            yield user_msg
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.send("run"):
                events.append(event)

        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert len(tool_results) == 1
        assert tool_results[0].output == "command output"


class TestSessionManagerCostTracking:
    @pytest.mark.asyncio
    async def test_cost_accumulates(self):
        from claude_agent_sdk import SystemMessage

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        result1 = _mock_result(cost=0.05, num_turns=1)
        result2 = _mock_result(cost=0.03, num_turns=1)

        call_count = 0

        async def fake_response():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield result1
            else:
                yield result2

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            async for _ in sm.send("first"):
                pass
            async for _ in sm.send("second"):
                pass

            assert sm.cost == pytest.approx(0.08)
            assert sm.turns == 2


class TestSessionManagerOptions:
    def test_builds_options_with_defaults(self):
        sm = SessionManager()
        options = sm._build_options()
        assert options.include_partial_messages is True
        assert options.setting_sources == ["project", "local"]

    def test_builds_options_with_resume(self):
        sm = SessionManager(session_id="resume-me")
        options = sm._build_options()
        assert options.resume == "resume-me"
        assert options.fork_session is not True

    def test_builds_options_with_fork(self):
        sm = SessionManager(session_id="fork-me", fork=True)
        options = sm._build_options()
        assert options.resume == "fork-me"
        assert options.fork_session is True

    def test_builds_options_with_model(self):
        config = ManagerConfig(model="sonnet")
        sm = SessionManager(config=config)
        options = sm._build_options()
        assert options.model == "sonnet"

    def test_builds_options_with_budget(self):
        config = ManagerConfig(max_budget_usd=5.0)
        sm = SessionManager(config=config)
        options = sm._build_options()
        assert options.max_budget_usd == 5.0


class TestSlashCommands:
    @pytest.mark.asyncio
    async def test_compact_sends_slash_command(self):
        """compact() sends /compact through the client."""
        from claude_agent_sdk import SystemMessage

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        compact_msg = SystemMessage(subtype="compact", data={"trigger": "manual"})
        result = _mock_result(session_id="s1")

        async def fake_response():
            yield compact_msg
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.compact():
                events.append(event)

        client.query.assert_awaited_once_with("/compact")
        compact_events = [e for e in events if isinstance(e, CompactComplete)]
        assert len(compact_events) == 1
        assert compact_events[0].trigger == "manual"

    @pytest.mark.asyncio
    async def test_command_sends_arbitrary_slash_command(self):
        """command() forwards any slash command to send()."""
        from claude_agent_sdk import SystemMessage

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        result = _mock_result(session_id="s1")

        async def fake_response():
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.command("/help"):
                events.append(event)

        client.query.assert_awaited_once_with("/help")
        assert isinstance(events[-1], TurnComplete)

    @pytest.mark.asyncio
    async def test_compact_complete_event_from_system_message(self):
        """SystemMessage with subtype 'compact' yields CompactComplete."""
        from claude_agent_sdk import SystemMessage

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        compact_msg = SystemMessage(subtype="compact", data={"trigger": "auto"})
        result = _mock_result(session_id="s1")

        async def fake_response():
            yield compact_msg
            yield result

        client = _make_mock_client([init_msg], fake_response)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()

            events = []
            async for event in sm.send("test"):
                events.append(event)

        compact_events = [e for e in events if isinstance(e, CompactComplete)]
        assert len(compact_events) == 1
        assert compact_events[0].trigger == "auto"
