"""Tests for the orchestrator agent package."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.types import (
    Message,
    ToolCall,
    ToolResult,
    TextDelta,
    TextComplete,
    ToolUseStart,
    ToolResultEvent,
    TurnComplete,
    ErrorEvent,
)
from orchestrator.config import OrchestratorConfig
from orchestrator.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Types tests
# ---------------------------------------------------------------------------


class TestMessage:
    def test_to_api_dict_simple(self):
        msg = Message(role="user", content="Hello")
        d = msg.to_api_dict()
        assert d == {"role": "user", "content": "Hello"}

    def test_to_api_dict_with_blocks(self):
        msg = Message(role="assistant", content=[{"type": "text", "text": "Hi"}])
        d = msg.to_api_dict()
        assert d["role"] == "assistant"
        assert isinstance(d["content"], list)

    def test_tool_call_frozen(self):
        tc = ToolCall(id="tc1", name="test", input={"a": 1})
        with pytest.raises(AttributeError):
            tc.id = "tc2"  # type: ignore

    def test_tool_result(self):
        tr = ToolResult(tool_use_id="tc1", output="ok")
        assert tr.is_error is False

    def test_events_are_frozen(self):
        td = TextDelta(text="hello")
        with pytest.raises(AttributeError):
            td.text = "world"  # type: ignore


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestOrchestratorConfig:
    def test_load_defaults(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/test-claude")
        monkeypatch.delenv("ORCHESTRATOR_MODEL", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_PROVIDER", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_MAX_TOKENS", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_PROJECT_DIR", raising=False)

        config = OrchestratorConfig.load()
        assert config.model == "claude-sonnet-4-5-20250929"
        assert config.provider == "anthropic"
        assert config.max_tokens == 8192
        assert "ORCHESTRATOR_MEMORY.md" in config.memory_path

    def test_load_custom_model(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "claude-opus-4-6")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/test-claude")
        config = OrchestratorConfig.load()
        assert config.model == "claude-opus-4-6"

    def test_memory_path_uses_context_dir(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/my-config")
        config = OrchestratorConfig.load()
        assert "context/memory/ORCHESTRATOR_MEMORY.md" in config.memory_path


# ---------------------------------------------------------------------------
# ToolRegistry tests
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_list(self):
        reg = ToolRegistry()

        @reg.register(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {"arg": {"type": "string"}},
                "required": ["arg"],
            },
        )
        async def test_tool(context: dict, arg: str) -> str:
            return f"got: {arg}"

        assert len(reg) == 1
        assert "test_tool" in reg.tool_names

    def test_get_definitions(self):
        reg = ToolRegistry()

        @reg.register(
            name="my_tool",
            description="Does stuff",
            input_schema={"type": "object", "properties": {}},
        )
        async def my_tool(context: dict) -> str:
            return "ok"

        defs = reg.get_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "my_tool"
        assert defs[0]["description"] == "Does stuff"
        assert "input_schema" in defs[0]

    @pytest.mark.asyncio
    async def test_execute_success(self):
        reg = ToolRegistry()

        @reg.register(
            name="greet",
            description="Greet someone",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
        async def greet(context: dict, name: str) -> str:
            return f"Hello, {name}!"

        result = await reg.execute("greet", {"name": "World"}, context={})
        assert result == "Hello, World!"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        reg = ToolRegistry()
        result = await reg.execute("nonexistent", {}, context={})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown tool" in parsed["error"]

    @pytest.mark.asyncio
    async def test_execute_handler_error(self):
        reg = ToolRegistry()

        @reg.register(
            name="failing",
            description="Always fails",
            input_schema={"type": "object", "properties": {}},
        )
        async def failing(context: dict) -> str:
            raise ValueError("boom")

        result = await reg.execute("failing", {}, context={})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "boom" in parsed["error"]

    @pytest.mark.asyncio
    async def test_execute_filters_extra_params(self):
        """Extra params not in handler signature should be ignored."""
        reg = ToolRegistry()

        @reg.register(
            name="simple",
            description="Simple tool",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        async def simple(context: dict, x: str) -> str:
            return x

        result = await reg.execute("simple", {"x": "ok", "extra": "ignored"}, context={})
        assert result == "ok"


# ---------------------------------------------------------------------------
# Anthropic provider tests
# ---------------------------------------------------------------------------


class TestAnthropicProvider:
    def _make_provider(self, mock_stream):
        """Create a provider with a mocked client."""
        from orchestrator.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._model = "test-model"
        provider._max_tokens = 1024

        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=mock_stream)
        provider._client = mock_client
        return provider

    @pytest.mark.asyncio
    async def test_streaming_text_response(self):
        """Test that text streaming events are yielded correctly."""
        events = _build_mock_text_stream("Hello world")
        mock_stream = _MockAsyncContextStream(events)
        provider = self._make_provider(mock_stream)

        collected = []
        async for event in provider.create_message(
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            system="You are helpful",
        ):
            collected.append(event)

        text_deltas = [e for e in collected if isinstance(e, TextDelta)]
        text_completes = [e for e in collected if isinstance(e, TextComplete)]
        turn_completes = [e for e in collected if isinstance(e, TurnComplete)]

        assert len(text_deltas) >= 1
        assert len(text_completes) == 1
        assert text_completes[0].text == "Hello world"
        assert len(turn_completes) == 1

    @pytest.mark.asyncio
    async def test_streaming_tool_use(self):
        """Test that tool use events are accumulated and yielded."""
        events = _build_mock_tool_stream("my_tool", {"arg": "val"})
        mock_stream = _MockAsyncContextStream(events)
        provider = self._make_provider(mock_stream)

        collected = []
        async for event in provider.create_message(
            messages=[{"role": "user", "content": "Do something"}],
            tools=[{"name": "my_tool"}],
            system="sys",
        ):
            collected.append(event)

        tool_events = [e for e in collected if isinstance(e, ToolUseStart)]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "my_tool"
        assert tool_events[0].tool_input == {"arg": "val"}


# ---------------------------------------------------------------------------
# Mock stream helpers
# ---------------------------------------------------------------------------


class _MockAsyncContextStream:
    """Mock for anthropic's stream context manager."""

    def __init__(self, events: list):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def __aiter__(self):
        for event in self._events:
            yield event


