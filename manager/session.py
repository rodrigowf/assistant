"""SessionManager — wraps a single Claude Code session via claude-agent-sdk."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

logger = logging.getLogger(__name__)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from .config import ManagerConfig
from .types import (
    CompactComplete,
    Event,
    SessionStatus,
    TextComplete,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)


class SessionManager:
    """Manage a single Claude Code conversation.

    Usage::

        sm = SessionManager()
        session_id = await sm.start()

        async for event in sm.send("Hello!"):
            if isinstance(event, TextDelta):
                print(event.text, end="", flush=True)

        await sm.stop()

    Or as an async context manager::

        async with SessionManager() as sm:
            async for event in sm.send("Hello!"):
                ...
    """

    def __init__(
        self,
        session_id: str | None = None,
        *,
        fork: bool = False,
        config: ManagerConfig | None = None,
    ) -> None:
        self._config = config or ManagerConfig.load()
        self._resume_id = session_id
        self._fork = fork
        self._session_id: str | None = None
        self._client: ClaudeSDKClient | None = None
        self._status = SessionStatus.DISCONNECTED
        self._cost: float = 0.0
        self._turns: int = 0

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> SessionManager:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> str:
        """Connect to Claude Code and return the session ID."""
        import uuid

        options = self._build_options()
        self._client = ClaudeSDKClient(options)
        await self._client.connect()

        # Use resume_id if provided (resuming existing session).
        # Otherwise check server_info or generate a new UUID.
        # Note: The actual SDK session ID comes back in ResultMessage after
        # queries, so for new sessions _session_id may be updated later.
        if self._resume_id:
            self._session_id = self._resume_id
        else:
            server_info = await self._client.get_server_info()
            if server_info:
                self._session_id = server_info.get("session_id") or str(uuid.uuid4())
            else:
                self._session_id = str(uuid.uuid4())

        self._status = SessionStatus.IDLE
        return self._session_id or ""

    async def stop(self) -> None:
        """Disconnect from Claude Code."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        self._status = SessionStatus.DISCONNECTED

    async def interrupt(self) -> None:
        """Interrupt the current response."""
        if self._client is not None:
            self._client.interrupt()
        self._status = SessionStatus.INTERRUPTED

    # ------------------------------------------------------------------
    # Sending messages / commands
    # ------------------------------------------------------------------

    async def compact(self) -> AsyncIterator[Event]:
        """Trigger conversation compaction.

        The PreCompact hook will auto-export before the compaction runs,
        then this yields the normal response events (including a
        ``CompactComplete`` when the SDK confirms it).
        """
        async for event in self.command("/compact"):
            yield event

    async def command(self, slash_command: str) -> AsyncIterator[Event]:
        """Send an arbitrary slash command (e.g. ``/compact``, ``/help``).

        Yields typed events just like :meth:`send`.
        """
        async for event in self.send(slash_command):
            yield event

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        """Send a message and yield typed events as the response streams in.

        Yields ``TextDelta`` for each streaming token, ``ToolUse`` / ``ToolResult``
        for tool interactions, and ``TurnComplete`` at the end.
        """
        if self._client is None:
            raise RuntimeError("SessionManager is not connected — call start() first")

        self._status = SessionStatus.STREAMING
        await self._client.query(prompt)

        async for msg in self._client.receive_response():
            async for event in self._process_message(msg):
                yield event

        self._status = SessionStatus.IDLE

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def is_active(self) -> bool:
        return self._status not in (
            SessionStatus.DISCONNECTED,
            SessionStatus.INTERRUPTED,
        )

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def cost(self) -> float:
        return self._cost

    @property
    def turns(self) -> int:
        return self._turns

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_options(self) -> ClaudeAgentOptions:
        """Build SDK options from our config."""
        kwargs: dict = {
            "cwd": self._config.project_dir,
            "include_partial_messages": True,
            "setting_sources": ["project", "local"],
        }
        if self._config.permission_mode:
            kwargs["permission_mode"] = self._config.permission_mode
        if self._config.model:
            kwargs["model"] = self._config.model
        if self._config.max_budget_usd is not None:
            kwargs["max_budget_usd"] = self._config.max_budget_usd
        if self._config.max_turns is not None:
            kwargs["max_turns"] = self._config.max_turns
        if self._resume_id:
            kwargs["resume"] = self._resume_id
        if self._fork:
            kwargs["fork_session"] = True

        return ClaudeAgentOptions(**kwargs)

    async def _process_message(self, msg: object) -> AsyncIterator[Event]:
        """Convert an SDK message into our typed Event stream."""

        if isinstance(msg, StreamEvent):
            event = msg.event
            if not isinstance(event, dict):
                logger.warning("StreamEvent.event is not a dict: %r", type(event))
                return
            evt_type = event.get("type", "")

            if evt_type == "content_block_delta":
                delta = event.get("delta", {})
                if not isinstance(delta, dict):
                    return
                delta_type = delta.get("type", "")

                if delta_type == "text_delta":
                    self._status = SessionStatus.STREAMING
                    yield TextDelta(text=delta.get("text", ""))

                elif delta_type == "thinking_delta":
                    self._status = SessionStatus.THINKING
                    yield ThinkingDelta(text=delta.get("thinking", ""))

                elif delta_type == "input_json_delta":
                    # Tool input is streamed as partial JSON — we skip deltas
                    # and let the full ToolUseBlock from AssistantMessage handle it.
                    pass

        elif isinstance(msg, SystemMessage):
            if msg.subtype == "compact":
                data = msg.data if isinstance(msg.data, dict) else {}
                trigger = data.get("trigger", "manual")
                yield CompactComplete(trigger=trigger)

        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    yield TextComplete(text=block.text)

                elif isinstance(block, ThinkingBlock):
                    yield ThinkingComplete(text=block.thinking)

                elif isinstance(block, ToolUseBlock):
                    self._status = SessionStatus.TOOL_USE
                    yield ToolUse(
                        tool_use_id=block.id,
                        tool_name=block.name,
                        tool_input=block.input,
                    )

                elif isinstance(block, ToolResultBlock):
                    content = block.content
                    if isinstance(content, list):
                        content = json.dumps(content)
                    yield ToolResult(
                        tool_use_id=block.tool_use_id,
                        output=content or "",
                        is_error=block.is_error or False,
                    )

        elif isinstance(msg, UserMessage):
            # User messages with tool_use_result contain tool output
            if msg.tool_use_result:
                result = msg.tool_use_result
                if not isinstance(result, dict):
                    logger.warning("UserMessage.tool_use_result is not a dict: %r", type(result))
                    return
                content = result.get("content", "")
                if isinstance(content, list):
                    content = json.dumps(content)
                yield ToolResult(
                    tool_use_id=result.get("tool_use_id", ""),
                    output=str(content),
                    is_error=result.get("is_error", False),
                )

        elif isinstance(msg, ResultMessage):
            self._turns += msg.num_turns
            if msg.total_cost_usd is not None:
                self._cost += msg.total_cost_usd
            # Update session_id from ResultMessage if we didn't have one
            # (for new sessions where we generated a placeholder UUID)
            if msg.session_id and not self._resume_id:
                self._session_id = msg.session_id
            yield TurnComplete(
                cost=msg.total_cost_usd,
                usage=msg.usage or {},
                num_turns=msg.num_turns,
                session_id=msg.session_id,
                is_error=msg.is_error,
                result=msg.result,
            )
