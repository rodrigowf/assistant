"""Tests for manager/qwen_session.py — mocked subprocess, no real qwen CLI.

The Qwen session manager is fundamentally different from Claude's:
- Each ``send()`` spawns a fresh ``qwen`` subprocess (one-shot per turn).
- ``--resume <session_id>`` is what makes multi-turn work.
- Output is stream-json on stdout, parsed line-by-line.
- No persistent SDK client — the lifecycle task only marks IDLE and waits.

These tests mock ``asyncio.create_subprocess_exec`` to feed canned
stream-json output and verify the event translation.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manager.config import ManagerConfig
from manager.qwen.session import (
    QwenAbandoned,
    QwenSessionManager,
    _qwen_executable,
)
from manager.types import (
    SessionStatus,
    TextDelta,
    TextComplete,
    ThinkingDelta,
    ThinkingComplete,
    ToolResult,
    ToolUse,
    TurnComplete,
)


# ---------------------------------------------------------------------------
# Helpers — build a fake asyncio.subprocess.Process
# ---------------------------------------------------------------------------

class _FakeStream:
    """Minimal asyncio StreamReader stand-in driven by a list of byte lines.

    Returns lines one-by-one from ``readline()``; once the buffer is exhausted
    it returns ``b""`` to signal EOF.
    """

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStdin:
    """Stand-in for ``proc.stdin``; we don't actually consume what's written."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _make_fake_proc(
    stdout_lines: list[dict | bytes],
    stderr_lines: list[bytes] | None = None,
    returncode: int = 0,
):
    """Build a MagicMock ``asyncio.subprocess.Process`` that emits the given
    stdout events as stream-json lines, then EOFs."""
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None  # mutated by wait()

    encoded: list[bytes] = []
    for item in stdout_lines:
        if isinstance(item, bytes):
            encoded.append(item)
        else:
            encoded.append(json.dumps(item).encode("utf-8") + b"\n")

    proc.stdout = _FakeStream(encoded)
    proc.stderr = _FakeStream(stderr_lines or [])
    proc.stdin = _FakeStdin()

    async def _wait():
        proc.returncode = returncode
        return returncode

    proc.wait = AsyncMock(side_effect=_wait)
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    return proc


def _stream_event(evt_type: str, **kwargs) -> dict:
    """Build a Qwen ``stream_event`` envelope."""
    return {
        "type": "stream_event",
        "uuid": "evt-1",
        "session_id": "sess-1",
        "parent_tool_use_id": None,
        "event": {"type": evt_type, **kwargs},
    }


def _init_event(session_id: str = "sess-1") -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "uuid": session_id,
        "session_id": session_id,
        "cwd": "/tmp",
        "tools": [],
        "model": "qwen3.6-plus",
    }


def _result_event(session_id: str = "sess-1", num_turns: int = 1) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "uuid": "res-1",
        "session_id": session_id,
        "is_error": False,
        "num_turns": num_turns,
        "result": "done",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_returns_local_id(self):
        sm = QwenSessionManager(local_id="my-local")
        sid = await sm.start()
        assert sid == "my-local"
        assert sm.provider_name == "qwen"
        assert sm.status == SessionStatus.IDLE
        await sm.stop()
        assert sm.status == SessionStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_start_records_resume_id_as_provider_session_id(self):
        sm = QwenSessionManager(session_id="resumed-qwen-id", local_id="local")
        await sm.start()
        # Resume id is captured up front so the first send() can use it.
        assert sm.sdk_session_id == "resumed-qwen-id"
        assert sm.is_resumed is True
        await sm.stop()

    @pytest.mark.asyncio
    async def test_double_start_raises(self):
        sm = QwenSessionManager()
        await sm.start()
        with pytest.raises(RuntimeError, match="start"):
            await sm.start()
        await sm.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        sm = QwenSessionManager()
        await sm.start()
        await sm.stop()
        # Second stop should no-op cleanly.
        await sm.stop()
        assert sm.status == SessionStatus.DISCONNECTED


# ---------------------------------------------------------------------------
# send() — event translation
# ---------------------------------------------------------------------------

