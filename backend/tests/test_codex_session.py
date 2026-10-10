"""Tests for manager/codex/session.py against a scripted fake app-server.

``fixtures/codex/fake_app_server.py`` speaks the JSON-RPC subset the
harness uses (shapes copied from a real CLI 0.161 transcript) and logs
every request it receives, so these tests drive the real subprocess /
stdio / receive-loop path and then assert on what was sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from manager.base_session import SessionDeadError
from manager.config import ManagerConfig
from manager.codex.session import CodexSessionManager
from manager.types import (
    SessionStatus,
    TerminationReason,
    TextComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)

FAKE = Path(__file__).parent / "fixtures" / "codex" / "fake_app_server.py"
THREAD = "01a118ca-0000-7000-8000-000000000001"


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """Point CODEX_CLI_PATH at the fake server; return the request log path."""
    script = tmp_path / "codex"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    script.chmod(0o755)
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("CODEX_CLI_PATH", str(script))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setenv("ARCHIE_CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    return log


def _requests(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _req(log: Path, method: str) -> list[dict]:
    return [r.get("params") or {} for r in _requests(log) if r.get("method") == method]


def _config(tmp_path, **kw) -> ManagerConfig:
    return ManagerConfig(project_dir=str(tmp_path), provider="codex", **kw)


async def _collect(sm, prompt):
    return [ev async for ev in sm.send(prompt)]


async def test_start_handshake_and_thread_params(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        assert sm.sdk_session_id == THREAD
        assert sm.status == SessionStatus.IDLE
        assert sm.subprocess_pid is not None
        lines = _requests(fake_codex)
        head = lines[0]
        assert head["argv"][:3] == ["app-server", "--listen", "stdio://"]
        assert "project_doc_max_bytes=131072" in head["argv"]
        assert head["argv"].count("--disable") == 3 and {"plugins", "apps", "memories"} <= set(head["argv"])
        assert head["env_home"] == str(tmp_path / "codex-home")
        assert head["has_openai_key"] is False  # never bill API credits by accident
        init = _req(fake_codex, "initialize")[0]
        assert init["clientInfo"]["name"] == "archie"
        start = _req(fake_codex, "thread/start")[0]
        assert start["cwd"] == str(tmp_path)
        assert start["sandbox"] == "danger-full-access"
        assert start["approvalPolicy"] == "never"
        # No model chosen → the account default from model/list, never config.toml's.
        assert start["model"] == "gpt-6-luna"
        assert "config" not in start  # no options → no overrides
    finally:
        await sm.stop()
    assert sm.status == SessionStatus.DISCONNECTED


async def test_streaming_text_and_reasoning(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        events = await _collect(sm, "say hello")
    finally:
        await sm.stop()
    thinking = "".join(e.text for e in events if isinstance(e, ThinkingDelta))
    assert thinking == "**Greeting**\n\nsecond"
    assert [e.text for e in events if isinstance(e, ThinkingComplete)] == ["**Greeting**\n\nsecond"]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).strip() == "Hello there friend"
    assert [e.text for e in events if isinstance(e, TextComplete)] == ["Hello there friend"]
    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert done.session_id == THREAD and done.is_error is False and done.cost is None
    assert done.result == "Hello there friend"
    assert done.usage["input_tokens"] == 40 and done.usage["cache_read_input_tokens"] == 60
    assert done.usage["output_tokens"] == 20
    # Default reasoning summary is sent so the UI gets thinking.
    turn = _req(fake_codex, "turn/start")[0]
    assert turn["summary"] == "concise"
    assert "effort" not in turn
    assert turn["input"][0] == {"type": "text", "text": "say hello", "text_elements": []}


async def test_command_tool_call(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        events = await _collect(sm, "run ls")
    finally:
        await sm.stop()
    uses = [e for e in events if isinstance(e, ToolUse)]
    results = [e for e in events if isinstance(e, ToolResult)]
    assert uses == [ToolUse(tool_use_id="exec-1", tool_name="Bash", tool_input={"command": "ls -la"})]
    assert results == [ToolResult(tool_use_id="exec-1", output="a.txt\n", is_error=False)]


async def test_file_change_and_plan(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        patch_events = await _collect(sm, "patch it")
        plan_events = await _collect(sm, "make a plan")
    finally:
        await sm.stop()
    use = next(e for e in patch_events if isinstance(e, ToolUse))
    assert use.tool_name == "Edit"
    assert use.tool_input == {"file_path": "/tmp/x.py", "old_string": "a = 1\nb = 2", "new_string": "a = 1\nb = 3"}
    plan = next(e for e in plan_events if isinstance(e, ToolUse))
    assert plan.tool_name == "TodoWrite"
    assert [t["status"] for t in plan.tool_input["todos"]] == ["completed", "in_progress", "pending"]
    assert any(isinstance(e, ToolResult) and e.tool_use_id == plan.tool_use_id for e in plan_events)


async def test_interrupt_mid_turn(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        events = []
        async for ev in sm.send("go slow"):
            events.append(ev)
            if isinstance(ev, TextDelta):
                await sm.interrupt()
        assert _req(fake_codex, "turn/interrupt") == [{"threadId": THREAD, "turnId": "turn-1"}]
        done = events[-1]
        assert isinstance(done, TurnComplete) and done.result == "interrupted" and not done.is_error
        # The open agent message is flushed so the UI has its final text.
        assert any(isinstance(e, TextComplete) and e.text == "1\n2\n" for e in events)
        # The process stays up: the next turn works.
        again = await _collect(sm, "anything")
        assert isinstance(again[-1], TurnComplete) and again[-1].result == "ok"
        assert sm.status == SessionStatus.IDLE
    finally:
        await sm.stop()


async def test_failed_turn_surfaces_error(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        events = await _collect(sm, "fail please")
        bad = await _collect(sm, "turnerr")
    finally:
        await sm.stop()
    done = events[-1]
    assert isinstance(done, TurnComplete) and done.is_error
    assert done.result == "You've hit your usage limit."
    assert any(isinstance(e, TextComplete) and "usage limit" in e.text for e in events)
    assert isinstance(bad[-1], TurnComplete) and bad[-1].is_error and "model not supported" in bad[-1].result


async def test_approval_request_is_auto_accepted(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        events = await _collect(sm, "approve this")
    finally:
        await sm.stop()
    text = next(e.text for e in events if isinstance(e, TextComplete))
    assert text == 'decision {"decision": "accept"}'


async def test_crash_terminates_session(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    try:
        events = await asyncio.wait_for(_collect(sm, "crash now"), timeout=10)
        assert isinstance(events[-1], TurnComplete) and events[-1].is_error
        await asyncio.wait_for(sm._receive_loop_done.wait(), timeout=5)
        assert sm._termination_reason == TerminationReason.SUBPROCESS_CRASHED
        assert "code 3" in (sm._termination_detail or "")
        assert sm.status == SessionStatus.DISCONNECTED
        with pytest.raises(SessionDeadError):
            await _collect(sm, "hello again")
    finally:
        await sm.stop()


async def test_resume_and_fork(fake_codex, tmp_path):
    sm = CodexSessionManager("01a118ca-1111-7000-8000-00000000abcd", config=_config(tmp_path, model="gpt-5.6-terra"))
    await sm.start()
    await sm.stop()
    resume = _req(fake_codex, "thread/resume")[0]
    assert resume["threadId"] == "01a118ca-1111-7000-8000-00000000abcd"
    assert resume["excludeTurns"] is True
    assert resume["model"] == "gpt-5.6-terra"
    assert sm.sdk_session_id == "01a118ca-1111-7000-8000-00000000abcd"

    fork = CodexSessionManager("01a118ca-1111-7000-8000-00000000abcd", fork=True, config=_config(tmp_path))
    await fork.start()
    await fork.stop()
    assert _req(fake_codex, "thread/fork")[0]["threadId"] == "01a118ca-1111-7000-8000-00000000abcd"
    assert fork.sdk_session_id == THREAD


async def test_memory_index_is_developer_instructions_on_new_threads_only(fake_codex, tmp_path, monkeypatch):
    """Archie's MEMORY.md goes in once, at thread start (manager/memory_context.py);
    a resumed or forked thread already carries it in its history."""
    seen: list = []

    def fake_memory(project_dir):
        seen.append(project_dir)
        return "# Memory\n\n<memory_index>\nX\n</memory_index>"

    monkeypatch.setattr("manager.codex.session.memory_instructions", fake_memory)
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    await sm.stop()
    start = _req(fake_codex, "thread/start")[0]
    assert start["developerInstructions"].startswith("# Memory")
    assert seen == [str(tmp_path)]

    for kw in ({}, {"fork": True}):
        other = CodexSessionManager("01a118ca-1111-7000-8000-00000000abcd", config=_config(tmp_path), **kw)
        await other.start()
        await other.stop()
    assert "developerInstructions" not in _req(fake_codex, "thread/resume")[0]
    assert "developerInstructions" not in _req(fake_codex, "thread/fork")[0]


async def test_no_memory_index_outside_the_repo(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path))
    await sm.start()
    await sm.stop()
    assert "developerInstructions" not in _req(fake_codex, "thread/start")[0]


async def test_resume_unknown_thread_fails_start(fake_codex, tmp_path):
    sm = CodexSessionManager("missing", config=_config(tmp_path))
    with pytest.raises(Exception, match="no rollout found"):
        await sm.start()
    assert sm.subprocess_pid is None


async def test_options_map_to_params(fake_codex, tmp_path):
    opts = {"effort": "high", "reasoning_summary": "detailed", "verbosity": "medium", "web_search": "live"}
    sm = CodexSessionManager(config=_config(tmp_path, harness_options=opts))
    await sm.start()
    try:
        await _collect(sm, "x")
    finally:
        await sm.stop()
    start = _req(fake_codex, "thread/start")[0]
    assert start["config"] == {"model_verbosity": "medium", "web_search": "live"}
    turn = _req(fake_codex, "turn/start")[0]
    assert turn["effort"] == "high" and turn["summary"] == "detailed"


async def test_effort_not_supported_by_model_is_dropped(fake_codex, tmp_path):
    sm = CodexSessionManager(config=_config(tmp_path, harness_options={"effort": "ultra"}))
    await sm.start()  # default model gpt-6-luna: low/medium/high only
    try:
        await _collect(sm, "x")
    finally:
        await sm.stop()
    assert "effort" not in _req(fake_codex, "turn/start")[0]


async def test_missing_binary_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_CLI_PATH", str(tmp_path / "nope" / "codex"))
    sm = CodexSessionManager(config=_config(tmp_path))
    with pytest.raises(RuntimeError, match="npm install -g @openai/codex"):
        await sm.start()


def test_ssh_argv_uses_remote_bootstrap(tmp_path, monkeypatch):
    import manager.codex.session as mod

    monkeypatch.setattr(mod, "resolve_remote_cli_path", lambda cli, target: "/home/u/.nvm/bin/codex")
    sm = CodexSessionManager(config=_config(tmp_path, ssh_host="jetson", ssh_user="u"))
    argv, cwd, env = asyncio.run(sm._spawn_spec())
    assert argv[0] == "ssh" and "u@jetson" in argv
    assert any("ControlPath=/tmp/codex-ssh-jetson-%r" in a for a in argv)
    remote = argv[-1]
    assert remote.startswith(f"cd '{tmp_path}' && ")
    assert "'/home/u/.nvm/bin/codex' 'app-server' '--listen' 'stdio://'" in remote
    assert ".codex-archie" in remote
    assert cwd is None and "CLAUDECODE" not in env


def test_codex_context_window_is_codex_default():
    from manager.codex.catalog import DEFAULT_CONTEXT_WINDOW
    from manager.context_windows import context_window_for

    assert context_window_for("codex", "gpt-6-luna") == DEFAULT_CONTEXT_WINDOW
    assert context_window_for("codex", None) == DEFAULT_CONTEXT_WINDOW