def _make_event(type_: str, **kwargs):
    """Create a mock stream event."""
    ev = MagicMock()
    ev.type = type_
    for k, v in kwargs.items():
        setattr(ev, k, v)
    return ev


def _build_mock_text_stream(text: str) -> list:
    """Build a sequence of mock events for a simple text response."""
    # message_start
    msg = MagicMock()
    msg.usage = MagicMock(input_tokens=10)
    e_msg_start = _make_event("message_start", message=msg)

    # content_block_start (text)
    block = MagicMock()
    block.type = "text"
    e_block_start = _make_event("content_block_start", content_block=block)

    # content_block_delta (text)
    delta = MagicMock()
    delta.type = "text_delta"
    delta.text = text
    e_delta = _make_event("content_block_delta", delta=delta)

    # content_block_stop
    e_block_stop = _make_event("content_block_stop")

    # message_delta
    usage = MagicMock(output_tokens=5)
    e_msg_delta = _make_event("message_delta", usage=usage)

    return [e_msg_start, e_block_start, e_delta, e_block_stop, e_msg_delta]


def _build_mock_tool_stream(tool_name: str, tool_input: dict) -> list:
    """Build mock events for a tool use response."""
    msg = MagicMock()
    msg.usage = MagicMock(input_tokens=15)
    e_msg_start = _make_event("message_start", message=msg)

    # content_block_start (tool_use)
    block = MagicMock()
    block.type = "tool_use"
    block.id = "tool_call_123"
    block.name = tool_name
    e_block_start = _make_event("content_block_start", content_block=block)

    # content_block_delta (input_json_delta)
    delta = MagicMock()
    delta.type = "input_json_delta"
    delta.partial_json = json.dumps(tool_input)
    e_delta = _make_event("content_block_delta", delta=delta)

    # content_block_stop
    e_block_stop = _make_event("content_block_stop")

    # message_delta
    usage = MagicMock(output_tokens=8)
    e_msg_delta = _make_event("message_delta", usage=usage)

    return [e_msg_start, e_block_start, e_delta, e_block_stop, e_msg_delta]