class TestSendEventTranslation:
    @pytest.mark.asyncio
    async def test_basic_text_response(self):
        """A minimal Qwen interaction: init → text block start/delta/stop →
        assistant message → result. We should emit TextDelta, TextComplete,
        and TurnComplete."""
        proc = _make_fake_proc([
            _init_event(),
            _stream_event(
                "message_start",
                message={"id": "m1", "role": "assistant", "content": []},
            ),
            _stream_event(
                "content_block_start",
                index=0,
                content_block={"type": "text", "text": ""},
            ),
            _stream_event(
                "content_block_delta",
                index=0,
                delta={"type": "text_delta", "text": "Hello"},
            ),
            _stream_event(
                "content_block_delta",
                index=0,
                delta={"type": "text_delta", "text": " there"},
            ),
            _stream_event("content_block_stop", index=0),
            _stream_event("message_stop"),
            _result_event(),
        ])

        sm = QwenSessionManager(local_id="t1")
        await sm.start()
        events = []
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for ev in sm.send("hi"):
                events.append(ev)
        await sm.stop()

        text_deltas = [e for e in events if isinstance(e, TextDelta)]
        text_completes = [e for e in events if isinstance(e, TextComplete)]
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]

        assert [e.text for e in text_deltas] == ["Hello", " there"]
        assert len(text_completes) == 1
        assert text_completes[0].text == "Hello there"
        assert len(turn_completes) == 1
        assert turn_completes[0].session_id == "sess-1"
        assert turn_completes[0].num_turns == 1

    @pytest.mark.asyncio
    async def test_thinking_block_emits_thinking_events(self):
        proc = _make_fake_proc([
            _init_event(),
            _stream_event(
                "content_block_start",
                index=0,
                content_block={"type": "thinking", "thinking": ""},
            ),
            _stream_event(
                "content_block_delta",
                index=0,
                delta={"type": "thinking_delta", "thinking": "let me think"},
            ),
            _stream_event("content_block_stop", index=0),
            _result_event(),
        ])

        sm = QwenSessionManager()
        await sm.start()
        events = []
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for ev in sm.send("hi"):
                events.append(ev)
        await sm.stop()

        thinking_deltas = [e for e in events if isinstance(e, ThinkingDelta)]
        thinking_completes = [e for e in events if isinstance(e, ThinkingComplete)]
        assert [e.text for e in thinking_deltas] == ["let me think"]
        assert len(thinking_completes) == 1
        assert thinking_completes[0].text == "let me think"

    @pytest.mark.asyncio
    async def test_assistant_message_with_tool_use_emits_tooluse_event(self):
        """When Qwen emits an assistant message with a tool_use block, the
        wrapper should emit a ToolUse event with the right id/name/input."""
        proc = _make_fake_proc([
            _init_event(),
            {
                "type": "assistant",
                "uuid": "asst-1",
                "session_id": "sess-1",
                "parent_tool_use_id": None,
                "message": {
                    "id": "m1", "type": "message", "role": "assistant",
                    "content": [
                        {"type": "text", "text": "running tool"},
                        {
                            "type": "tool_use",
                            "id": "call_42",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ],
                },
            },
            _result_event(),
        ])

        sm = QwenSessionManager()
        await sm.start()
        events = []
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for ev in sm.send("run ls"):
                events.append(ev)
        await sm.stop()

        tool_uses = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_uses) == 1
        assert tool_uses[0].tool_use_id == "call_42"
        assert tool_uses[0].tool_name == "Bash"
        assert tool_uses[0].tool_input == {"command": "ls"}

    @pytest.mark.asyncio
    async def test_tool_result_from_user_message(self):
        """Qwen sends tool results as user messages with tool_result blocks
        in the content array."""
        proc = _make_fake_proc([
            _init_event(),
            {
                "type": "user",
                "uuid": "user-tool-result",
                "session_id": "sess-1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_42",
                            "content": "file1\nfile2",
                            "is_error": False,
                        },
                    ],
                },
            },
            _result_event(),
        ])

        sm = QwenSessionManager()
        await sm.start()
        events = []
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for ev in sm.send("hi"):
                events.append(ev)
        await sm.stop()

        results = [e for e in events if isinstance(e, ToolResult)]
        assert len(results) == 1
        assert results[0].tool_use_id == "call_42"
        assert results[0].output == "file1\nfile2"
        assert results[0].is_error is False

    @pytest.mark.asyncio
    async def test_session_id_captured_from_init_event(self):
        """The first init event publishes the session_id; subsequent turns
        should reuse it via --resume."""
        proc = _make_fake_proc([
            _init_event(session_id="freshly-created-id"),
            _result_event(session_id="freshly-created-id"),
        ])

        sm = QwenSessionManager()
        await sm.start()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for _ in sm.send("hi"):
                pass
        assert sm.sdk_session_id == "freshly-created-id"
        await sm.stop()

    @pytest.mark.asyncio
    async def test_unparseable_stdout_lines_are_skipped(self):
        """A garbage line in the middle of valid output shouldn't crash the
        stream — we should just log a warning and keep going."""
        proc = _make_fake_proc([
            _init_event(),
            b"not valid json at all\n",
            _stream_event(
                "content_block_start", index=0,
                content_block={"type": "text", "text": ""},
            ),
            _stream_event(
                "content_block_delta", index=0,
                delta={"type": "text_delta", "text": "ok"},
            ),
            _stream_event("content_block_stop", index=0),
            _result_event(),
        ])

        sm = QwenSessionManager()
        await sm.start()
        events = []
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for ev in sm.send("hi"):
                events.append(ev)
        await sm.stop()
        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

