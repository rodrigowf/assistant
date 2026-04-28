"""Tests for manager/session.py — mocked SDK, no real Claude Code."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manager.config import ManagerConfig
from manager.session import SessionManager
from manager.types import (
    CompactComplete,
    SessionStalled,
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
    client.interrupt = AsyncMock()

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
    async def test_start_returns_local_id(self):
        """start() returns the stable local_id, not the SDK session ID."""
        client = _make_mock_client(server_info={"session_id": "abc-123"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager(local_id="my-local-id")
            sid = await sm.start()

            # Returns the local_id (stable identifier)
            assert sid == "my-local-id"
            assert sm.local_id == "my-local-id"
            assert sm.session_id == "my-local-id"  # alias for local_id
            # SDK session ID captured from server_info
            assert sm.sdk_session_id == "abc-123"
            assert sm.status == SessionStatus.IDLE

    @pytest.mark.asyncio
    async def test_start_generates_local_id(self):
        """If no local_id is provided, one is generated."""
        client = _make_mock_client(server_info={"session_id": "sdk-abc"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            sid = await sm.start()

            # A UUID was generated as local_id
            assert sid == sm.local_id
            assert len(sid) > 0
            assert sm.sdk_session_id == "sdk-abc"

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
            async with SessionManager(local_id="ctx-local") as sm:
                assert sm.local_id == "ctx-local"
                assert sm.sdk_session_id == "ctx"

            client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_stores_sdk_session_id(self):
        """When resuming, the SDK session ID is stored separately from local_id."""
        client = _make_mock_client(server_info={"commands": [], "output_style": "plain"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager(session_id="existing-session-abc", local_id="local-xyz")
            sid = await sm.start()

            # Returns local_id
            assert sid == "local-xyz"
            assert sm.local_id == "local-xyz"
            # SDK session ID captured from the resume_id
            assert sm.sdk_session_id == "existing-session-abc"

    @pytest.mark.asyncio
    async def test_stop_from_different_task_succeeds(self):
        """Regression: stop() must work even when called from a different
        asyncio task than start(). This used to fail because the SDK's
        internal anyio task group was entered in the start-task and trying
        to exit it from another task raised:
          RuntimeError: Attempted to exit cancel scope in a different task
        On the Jetson (2026-04-26) this leak pinned the event loop in a
        spin and accumulated 6h+ of CPU on the backend in 16h of uptime.
        The fix: a lifecycle task owns both connect() and disconnect().
        """
        client = _make_mock_client(server_info={"session_id": "x"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager(local_id="cross-task")
            # Start from this task.
            await sm.start()
            assert sm.status == SessionStatus.IDLE

            # Stop from a different task — this is what FastAPI request
            # handlers do (each HTTP request runs in its own task).
            stop_task = asyncio.create_task(sm.stop())
            await stop_task  # must not raise

            assert sm.status == SessionStatus.DISCONNECTED
            client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_double_start_raises(self):
        """Calling start() twice on the same SessionManager is a programming
        error and should raise rather than silently leak a second lifecycle
        task."""
        client = _make_mock_client(server_info={"session_id": "x"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()
            with pytest.raises(RuntimeError, match="called twice"):
                await sm.start()
            await sm.stop()

    @pytest.mark.asyncio
    async def test_double_stop_is_noop(self):
        """Calling stop() twice should be safe — the second call is a no-op
        rather than awaiting a dead task or raising."""
        client = _make_mock_client(server_info={"session_id": "x"})

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()
            await sm.stop()
            await sm.stop()  # must not raise

            assert sm.status == SessionStatus.DISCONNECTED
            # disconnect() only called once — second stop() short-circuited
            client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_failure_propagates_to_start_caller(self):
        """If client.connect() fails, start() must raise the underlying
        exception to its caller — not swallow it inside the lifecycle task."""
        client = _make_mock_client(server_info={"session_id": "x"})
        client.connect.side_effect = ConnectionRefusedError("nope")

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            with pytest.raises(ConnectionRefusedError, match="nope"):
                await sm.start()

            # And stop() afterwards is a no-op (lifecycle never entered the
            # idle-wait phase).
            await sm.stop()

    @pytest.mark.asyncio
    async def test_subprocess_pid_captured_at_connect(self):
        """The bundled-claude subprocess pid (from the SDK's private
        transport._process.pid) must be captured at connect time so the
        kill fallback in stop() has something to signal."""
        client = _make_mock_client(server_info={"session_id": "x"})
        # Simulate the SDK's private structure
        client._transport = MagicMock()
        client._transport._process = MagicMock()
        client._transport._process.pid = 99999  # arbitrary non-real pid

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager()
            await sm.start()
            assert sm.subprocess_pid == 99999
            await sm.stop()
            # After stop the captured pid should be cleared
            assert sm.subprocess_pid is None

    @pytest.mark.asyncio
    async def test_subprocess_pid_none_when_sdk_shape_changes(self):
        """If a future SDK refactor moves the private attribute, we should
        log a debug and continue — NOT crash the session.  The pool's
        orphan reaper still acts as a fallback."""
        client = _make_mock_client(server_info={"session_id": "x"})

        with patch("manager.session.ClaudeSDKClient", return_value=client), \
             patch("manager.session._extract_subprocess_pid", return_value=None):
            sm = SessionManager()
            await sm.start()
            assert sm.subprocess_pid is None
            await sm.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_disconnect_timeout_triggers_subprocess_kill(self):
        """If client.disconnect() exceeds 8s (because the SDK's transport
        sits in `await self._process.wait()` after a SIGTERM the bundled
        claude is ignoring), the lifecycle finally must escalate to
        kill_claude_subprocess() so we don't leak."""
        client = _make_mock_client(server_info={"session_id": "x"})

        # Make disconnect() hang forever (simulating the real SDK bug)
        async def _hang():
            await asyncio.sleep(60)
        client.disconnect = AsyncMock(side_effect=_hang)

        client._transport = MagicMock()
        client._transport._process = MagicMock()
        client._transport._process.pid = 88888

        with patch("manager.session.ClaudeSDKClient", return_value=client), \
             patch("manager.session._process_alive", return_value=True), \
             patch("manager.session.kill_claude_subprocess", return_value=True) as kill_mock, \
             patch("manager.session.asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            sm = SessionManager()
            await sm.start()
            await sm.stop()

            # The kill fallback must have been invoked with the pid
            kill_mock.assert_called_once_with(88888)
            assert sm.status == SessionStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_caller_cancel_does_not_cancel_lifecycle(self):
        """Regression for the 2026-04-26 hot-spin: when our caller wraps
        ``stop()`` in a tight ``asyncio.wait_for`` and times out, the
        cancellation must NOT propagate into the lifecycle task and
        cancel the in-flight ``client.disconnect()``.  If it does, the
        SDK's anyio task group fails to exit cleanly, leaving a
        cancelled-but-never-awaited ``_read_messages`` task that pins
        the event loop at ~98% CPU forever.

        Verifies: lifecycle task is shielded from caller cancellation
        and its disconnect + cleanup still complete in the background.
        """
        client = _make_mock_client(server_info={"session_id": "x"})

        # disconnect() sleeps for 1.5s — longer than our caller's wait
        disconnect_started = asyncio.Event()
        disconnect_finished = asyncio.Event()

        async def _slow_disconnect():
            disconnect_started.set()
            try:
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                # If this fires, the bug is back: shield isn't protecting
                # the lifecycle task.  Mark for the assertion below.
                disconnect_finished.set()  # signals it was cancelled
                raise
            disconnect_finished.set()

        client.disconnect = AsyncMock(side_effect=_slow_disconnect)

        with patch("manager.session.ClaudeSDKClient", return_value=client):
            sm = SessionManager(local_id="shielded")
            await sm.start()

            # Caller wraps stop() in a tight 0.3s timeout — way under
            # disconnect's 1.5s.  This cancels stop(), which without
            # shield() would also cancel the lifecycle's disconnect.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(sm.stop(), timeout=0.3)

            # disconnect() actually started inside the lifecycle
            assert disconnect_started.is_set()

            # Now wait long enough for the lifecycle to finish naturally
            # (it should — shield protected it from our cancel).
            await asyncio.sleep(2.0)

            # Verify the lifecycle's disconnect ran to completion, not
            # cancelled.  The mock's call count proves it was awaited.
            client.disconnect.assert_called_once()
            assert disconnect_finished.is_set()
            # And the session is properly torn down
            assert sm.status == SessionStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_clean_disconnect_does_not_kill(self):
        """If client.disconnect() returns cleanly AND the subprocess exits
        on its own, kill_claude_subprocess must NOT be called.  Steady-
        state cleanup should be silent."""
        client = _make_mock_client(server_info={"session_id": "x"})
        client._transport = MagicMock()
        client._transport._process = MagicMock()
        client._transport._process.pid = 77777

        with patch("manager.session.ClaudeSDKClient", return_value=client), \
             patch("manager.session._process_alive", return_value=False), \
             patch("manager.session.kill_claude_subprocess") as kill_mock:
            sm = SessionManager()
            await sm.start()
            await sm.stop()

            # Process is gone (mocked _process_alive=False) → no kill
            kill_mock.assert_not_called()


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

    @pytest.mark.asyncio
    async def test_tool_result_from_user_message_string_content(self):
        """UserMessage with a string tool_use_result still yields ToolResult.

        Regression: some claude-cli versions hand the bundled web tools'
        output back as a plain string rather than the documented dict.  The
        old code logged a warning and dropped the message, leaving the UI
        spinning forever.  Now we preserve the string as the tool output
        and recover the tool_use_id from parent_tool_use_id.
        """
        from claude_agent_sdk import SystemMessage, UserMessage

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        user_msg = UserMessage(
            content="",
            uuid="u1",
            parent_tool_use_id="tool_xyz",
            tool_use_result="raw stdout from web tool",
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
        assert tool_results[0].output == "raw stdout from web tool"
        assert tool_results[0].tool_use_id == "tool_xyz"
        assert tool_results[0].is_error is False


class TestSessionManagerStallWatchdog:
    @pytest.mark.asyncio
    async def test_stall_event_emitted_when_sdk_goes_silent(self):
        """SessionStalled is yielded when receive_response stalls past the
        first-notice threshold; the underlying stream is not aborted.

        The SDK fixture below blocks indefinitely between a ToolUse and
        the eventual ResultMessage.  We monkey-patch the threshold down
        so the test runs in milliseconds rather than minutes.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            SystemMessage,
            ToolUseBlock,
        )
        import manager.session as session_mod

        init_msg = SystemMessage(subtype="init", data={"session_id": "s1"})
        tool_use_msg = AssistantMessage(
            model="claude-test",
            content=[
                ToolUseBlock(
                    id="toolu_stuck", name="WebFetch",
                    input={"url": "https://hung.example.com"},
                ),
            ],
            parent_tool_use_id=None,
        )
        result = _mock_result()

        # Tiny thresholds so the watchdog fires in test time.
        original_first = session_mod._STALL_FIRST_NOTICE_S
        original_repeat = session_mod._STALL_REPEAT_INTERVAL_S
        session_mod._STALL_FIRST_NOTICE_S = 0.1
        session_mod._STALL_REPEAT_INTERVAL_S = 0.1

        try:
            unblock = asyncio.Event()

            async def fake_response():
                yield tool_use_msg
                # Hold the stream open with no events — this is the stall.
                # The test will set unblock once it has observed a stall.
                await unblock.wait()
                yield result

            client = _make_mock_client([init_msg], fake_response)

            with patch("manager.session.ClaudeSDKClient", return_value=client):
                sm = SessionManager()
                await sm.start()

                stall_events: list[SessionStalled] = []
                tool_uses: list[ToolUse] = []
                turn_completes: list[TurnComplete] = []

                async for event in sm.send("research"):
                    if isinstance(event, ToolUse):
                        tool_uses.append(event)
                    elif isinstance(event, SessionStalled):
                        stall_events.append(event)
                        # Once we have at least one stall notice, unblock
                        # the SDK so the turn can finish and the loop exits.
                        if not unblock.is_set():
                            unblock.set()
                    elif isinstance(event, TurnComplete):
                        turn_completes.append(event)
        finally:
            session_mod._STALL_FIRST_NOTICE_S = original_first
            session_mod._STALL_REPEAT_INTERVAL_S = original_repeat

        # We saw the in-flight tool, then at least one stall notice
        # naming it, then the eventual TurnComplete.
        assert len(tool_uses) == 1
        assert len(stall_events) >= 1
        assert stall_events[0].last_tool_name == "WebFetch"
        assert stall_events[0].last_tool_use_id == "toolu_stuck"
        assert stall_events[0].elapsed_seconds > 0
        assert len(turn_completes) == 1


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