async def _async_iter(items):
    """Convert a list to an async iterator."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# File tools tests
# ---------------------------------------------------------------------------


class TestFileTools:
    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        from orchestrator.tools.files import read_file

        result = await read_file(context={"project_dir": str(tmp_path)}, path="test.txt")
        parsed = json.loads(result)
        assert parsed["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        from orchestrator.tools.files import read_file

        result = await read_file(context={"project_dir": str(tmp_path)}, path="nope.txt")
        parsed = json.loads(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        from orchestrator.tools.files import write_file

        result = await write_file(
            context={"project_dir": str(tmp_path)},
            path="output/new.txt",
            content="written!",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "written"
        assert (tmp_path / "output" / "new.txt").read_text() == "written!"

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, tmp_path):
        from orchestrator.tools.files import read_file

        result = await read_file(
            context={"project_dir": str(tmp_path)},
            path="../../etc/passwd",
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "escapes" in parsed["error"]


# ---------------------------------------------------------------------------
# System prompt builder tests
# ---------------------------------------------------------------------------


class TestPromptBuilder:
    def test_includes_role(self):
        from orchestrator.prompt import build_system_prompt
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(project_dir="/tmp/test", memory_path="/tmp/nonexistent")
        prompt = build_system_prompt(config, context={"orchestrator_sessions": {}})
        assert "orchestrator agent" in prompt
        assert "Claude Code" in prompt

    def test_includes_memory_content(self, tmp_path):
        from orchestrator.prompt import build_system_prompt
        from orchestrator.config import OrchestratorConfig

        mem_file = tmp_path / "ORCHESTRATOR_MEMORY.md"
        mem_file.write_text("# My Memory\nSome context here")

        config = OrchestratorConfig(
            project_dir=str(tmp_path),
            memory_path=str(mem_file),
        )
        prompt = build_system_prompt(config, context={"orchestrator_sessions": {}})
        assert "My Memory" in prompt
        assert "Some context here" in prompt

    def test_shows_no_active_sessions(self):
        from orchestrator.prompt import build_system_prompt
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(project_dir="/tmp/test", memory_path="/tmp/nonexistent")
        prompt = build_system_prompt(config, context={"orchestrator_sessions": {}})
        assert "No agent sessions" in prompt

    def test_shows_active_sessions(self):
        from orchestrator.prompt import build_system_prompt
        from orchestrator.config import OrchestratorConfig
        from manager.types import SessionStatus

        mock_sm = MagicMock()
        mock_sm.status = SessionStatus.IDLE
        mock_sm.turns = 3
        mock_sm.cost = 0.05

        config = OrchestratorConfig(project_dir="/tmp/test", memory_path="/tmp/nonexistent")
        prompt = build_system_prompt(config, context={
            "orchestrator_sessions": {"sess-1": mock_sm},
        })
        assert "sess-1" in prompt
        assert "idle" in prompt


# ---------------------------------------------------------------------------
# Agent loop tests
# ---------------------------------------------------------------------------


class _MockProvider:
    """A mock provider that yields predetermined events."""

    def __init__(self, responses: list[list[OrchestratorEvent]]):
        self._responses = iter(responses)

    async def create_message(self, messages, tools, system):
        events = next(self._responses)
        for e in events:
            yield e


class TestOrchestratorAgent:
    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        from orchestrator.agent import OrchestratorAgent
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(project_dir="/tmp/test", memory_path="/tmp/nonexistent")
        reg = ToolRegistry()

        provider = _MockProvider([
            [TextDelta(text="Hi"), TextComplete(text="Hi there!"), TurnComplete(input_tokens=10, output_tokens=5)],
        ])

        agent = OrchestratorAgent(config, reg, provider, context={"orchestrator_sessions": {}})

        collected = []
        async for event in agent.run("Hello"):
            collected.append(event)

        deltas = [e for e in collected if isinstance(e, TextDelta)]
        completes = [e for e in collected if isinstance(e, TextComplete)]
        turns = [e for e in collected if isinstance(e, TurnComplete)]

        assert len(deltas) == 1
        assert len(completes) == 1
        assert completes[0].text == "Hi there!"
        assert len(turns) == 1

        # History should have user + assistant
        assert len(agent.history) == 2
        assert agent.history[0]["role"] == "user"
        assert agent.history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_tool_use_loop(self):
        """Agent should execute tools and loop back to the model."""
        from orchestrator.agent import OrchestratorAgent
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(project_dir="/tmp/test", memory_path="/tmp/nonexistent")
        reg = ToolRegistry()

        @reg.register(
            name="test_tool",
            description="Test",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        )
        async def test_tool(context, x):
            return json.dumps({"result": x.upper()})

        # First response: tool call. Second response: text.
        provider = _MockProvider([
            [
                ToolUseStart(tool_call_id="tc1", tool_name="test_tool", tool_input={"x": "hello"}),
                TurnComplete(input_tokens=10, output_tokens=5),
            ],
            [
                TextComplete(text="Done! Result was HELLO"),
                TurnComplete(input_tokens=15, output_tokens=8),
            ],
        ])

        agent = OrchestratorAgent(config, reg, provider, context={"orchestrator_sessions": {}})

        collected = []
        async for event in agent.run("Use the tool"):
            collected.append(event)

        tool_starts = [e for e in collected if isinstance(e, ToolUseStart)]
        tool_results = [e for e in collected if isinstance(e, ToolResultEvent)]
        text_completes = [e for e in collected if isinstance(e, TextComplete)]

        assert len(tool_starts) == 1
        assert tool_starts[0].tool_name == "test_tool"
        assert len(tool_results) == 1
        assert "HELLO" in tool_results[0].output
        assert len(text_completes) == 1


# ---------------------------------------------------------------------------
# Session persistence tests
# ---------------------------------------------------------------------------


class TestOrchestratorSession:
    @pytest.mark.asyncio
    async def test_start_creates_jsonl(self, tmp_path, monkeypatch):
        import utils.paths as _paths
        monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path)

        from orchestrator.session import OrchestratorSession
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(project_dir=str(tmp_path), memory_path=str(tmp_path / "mem.md"))

        session = OrchestratorSession(config=config, context={"orchestrator_sessions": {}})

        # Patch the provider creation to avoid real API calls
        with patch("orchestrator.session.AnthropicProvider"):
            sid = await session.start()

        assert sid is not None
        # JSONL file should exist with orchestrator metadata
        jsonl_path = session._jsonl_path
        assert jsonl_path.is_file()

        lines = jsonl_path.read_text().strip().split("\n")
        first = json.loads(lines[0])
        assert first["orchestrator"] is True

        await session.stop()

    @pytest.mark.asyncio
    async def test_resume_loads_history(self, tmp_path, monkeypatch):
        import utils.paths as _paths
        monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path)

        from orchestrator.session import OrchestratorSession
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(project_dir=str(tmp_path), memory_path=str(tmp_path / "mem.md"))

        # Create initial session
        session1 = OrchestratorSession(config=config, context={"orchestrator_sessions": {}})
        with patch("orchestrator.session.AnthropicProvider"):
            sid = await session1.start()

        # Manually write some history
        session1._writer.append({
            "type": "user",
            "message": {"role": "user", "content": "Hello"},
            "timestamp": "2026-01-01T00:00:00Z",
        })
        session1._writer.append({
            "type": "assistant",
            "message": {"role": "assistant", "content": "Hi there!"},
            "timestamp": "2026-01-01T00:00:01Z",
        })
        await session1.stop()

        # Resume session — local_id is different (simulating new tab), session_id
        # is the original for JSONL continuity
        session2 = OrchestratorSession(
            config=config, context={"orchestrator_sessions": {}},
            session_id=sid, local_id="new-tab-id",
        )
        with patch("orchestrator.session.AnthropicProvider"):
            local_id2 = await session2.start()

        assert local_id2 == "new-tab-id"
        assert session2.jsonl_id == sid
        assert len(session2._agent.history) == 2
        assert session2._agent.history[0]["content"] == "Hello"
        # Assistant messages are now in content block format
        assert session2._agent.history[1]["content"] == [{"type": "text", "text": "Hi there!"}]
        await session2.stop()


# ---------------------------------------------------------------------------
# SessionStore orchestrator detection tests
# ---------------------------------------------------------------------------


class TestSessionStoreOrchestrator:
    def test_detects_orchestrator_session(self, tmp_path, monkeypatch):
        """SessionStore should detect orchestrator: true in JSONL metadata."""
        import utils.paths as _paths
        monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path)

        from manager.store import SessionStore

        # Create context dir (where sessions live)
        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        # Write an orchestrator JSONL
        jsonl = context_dir / "orch-session-1.jsonl"
        lines = [
            json.dumps({"type": "orchestrator_meta", "orchestrator": True, "timestamp": "2026-01-01T00:00:00Z"}),
            json.dumps({"type": "user", "message": {"content": "Hello orchestrator"}, "timestamp": "2026-01-01T00:00:01Z"}),
            json.dumps({"type": "assistant", "message": {"content": "Hi!"}, "timestamp": "2026-01-01T00:00:02Z"}),
        ]
        jsonl.write_text("\n".join(lines))

        store = SessionStore(str(tmp_path))
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].is_orchestrator is True

    def test_regular_session_not_orchestrator(self, tmp_path, monkeypatch):
        """Regular sessions should have is_orchestrator=False."""
        import utils.paths as _paths
        monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path)

        from manager.store import SessionStore

        context_dir = tmp_path / "context"
        context_dir.mkdir(parents=True)

        jsonl = context_dir / "regular-session.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"content": "Hello"}, "timestamp": "2026-01-01T00:00:00Z"}),
            json.dumps({"type": "assistant", "message": {"content": "Hi!"}, "timestamp": "2026-01-01T00:00:01Z"}),
        ]
        jsonl.write_text("\n".join(lines))

        store = SessionStore(str(tmp_path))
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].is_orchestrator is False


# ---------------------------------------------------------------------------
# SessionPool orchestrator tests
# ---------------------------------------------------------------------------


class TestSessionPoolOrchestrator:
    def test_set_and_has_orchestrator(self):
        from api.pool import SessionPool

        pool = SessionPool()
        assert not pool.has_orchestrator()
        assert pool.orchestrator_id is None

        mock_session = MagicMock()
        pool.set_orchestrator("s1", mock_session)
        assert pool.has_orchestrator()
        assert pool.orchestrator_id == "s1"
        assert pool.get_orchestrator() is mock_session

    def test_subscribe_orchestrator(self):
        from api.pool import SessionPool

        pool = SessionPool()
        mock_ws = MagicMock()
        mock_session = MagicMock()
        pool.set_orchestrator("s1", mock_session)

        assert pool.subscribe_orchestrator("s1", mock_ws) is True
        assert pool.orchestrator_subscriber_count == 1

        # Wrong ID — should fail
        assert pool.subscribe_orchestrator("other", mock_ws) is False

    def test_subscribe_without_active_orchestrator(self):
        from api.pool import SessionPool

        pool = SessionPool()
        mock_ws = MagicMock()
        assert pool.subscribe_orchestrator("s1", mock_ws) is False

    @pytest.mark.asyncio
    async def test_stop_orchestrator(self):
        from api.pool import SessionPool

        pool = SessionPool()
        mock_session = AsyncMock()
        pool.set_orchestrator("s1", mock_session)

        await pool.stop_orchestrator()
        assert not pool.has_orchestrator()
        assert pool.orchestrator_id is None
        mock_session.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Serializer tests
# ---------------------------------------------------------------------------


class TestOrchestratorSerializer:
    def test_text_delta(self):
        from api.serializers import serialize_orchestrator_event

        result = serialize_orchestrator_event(TextDelta(text="hi"))
        assert result == {"type": "text_delta", "text": "hi"}

    def test_text_complete(self):
        from api.serializers import serialize_orchestrator_event

        result = serialize_orchestrator_event(TextComplete(text="done"))
        assert result == {"type": "text_complete", "text": "done"}

    def test_tool_use_start(self):
        from api.serializers import serialize_orchestrator_event

        result = serialize_orchestrator_event(
            ToolUseStart(tool_call_id="tc1", tool_name="test", tool_input={"a": 1})
        )
        assert result["type"] == "tool_use"
        assert result["tool_name"] == "test"

    def test_turn_complete(self):
        from api.serializers import serialize_orchestrator_event

        result = serialize_orchestrator_event(TurnComplete(input_tokens=10, output_tokens=5))
        assert result["type"] == "turn_complete"
        assert result["input_tokens"] == 10

    def test_error_event(self):
        from api.serializers import serialize_orchestrator_event

        result = serialize_orchestrator_event(ErrorEvent(error="oops", detail="bad"))
        assert result["type"] == "error"
        assert result["error"] == "oops"