class TestBuildArgv:
    def test_argv_baseline(self):
        sm = QwenSessionManager(config=ManagerConfig(project_dir="/tmp"))
        argv = sm._build_argv()
        # First element is the qwen executable.
        assert argv[0] == _qwen_executable()
        # Required protocol flags
        assert "--input-format" in argv and "stream-json" in argv
        assert "--output-format" in argv
        assert "--include-partial-messages" in argv
        # Approval mode is yolo (our wrapper enforces gating at a higher level).
        assert "--approval-mode" in argv
        assert argv[argv.index("--approval-mode") + 1] == "yolo"

    @pytest.mark.asyncio
    async def test_argv_includes_resume_when_session_id_set(self):
        """``_provider_session_id`` is populated by ``_run_lifecycle`` when
        ``session_id`` is passed to the constructor; argv-build happens after."""
        sm = QwenSessionManager(session_id="prev-id")
        await sm.start()
        argv = sm._build_argv()
        await sm.stop()
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == "prev-id"

    @pytest.mark.asyncio
    async def test_argv_omits_resume_for_fresh_session(self):
        sm = QwenSessionManager()
        await sm.start()
        argv = sm._build_argv()
        await sm.stop()
        assert "--resume" not in argv

    def test_argv_includes_model_override(self):
        sm = QwenSessionManager(config=ManagerConfig(model="qwen3-coder-plus"))
        argv = sm._build_argv()
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "qwen3-coder-plus"

    def test_argv_includes_max_turns(self):
        sm = QwenSessionManager(config=ManagerConfig(max_turns=20))
        argv = sm._build_argv()
        assert "--max-session-turns" in argv
        assert argv[argv.index("--max-session-turns") + 1] == "20"


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

class TestRenderPrompt:
    def test_renders_as_stream_json_line(self):
        rendered = QwenSessionManager._render_prompt("Hello")
        assert rendered.endswith("\n")
        obj = json.loads(rendered.strip())
        assert obj == {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello"}],
            },
        }

    def test_handles_special_characters(self):
        rendered = QwenSessionManager._render_prompt('Quotes "and"\nnewlines')
        obj = json.loads(rendered.strip())
        assert obj["message"]["content"][0]["text"] == 'Quotes "and"\nnewlines'


# ---------------------------------------------------------------------------
# Per-turn PID tracking callbacks
# ---------------------------------------------------------------------------

