"""Tests for manager/gemini/session.py — mocked subprocess, no real gemini CLI.

The Gemini session manager is shaped like Qwen's: each ``send()`` spawns
a fresh subprocess (one-shot per turn) and parses stream-json on stdout.
These tests mock ``asyncio.create_subprocess_exec`` to feed canned
stream-json output and verify the event translation.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manager.config import ManagerConfig
from manager.gemini.session import (
    GeminiAbandoned,
    GeminiSessionManager,
    _gemini_executable,
)
from manager.types import (
    SessionStatus,
    TextComplete,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)


# ---------------------------------------------------------------------------
# Fake subprocess helpers — same shape as test_qwen_session.py
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _make_fake_proc(
    stdout_lines: list[dict | bytes],
    stderr_lines: list[bytes] | None = None,
    returncode: int = 0,
):
    """Build a MagicMock asyncio.subprocess.Process emitting the given
    stream-json lines, then EOFing."""
    proc = MagicMock()
    proc.pid = 54321
    proc.returncode = None

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


# Build a couple of canonical stream-json events.

def _init_event(session_id: str = "11111111-1111-1111-1111-111111111111") -> dict:
    return {
        "type": "init",
        "timestamp": "2026-05-15T20:00:00Z",
        "session_id": session_id,
        "model": "gemini-3-flash-preview",
    }


def _user_echo_event(content: str = "hi") -> dict:
    return {
        "type": "message",
        "timestamp": "2026-05-15T20:00:00Z",
        "role": "user",
        "content": content,
    }


def _assistant_delta_event(content: str) -> dict:
    return {
        "type": "message",
        "timestamp": "2026-05-15T20:00:01Z",
        "role": "assistant",
        "content": content,
        "delta": True,
    }


def _tool_use_event(name: str, tid: str, params: dict) -> dict:
    return {
        "type": "tool_use",
        "timestamp": "2026-05-15T20:00:02Z",
        "tool_name": name,
        "tool_id": tid,
        "parameters": params,
    }


def _tool_result_event(tid: str, output: str, error: bool = False) -> dict:
    if error:
        return {
            "type": "tool_result",
            "timestamp": "2026-05-15T20:00:03Z",
            "tool_id": tid,
            "status": "error",
            "error": {"type": "x", "message": output},
        }
    return {
        "type": "tool_result",
        "timestamp": "2026-05-15T20:00:03Z",
        "tool_id": tid,
        "status": "success",
        "output": output,
    }


def _result_event(status: str = "success") -> dict:
    return {
        "type": "result",
        "timestamp": "2026-05-15T20:00:04Z",
        "status": status,
        "stats": {
            "total_tokens": 100, "input_tokens": 50, "output_tokens": 10, "cached": 0,
        },
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_returns_local_id_and_generates_session_id(self):
        sm = GeminiSessionManager(local_id="my-local")
        sid = await sm.start()
        assert sid == "my-local"
        assert sm.provider_name == "gemini"
        assert sm.status == SessionStatus.IDLE
        # Fresh session — manager generates a UUID up front so the very
        # first send() can pin it via --session-id.
        assert sm.sdk_session_id is not None
        uuid.UUID(sm.sdk_session_id)  # well-formed UUID; raises if not
        await sm.stop()
        assert sm.status == SessionStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_start_records_resume_id_as_provider_session_id(self):
        sm = GeminiSessionManager(
            session_id="resume-gemini-id", local_id="local",
        )
        await sm.start()
        assert sm.sdk_session_id == "resume-gemini-id"
        assert sm.is_resumed is True
        await sm.stop()

    @pytest.mark.asyncio
    async def test_double_start_raises(self):
        sm = GeminiSessionManager()
        await sm.start()
        with pytest.raises(RuntimeError, match="start"):
            await sm.start()
        await sm.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        sm = GeminiSessionManager()
        await sm.start()
        await sm.stop()
        await sm.stop()
        assert sm.status == SessionStatus.DISCONNECTED


# ---------------------------------------------------------------------------
# Argv construction
# ---------------------------------------------------------------------------


class TestArgvConstruction:
    @pytest.mark.asyncio
    async def test_argv_pins_session_id_on_first_turn(self):
        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        argv = sm._build_argv("hello world")
        # Prompt is on argv (not stdin) per the CLI's --prompt semantic.
        assert "--prompt" in argv
        assert "hello world" in argv
        # First turn pins the id with --session-id, NOT --resume.
        assert "--session-id" in argv
        assert "--resume" not in argv
        # Always pass --skip-trust for headless mode.
        assert "--skip-trust" in argv
        # Stream-json output for parsing.
        assert "--output-format" in argv
        assert "stream-json" in argv
        await sm.stop()

    @pytest.mark.asyncio
    async def test_argv_uses_resume_after_first_turn(self):
        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        # Simulate one completed turn.
        sm._turns = 1
        argv = sm._build_argv("second prompt")
        assert "--resume" in argv
        assert "--session-id" not in argv
        await sm.stop()

    @pytest.mark.asyncio
    async def test_argv_passes_model_when_configured(self):
        cfg = ManagerConfig(model="gemini-3-pro-preview")
        sm = GeminiSessionManager(config=cfg, local_id="local")
        await sm.start()
        argv = sm._build_argv("x")
        i = argv.index("--model")
        assert argv[i + 1] == "gemini-3-pro-preview"
        await sm.stop()

    @pytest.mark.asyncio
    async def test_env_sets_trust_workspace_belt_and_suspenders(self):
        sm = GeminiSessionManager()
        await sm.start()
        env = sm._build_env()
        # Even though we pass --skip-trust on argv, also set the env var
        # so a future CLI rename of the flag doesn't break us silently.
        assert env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"
        # CLAUDECODE marker is stripped (same as Qwen does) so the CLI
        # doesn't think it's nested inside Claude.
        assert "CLAUDECODE" not in env
        await sm.stop()


# ---------------------------------------------------------------------------
# send() — event translation
# ---------------------------------------------------------------------------


class TestSendEventTranslation:
    @pytest.mark.asyncio
    async def test_simple_text_turn_emits_delta_complete_and_turn_complete(self):
        proc = _make_fake_proc([
            _init_event(),
            _user_echo_event("hi"),
            _assistant_delta_event("Hel"),
            _assistant_delta_event("lo!"),
            _result_event(),
        ])

        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        with patch(
            "manager.gemini.session.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            events = [ev async for ev in sm.send("hi")]
        await sm.stop()

        # Should see two TextDelta chunks, then a TextComplete with the
        # concatenated text, then a TurnComplete.
        deltas = [e for e in events if isinstance(e, TextDelta)]
        completes = [e for e in events if isinstance(e, TextComplete)]
        turns = [e for e in events if isinstance(e, TurnComplete)]

        assert [d.text for d in deltas] == ["Hel", "lo!"]
        assert len(completes) == 1
        assert completes[0].text == "Hello!"
        assert len(turns) == 1
        # Stats are surfaced via the usage dict on TurnComplete.
        assert turns[0].usage.get("total_tokens") == 100

    @pytest.mark.asyncio
    async def test_tool_use_and_tool_result_are_translated(self):
        proc = _make_fake_proc([
            _init_event(),
            _user_echo_event("read"),
            _tool_use_event("read_file", "tid1", {"file_path": "/etc/hosts"}),
            _tool_result_event("tid1", "127.0.0.1 localhost"),
            _assistant_delta_event("Done."),
            _result_event(),
        ])

        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        with patch(
            "manager.gemini.session.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            events = [ev async for ev in sm.send("read")]
        await sm.stop()

        tool_uses = [e for e in events if isinstance(e, ToolUse)]
        tool_results = [e for e in events if isinstance(e, ToolResult)]

        assert len(tool_uses) == 1
        assert tool_uses[0].tool_name == "read_file"
        assert tool_uses[0].tool_use_id == "tid1"
        assert tool_uses[0].tool_input == {"file_path": "/etc/hosts"}

        assert len(tool_results) == 1
        assert tool_results[0].tool_use_id == "tid1"
        assert tool_results[0].output == "127.0.0.1 localhost"
        assert tool_results[0].is_error is False

    @pytest.mark.asyncio
    async def test_tool_result_error_maps_to_is_error_true(self):
        proc = _make_fake_proc([
            _init_event(),
            _tool_use_event("read_file", "tid1", {"file_path": "/missing"}),
            _tool_result_event("tid1", "No such file", error=True),
            _result_event(),
        ])

        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        with patch(
            "manager.gemini.session.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            events = [ev async for ev in sm.send("read missing")]
        await sm.stop()

        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is True
        assert "No such file" in tool_results[0].output

    @pytest.mark.asyncio
    async def test_user_echo_message_is_filtered_out(self):
        """The CLI echoes the user prompt back on stream-json — we already
        broadcast it via the WS layer, so the session manager must NOT
        re-emit it as a TextDelta."""
        proc = _make_fake_proc([
            _init_event(),
            _user_echo_event("my prompt"),
            _assistant_delta_event("Ack."),
            _result_event(),
        ])

        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        with patch(
            "manager.gemini.session.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            events = [ev async for ev in sm.send("my prompt")]
        await sm.stop()

        deltas = [e for e in events if isinstance(e, TextDelta)]
        assert all("my prompt" not in d.text for d in deltas)

    @pytest.mark.asyncio
    async def test_init_event_captures_session_id_if_not_yet_pinned(self):
        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        # Force the pinned id back to None to mimic a session that didn't
        # generate one up-front (defensive: future code paths may skip
        # the lifecycle pre-generation).
        sm._provider_session_id = None

        proc = _make_fake_proc([
            _init_event(session_id="learned-from-init"),
            _result_event(),
        ])
        with patch(
            "manager.gemini.session.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            _ = [ev async for ev in sm.send("hi")]
        await sm.stop()

        assert sm.sdk_session_id == "learned-from-init"

    @pytest.mark.asyncio
    async def test_non_json_stdout_lines_are_skipped(self):
        """The CLI prints "Shell cwd was reset to..." on stdout in some
        builds.  Non-JSON lines must not crash the parser."""
        proc = _make_fake_proc([
            _init_event(),
            b"Shell cwd was reset to /home/rodrigo/assistant\n",
            _assistant_delta_event("ok"),
            _result_event(),
        ])

        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        with patch(
            "manager.gemini.session.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            events = [ev async for ev in sm.send("hi")]
        await sm.stop()

        # Test passed if we got TurnComplete (didn't crash).
        assert any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# Abandoned-turn watchdog
# ---------------------------------------------------------------------------


class TestAbandonedWatchdog:
    @pytest.mark.asyncio
    async def test_abandoned_when_no_events_received(self):
        """If the subprocess produces zero events for _TURN_ABANDON_S, raise
        GeminiAbandoned. We monkey-patch the threshold tiny for the test."""
        from manager.gemini import session as gs

        original_abandon = gs._TURN_ABANDON_S
        original_first = gs._STALL_FIRST_NOTICE_S
        gs._TURN_ABANDON_S = 0.2
        gs._STALL_FIRST_NOTICE_S = 0.1

        # A subprocess that hangs forever with no output.
        async def _slow_readline():
            await asyncio.sleep(5)
            return b""

        proc = MagicMock()
        proc.pid = 99999
        proc.returncode = None
        proc.stdout = MagicMock()
        proc.stdout.readline = _slow_readline
        proc.stderr = _FakeStream([])
        proc.stdin = _FakeStdin()
        proc.wait = AsyncMock(return_value=0)
        proc.send_signal = MagicMock()
        proc.kill = MagicMock()

        sm = GeminiSessionManager(local_id="local")
        await sm.start()
        try:
            with patch(
                "manager.gemini.session.asyncio.create_subprocess_exec",
                return_value=proc,
            ):
                with pytest.raises(GeminiAbandoned):
                    async for _ in sm.send("hi"):
                        pass
        finally:
            gs._TURN_ABANDON_S = original_abandon
            gs._STALL_FIRST_NOTICE_S = original_first
            await sm.stop()


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------


def test_gemini_executable_default():
    assert _gemini_executable() == "gemini"


def test_gemini_executable_honors_env(monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_PATH", "/opt/gemini/bin/gemini")
    assert _gemini_executable() == "/opt/gemini/bin/gemini"