class TestPidCallbacks:
    @pytest.mark.asyncio
    async def test_callbacks_fire_around_turn(self):
        """Pool-installed callbacks fire when a subprocess spawns and exits.

        Qwen spawns a fresh process per turn, so the pool needs spawn/exit
        notifications to keep its orphan-reaper bookkeeping in sync.
        """
        proc = _make_fake_proc([
            _init_event(),
            _result_event(),
        ])

        sm = QwenSessionManager()
        await sm.start()

        spawned: list[int] = []
        exited: list[int] = []
        sm.set_pid_callbacks(spawned.append, exited.append)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for _ in sm.send("hi"):
                pass

        assert spawned == [proc.pid]
        assert exited == [proc.pid]
        await sm.stop()

    @pytest.mark.asyncio
    async def test_callback_exceptions_dont_break_turn(self):
        """A misbehaving callback must NOT take down the in-flight turn.

        The session logs and keeps going — the pool's tracking might be
        stale but the user's message still gets through.
        """
        proc = _make_fake_proc([
            _init_event(),
            _stream_event(
                "content_block_start", index=0,
                content_block={"type": "text", "text": ""},
            ),
            _stream_event(
                "content_block_delta", index=0,
                delta={"type": "text_delta", "text": "ok"},
            ),
            _stream_event("content_block_stop", index=0),
            _result_event(),
        ])

        def explode(_pid: int) -> None:
            raise RuntimeError("callback boom")

        sm = QwenSessionManager()
        await sm.start()
        sm.set_pid_callbacks(explode, explode)

        events = []
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            async for ev in sm.send("hi"):
                events.append(ev)
        await sm.stop()

        # The turn still completed despite the callback raising both times.
        assert any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# Interrupt
# ---------------------------------------------------------------------------

class TestInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_idle_session_is_noop(self):
        """Interrupting when no subprocess is running shouldn't crash."""
        sm = QwenSessionManager()
        await sm.start()
        await sm.interrupt()
        assert sm.status == SessionStatus.INTERRUPTED
        await sm.stop()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_send_before_start_raises(self):
        sm = QwenSessionManager()
        # Not started → status is DISCONNECTED.
        with pytest.raises(RuntimeError, match="not connected"):
            async for _ in sm.send("hi"):
                pass

    @pytest.mark.asyncio
    async def test_qwen_cli_missing_raises_helpful_error(self):
        sm = QwenSessionManager()
        await sm.start()

        # Simulate the qwen binary not being on $PATH.
        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("no qwen here")),
        ):
            with pytest.raises(RuntimeError, match="Executable not found"):
                async for _ in sm.send("hi"):
                    pass
        # Status returned to IDLE so the session is reusable once the CLI
        # is installed.
        assert sm.status == SessionStatus.IDLE
        await sm.stop()


# ---------------------------------------------------------------------------
# QwenAbandoned watchdog
# ---------------------------------------------------------------------------

class TestAbandonedWatchdog:
    @pytest.mark.asyncio
    async def test_abandoned_when_no_events_received(self):
        """If the subprocess produces zero events for _TURN_ABANDON_S, raise
        QwenAbandoned. We monkey-patch the threshold tiny for the test."""
        from manager.qwen import session as qs

        original_abandon = qs._TURN_ABANDON_S
        original_first = qs._STALL_FIRST_NOTICE_S
        qs._TURN_ABANDON_S = 0.2
        qs._STALL_FIRST_NOTICE_S = 0.1

        # A process that yields no lines, then EOF after a long wait.
        async def _slow_readline():
            await asyncio.sleep(5)
            return b""

        proc = MagicMock()
        proc.pid = 999
        proc.returncode = None
        proc.stdout = MagicMock()
        proc.stdout.readline = _slow_readline
        proc.stderr = _FakeStream([])
        proc.stdin = _FakeStdin()
        proc.wait = AsyncMock(return_value=0)
        proc.send_signal = MagicMock()
        proc.kill = MagicMock()

        sm = QwenSessionManager()
        await sm.start()
        try:
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                with pytest.raises(QwenAbandoned) as excinfo:
                    async for _ in sm.send("hi"):
                        pass
            assert excinfo.value.elapsed_seconds >= 0.2
        finally:
            qs._TURN_ABANDON_S = original_abandon
            qs._STALL_FIRST_NOTICE_S = original_first
            await sm.stop()
