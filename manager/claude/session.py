"""ClaudeSessionManager — wraps a single Claude Code session via claude-agent-sdk.

Implements :class:`manager.base_session.BaseSessionManager`. The class is
exported under both ``ClaudeSessionManager`` and the historical
``SessionManager`` name; consumers of the latter continue to work unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from manager._ssh import (
    RemoteCommand,
    RemoteHostUnreachableError,
    SshTarget,
    build_ssh_argv,
    cleanup_ssh_wrapper_script,
    probe_host_reachable,
    resolve_remote_cli_path,
    write_ssh_wrapper_script,
)

logger = logging.getLogger(__name__)

# Backward-compatibility re-exports.  Callers (including the existing
# regression test suite in tests/test_ssh_session_churn.py) used to import
# these directly from this module; the implementations now live in
# manager._ssh but the import surface is preserved so external code keeps
# working.  When the regression suite is updated to point at the new
# module these can be deleted.
__all__ = ["RemoteHostUnreachableError", "ClaudeSessionManager", "SessionManager", "SessionAbandoned"]

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

# Monkey-patch the SDK's message parser to handle unknown message types gracefully
# instead of raising an exception (e.g., rate_limit_event is not handled by the SDK)
def _patch_sdk_message_parser():
    """Patch SDK to ignore unknown message types instead of crashing."""
    try:
        from claude_agent_sdk._internal import message_parser
        original_parse = message_parser.parse_message

        def patched_parse(data):
            try:
                return original_parse(data)
            except Exception as e:
                if "Unknown message type" in str(e):
                    # Return None for unknown types - we'll filter these out
                    logger.debug("Ignoring unknown message type: %s", data.get("type", "unknown"))
                    return None
                raise

        message_parser.parse_message = patched_parse
        logger.debug("SDK message parser patched for unknown message type handling")
    except Exception as e:
        logger.warning("Could not patch SDK message parser: %s", e)

_patch_sdk_message_parser()

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from ..base_session import BaseSessionManager, TurnAbandoned
from ..config import ManagerConfig
from ..types import (
    CompactComplete,
    Event,
    PermissionRequest,
    PermissionResolved,
    SessionStatus,
    TextComplete,
    TextDelta,
    SessionStalled,
    ThinkingComplete,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)

# Tools that require explicit user (or orchestrator) approval before the SDK
# is allowed to run them.  Everything else auto-allows in our `can_use_tool`
# callback — equivalent to `bypassPermissions` for those tools, but with the
# popup hook still wired so the set can grow without code changes.
_DEFAULT_GATED_TOOLS: frozenset[str] = frozenset({"ExitPlanMode"})


# Appended to the bundled Claude Code system prompt at session start.  Turns
# gated-tool permission popups into conversational checkpoints — see
# _build_options for the wiring rationale.
_PERMISSION_GATING_PROMPT = (
    "\n\n## Gated Tools — Conversational Checkpoint\n"
    "Before calling ExitPlanMode (or any other tool that triggers a permission "
    "prompt), first send a normal user-facing message describing what you "
    "intend to do and inviting the user (or orchestrator) to provide guidance, "
    "ask questions, or approve.  Only after that message should you call the "
    "gated tool.  The permission popup is a safety net — your conversational "
    "announcement is the primary checkpoint.  If the user responds with prose "
    "(\"go ahead, but skip migrations\"), incorporate that feedback into your "
    "plan or actions before proceeding.  When the user types a chat message "
    "while a permission is pending, the system auto-treats that as a denial "
    "with their prose as the reason — refine your approach based on their "
    "feedback and re-announce when ready."
)

# Stall watchdog: the bundled `claude` subprocess occasionally goes silent
# mid-tool (e.g. WebFetch waiting on an unresponsive HTTP endpoint with no
# upstream timeout).  We don't abort — the user may legitimately want to
# wait on a slow tool — but we do surface a SessionStalled event so the UI
# can show a "this looks stuck" banner with an interrupt affordance.
_STALL_FIRST_NOTICE_S = 120.0   # first warning after 2 min of silence
_STALL_REPEAT_INTERVAL_S = 60.0  # re-emit every minute thereafter

# Abandoned-turn detection: distinct from a mid-tool stall.  If the SDK has
# produced *zero* messages in this turn after this many seconds, the request
# almost certainly never reached Anthropic (e.g. the kernel TCP path to the
# API silently wedged with retransmits — observed once with cwnd:1
# backoff:10 lastrcv:8min).  Raise SessionAbandoned so callers can give up
# cleanly and (in the orchestrator's case) retry once.
_TURN_ABANDON_S = 240.0


class SessionAbandoned(TurnAbandoned):
    """Raised by SessionManager.send when a Claude turn produced zero events
    for so long we conclude the upstream request never landed.  Distinct
    from a mid-tool stall: by the time this fires, ``last_tool_name`` is
    None and ``messages_received == 0`` — no progress has been made at all.

    Inherits :class:`manager.base_session.TurnAbandoned` so catch sites
    that want to handle both Claude and Qwen abandoned turns can do so
    with a single ``except TurnAbandoned`` clause.
    """


# Process-management helpers live in manager/_proc.py so they're importable
# without dragging in claude-agent-sdk (which this module imports at load time).
# Re-export the legacy underscore names so pool / tests that already import
# them from here keep working.
from .._proc import (
    process_alive as _process_alive,
    process_comm as _process_comm,
    kill_subprocess as _kill_subprocess,
)


def _looks_like_claude(pid: int) -> bool:
    """Return True if /proc/<pid>/comm matches the bundled `claude` cli.

    The kernel's comm is the basename of the executable, capped at 15
    chars — for our subprocess that's exactly ``claude``.  We accept any
    value that starts with ``claude`` to tolerate possible future renames.
    """
    from .._proc import looks_like
    return looks_like(pid, "claude")


def _extract_subprocess_pid(client: ClaudeSDKClient) -> int | None:
    """Best-effort extraction of the bundled-claude subprocess PID from a
    connected ``ClaudeSDKClient``.

    The SDK doesn't expose this publicly, so we walk private attributes:
    ``client._transport._process.pid``.  Wrapped in defensive ``getattr``s
    and a broad except so any future SDK refactor (renamed attribute,
    custom transport, etc.) just yields None instead of crashing the
    session — at worst we lose the per-session SIGKILL fallback and
    rely on the pool's orphan reaper to clean up.
    """
    try:
        transport = getattr(client, "_transport", None)
        if transport is None:
            return None
        process = getattr(transport, "_process", None)
        if process is None:
            return None
        pid = getattr(process, "pid", None)
        return int(pid) if pid is not None else None
    except Exception:
        logger.debug("could not extract SDK subprocess pid", exc_info=True)
        return None


def kill_claude_subprocess(pid: int, *, sigterm_grace_s: float = 0.5) -> bool:
    """Force-kill an orphaned bundled-claude subprocess identified by *pid*.

    Verifies the pid still matches a ``claude*`` comm via ``/proc/<pid>/comm``
    before signalling — the kernel can recycle pids immediately after a
    process exits, and we never want to SIGKILL an unrelated process that
    happened to inherit the number.

    First sends SIGTERM (with *sigterm_grace_s* for clean shutdown); if the
    process is still alive after that, escalates to SIGKILL.  Returns True
    if a signal was sent, False otherwise.

    Safe to call concurrently from the per-session lifecycle finally and
    from the pool's orphan reaper.
    """
    return _kill_subprocess(pid, comm_prefix="claude", sigterm_grace_s=sigterm_grace_s)


class ClaudeSessionManager(BaseSessionManager):
    """Manage a single Claude Code conversation.

    Usage::

        sm = ClaudeSessionManager()
        session_id = await sm.start()

        async for event in sm.send("Hello!"):
            if isinstance(event, TextDelta):
                print(event.text, end="", flush=True)

        await sm.stop()

    Or as an async context manager::

        async with ClaudeSessionManager() as sm:
            async for event in sm.send("Hello!"):
                ...
    """

    def __init__(
        self,
        session_id: str | None = None,
        *,
        local_id: str | None = None,
        fork: bool = False,
        config: ManagerConfig | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id, local_id=local_id, fork=fork, config=config,
        )
        # Claude-specific state — everything shared with Qwen now lives on
        # BaseSessionManager.
        self._client: ClaudeSDKClient | None = None
        self._ssh_wrapper_path: str | None = None  # temp script for SSH sessions
        # PID of the bundled `claude` subprocess that the SDK transport opens
        # at connect() time.  Captured so we can SIGKILL it ourselves if the
        # SDK's transport.close() hangs on its own bounded-but-actually-
        # unbounded `await self._process.wait()` after SIGTERM.  Setting this
        # to None after a successful clean exit lets stop() distinguish
        # "process already gone" from "we should kill it".
        self._subprocess_pid: int | None = None
        # Override base class defaults with Claude's gated-tool set.
        self._gated_tools = set(_DEFAULT_GATED_TOOLS)

    @property
    def provider_name(self) -> str:
        return "claude"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _pre_start_check(self) -> None:
        """Cheap reachability pre-probe for remote sessions.

        One ICMP ping, 2 s deadline — if the host is asleep/offline we
        raise immediately instead of letting the SDK sit in a 30 s SSH
        TCP timeout (which historically pinned the CPU and spun the fan
        on the Jetson while the laptop was hibernating).  Done here (not
        in the lifecycle task) so the caller sees the failure synchronously.
        """
        if not self._config.ssh_host:
            return
        reachable = await asyncio.get_running_loop().run_in_executor(
            None, probe_host_reachable, self._config.ssh_host, 2.0,
        )
        if not reachable:
            raise RemoteHostUnreachableError(
                f"SSH host {self._config.ssh_host!r} did not reply to ICMP ping; "
                "refusing to open SSH connection (prevents stuck sessions and "
                "fan/CPU churn while the remote is offline)."
            )

    async def _run_lifecycle(self) -> None:
        """Own connect → idle-wait → disconnect from a single task.

        Both ``client.connect()`` (which enters the SDK's anyio task group)
        and ``client.disconnect()`` (which exits it) run inside this task.
        That's the only way to satisfy anyio's "exit from the same task you
        entered" invariant when the actual stop() trigger arrives from a
        different task (an HTTP request handler, the pool drain on shutdown,
        etc.).
        """
        try:
            options = self._build_options()
            self._client = ClaudeSDKClient(options)
            await self._client.connect()

            # Capture the bundled-claude subprocess PID via the SDK's
            # private transport attribute.  This is best-effort: if a
            # future SDK release moves the field, we fall back to the
            # pool-level orphan reaper as a safety net.  A captured PID
            # lets stop() force-kill if SDK transport.close() hangs on
            # its (unbounded) `await self._process.wait()` after SIGTERM.
            self._subprocess_pid = _extract_subprocess_pid(self._client)
            if self._subprocess_pid is not None:
                logger.debug(
                    "Session %s SDK subprocess pid=%d", self._local_id, self._subprocess_pid
                )

            # Capture the SDK session ID if available at connect time.
            if self._resume_id:
                self._provider_session_id = self._resume_id
            else:
                try:
                    server_info = await self._client.get_server_info()
                    if server_info:
                        self._provider_session_id = server_info.get("session_id")
                except Exception:
                    # Failing to read server_info shouldn't kill the session;
                    # the SDK ID will be filled in from the first ResultMessage.
                    logger.exception("get_server_info failed for session %s", self._local_id)

            self._status = SessionStatus.IDLE
        except BaseException as e:
            # Surface the error to start() and exit; do NOT signal _connect_done
            # before recording the error or start() will see "succeeded".
            self._connect_error = e
            self._connect_done.set()
            return

        # Tell start() it can return.
        self._connect_done.set()

        # Idle-wait until stop() is requested.  No CPU cost — pure event wait.
        try:
            await self._stop_requested.wait()
        finally:
            # Disconnect runs in this same task, so the SDK's task group
            # __aexit__ sees the same owner that __aenter__'d it.
            #
            # Bound the disconnect at 8s — comfortably under pool.close()'s
            # 10s outer timeout — so we always get a chance to escalate to
            # SIGKILL if the SDK transport's own internal wait() blocks.
            # The SDK's transport.close() does:
            #     self._process.terminate()
            #     await self._process.wait()   # NO TIMEOUT
            # If the bundled `claude` ignores SIGTERM (mid-flush, busy-loop,
            # etc.) the wait blocks forever and the lifecycle task pins a
            # CPU until we force-kill.  Pre-fix, this leaked subprocesses
            # accumulated on the Jetson at ~2.5% CPU each.
            pid_to_kill = self._subprocess_pid
            if self._client is not None:
                try:
                    await asyncio.wait_for(self._client.disconnect(), timeout=8.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "client.disconnect() for session %s exceeded 8s; will force-kill pid %s",
                        self._local_id,
                        pid_to_kill,
                    )
                except Exception:
                    logger.exception(
                        "client.disconnect() failed for session %s", self._local_id
                    )
                self._client = None

            # SIGTERM/SIGKILL fallback for orphaned subprocesses.  Runs
            # whether disconnect succeeded, timed out, or threw — the only
            # cost when the SDK already cleaned up is one cheap os.kill(0)
            # liveness check that finds the pid gone.
            if pid_to_kill is not None and _process_alive(pid_to_kill):
                killed = await asyncio.get_running_loop().run_in_executor(
                    None, kill_claude_subprocess, pid_to_kill
                )
                if killed:
                    logger.warning(
                        "Reaped orphaned claude subprocess pid=%d for session %s",
                        pid_to_kill,
                        self._local_id,
                    )
            self._subprocess_pid = None

            self._status = SessionStatus.DISCONNECTED
            cleanup_ssh_wrapper_script(self._ssh_wrapper_path)
            self._ssh_wrapper_path = None

    async def interrupt(self) -> None:
        """Interrupt the current response.

        ``ClaudeSDKClient.interrupt()`` is a coroutine — it must be awaited or
        the signal never reaches the CLI and the in-flight ``receive_response()``
        keeps streaming until the turn finishes naturally.
        """
        if self._client is not None:
            await self._client.interrupt()
        self._status = SessionStatus.INTERRUPTED

    # ------------------------------------------------------------------
    # Sending messages / commands
    # ------------------------------------------------------------------

    async def compact(self) -> AsyncIterator[Event]:
        """Trigger conversation compaction.

        Sends /compact as a slash command. After all SDK events are yielded,
        emits a ``CompactComplete`` so the frontend can display a divider.
        The SDK only emits ``SystemMessage(subtype="compact")`` for auto-compact;
        for manual compact we synthesize the event ourselves.
        """
        got_compact_event = False
        async for event in self.command("/compact"):
            if isinstance(event, CompactComplete):
                got_compact_event = True
            yield event
        if not got_compact_event:
            yield CompactComplete(trigger="manual", summary="")

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

        While receiving, a stall watchdog yields :class:`SessionStalled`
        events if the SDK goes silent for an extended period — the stream
        is *not* aborted (the caller may want to keep waiting), but the UI
        gets a chance to surface a "looks stuck, interrupt?" banner.
        """
        if self._client is None:
            raise RuntimeError("SessionManager is not connected — call start() first")

        # Drain any stale messages left in the SDK's per-client receive buffer
        # before kicking off this turn.  Background:
        #
        # The SDK keeps a single shared ``anyio.MemoryObjectStream`` (size 100)
        # for ALL messages from the bundled ``claude`` CLI.  ``receive_response()``
        # is just "iterate until you see ANY ResultMessage, then return" — it
        # has no per-turn fence.  If the previous turn was interrupted while
        # the CLI was mid-stream (e.g. the WS tab reloaded, our drain task got
        # cancelled, ``_interrupt_if_orphaned`` fired), the CLI may continue
        # to emit messages — including a synthesized terminal ResultMessage —
        # *after* our drain task has already been torn down.  Those messages
        # then sit in the SDK's buffer and get picked up by the NEXT turn's
        # ``receive_response()``, which sees the leftover ResultMessage and
        # exits immediately.  Status flips to IDLE, the frontend gets a fake
        # turn_complete with no content, and the CLI happily processes the
        # actual prompt with no Python reader watching.
        #
        # The buffer is bounded (size 100), so this drain is fast.  We also
        # log a warning on non-zero drain counts — that's the smoking gun for
        # the cross-turn-contamination bug class.
        #
        # First drain pass catches messages already buffered.  We then yield
        # the loop briefly so any in-flight sends from the read-task
        # (cancelled but not yet awaited at the prior turn's tear-down) can
        # complete and land in the buffer; a second drain catches those.
        # The 50 ms is empirical: enough for one or two anyio task switches
        # without adding meaningful latency to a normal send().
        stale_drained = await self._drain_stale_sdk_messages()
        await asyncio.sleep(0.05)
        stale_drained += await self._drain_stale_sdk_messages()
        if stale_drained:
            logger.warning(
                "Drained %d stale SDK message(s) for session %s before turn "
                "(prior turn left messages in the SDK buffer)",
                stale_drained,
                self._local_id,
            )

        self._status = SessionStatus.STREAMING
        await self._client.query(prompt)

        # Last-tool tracking so the SessionStalled event can name the tool
        # the SDK was waiting on (the most useful single piece of context
        # for "what looks stuck").
        last_tool_name: str | None = None
        last_tool_use_id: str | None = None

        # We run the SDK receiver in a dedicated task that pushes messages
        # into a queue.  The watchdog reads from the queue with a short
        # `wait_for`, which we can safely time out and retry without
        # disturbing the generator (cancelling `__anext__` directly tears
        # down the async generator).
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()
        # Expose the same queue to the can_use_tool callback so it can inject
        # PermissionRequest / PermissionResolved events into the live stream
        # without a second drain task or out-of-order delivery.
        self._event_inbox = queue
        SENTINEL_DONE = object()

        async def _drain() -> None:
            try:
                async for m in self._client.receive_response():
                    await queue.put(m)
            except BaseException as exc:  # noqa: BLE001
                await queue.put(exc)
            else:
                await queue.put(SENTINEL_DONE)

        drain_task = asyncio.create_task(_drain(), name="sdk-receive-drain")
        turn_started_at = loop.time()
        last_msg_at = turn_started_at
        stall_notified_at: float | None = None
        messages_received = 0

        try:
            while True:
                now = loop.time()
                if stall_notified_at is None:
                    next_notice_in = max(0.0, _STALL_FIRST_NOTICE_S - (now - last_msg_at))
                else:
                    next_notice_in = max(
                        0.0,
                        _STALL_REPEAT_INTERVAL_S - (now - stall_notified_at),
                    )

                try:
                    msg = await asyncio.wait_for(
                        queue.get(), timeout=max(next_notice_in, 0.5),
                    )
                except asyncio.TimeoutError:
                    now = loop.time()
                    # If we've never received a single message and we've been
                    # waiting longer than _TURN_ABANDON_S, the upstream request
                    # never landed.  Give up so the caller can retry instead
                    # of hanging forever.
                    if messages_received == 0 and (now - turn_started_at) >= _TURN_ABANDON_S:
                        raise SessionAbandoned(now - turn_started_at)
                    yield SessionStalled(
                        elapsed_seconds=now - last_msg_at,
                        last_tool_name=last_tool_name,
                        last_tool_use_id=last_tool_use_id,
                    )
                    stall_notified_at = now
                    continue

                last_msg_at = loop.time()
                stall_notified_at = None  # reset on any fresh activity
                messages_received += 1

                if msg is SENTINEL_DONE:
                    break
                if isinstance(msg, BaseException):
                    raise msg
                if msg is None:
                    # Patched parser ignored an unknown message type.
                    continue
                if isinstance(msg, Event):
                    # Out-of-band event injected by can_use_tool — yield as-is.
                    yield msg
                    continue

                async for event in self._process_message(msg):
                    if isinstance(event, ToolUse):
                        last_tool_name = event.tool_name
                        last_tool_use_id = event.tool_use_id
                    elif isinstance(event, (ToolResult, TurnComplete)):
                        last_tool_name = None
                        last_tool_use_id = None
                    yield event
        finally:
            if not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass
            # Stop accepting injected events; cancel any orphan permissions so
            # the SDK doesn't leak a future that nothing will ever resolve.
            self._event_inbox = None
            self._drain_pending_permissions()

        self._status = SessionStatus.IDLE

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def subprocess_pid(self) -> int | None:
        """PID of the bundled-claude subprocess (or None if not connected
        / not yet captured / SDK transport changed shape).  Used by the
        pool's orphan reaper as a fallback in case the per-session
        SIGKILL path didn't run."""
        return self._subprocess_pid

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict,
        _context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """SDK permission callback — auto-allow everything except gated tools.

        For gated tools (e.g. ``ExitPlanMode``) we emit a ``PermissionRequest``
        event onto the active send-stream and await a Future resolved by
        :meth:`resolve_permission`.  No active stream → auto-allow rather than
        deadlock (an out-of-band tool call shouldn't be silently blocked).
        """
        if tool_name not in self._gated_tools:
            return PermissionResultAllow()

        if self._event_inbox is None:
            logger.warning(
                "can_use_tool fired for %s but no active send() stream — auto-allowing",
                tool_name,
            )
            return PermissionResultAllow()

        decision, message = await self._emit_permission_request(tool_name, tool_input)
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message=message or "Denied", interrupt=False)

    async def _drain_stale_sdk_messages(self) -> int:
        """Discard any messages sitting in the SDK's per-client receive buffer.

        Called at the top of every ``send()`` to prevent leftover messages
        from a previously-cancelled turn from being consumed by the new
        turn's ``receive_response()``.  See the comment in ``send()`` for the
        full rationale.

        Reaches into ``client._query._message_receive`` because the SDK has
        no public drain API.  Wrapped in defensive ``getattr``s so a future
        SDK refactor degrades to "drain doesn't run" rather than crashing
        the session — at worst we regress to the pre-fix behavior for one
        turn until the user files a bug.

        Returns the number of messages drained.  Always 0 on a healthy
        session; non-zero indicates the cross-turn-contamination bug
        condition just triggered (and was prevented).
        """
        client = self._client
        if client is None:
            return 0
        query = getattr(client, "_query", None)
        if query is None:
            return 0
        receive_stream = getattr(query, "_message_receive", None)
        if receive_stream is None:
            return 0

        # Be strict about the type — defensive against future SDK refactors and
        # against tests that pass a MagicMock client (whose auto-mocked
        # ``receive_nowait`` would never raise ``WouldBlock`` and would spin
        # the drain loop until the cap, polluting logs).
        from anyio import WouldBlock, EndOfStream
        from anyio.streams.memory import MemoryObjectReceiveStream

        if not isinstance(receive_stream, MemoryObjectReceiveStream):
            return 0

        drained = 0
        # Hard cap matches the SDK's buffer size (100) plus a safety margin —
        # if the buffer somehow grows past that, breaking out instead of
        # spinning forever is the right call.
        for _ in range(200):
            try:
                msg = receive_stream.receive_nowait()
            except (WouldBlock, EndOfStream):
                break
            except Exception:
                logger.exception(
                    "Unexpected error draining SDK buffer for session %s",
                    self._local_id,
                )
                break
            drained += 1
            # Be loud about what we threw away — if a user later asks "where
            # did my message go?", this log answers it.
            if isinstance(msg, dict):
                msg_type = msg.get("type", "?")
                logger.warning(
                    "Discarded stale SDK message type=%s for session %s "
                    "(from a previously-cancelled turn)",
                    msg_type,
                    self._local_id,
                )
            else:
                logger.warning(
                    "Discarded stale SDK message %r for session %s",
                    type(msg).__name__,
                    self._local_id,
                )
        return drained

    def _build_options(self) -> ClaudeAgentOptions:
        """Build SDK options from our config."""
        kwargs: dict = {
            "include_partial_messages": True,
            "setting_sources": ["project", "local"],
            "can_use_tool": self._can_use_tool,
            # Append a small policy onto the bundled Claude Code system prompt
            # turning gated-tool permission popups into conversational
            # checkpoints.  Without this nudge the agent fires ExitPlanMode
            # cold and the user (or orchestrator) only ever sees a yes/no
            # popup; with it, the agent first announces intent in chat,
            # invites prose feedback ("yes but skip migrations"), and only
            # then calls the gated tool.  The popup remains as a safety net.
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": _PERMISSION_GATING_PROMPT,
            },
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
        if self._config.mcp_servers is not None:
            # Pass MCP servers directly to the SDK
            # When mcp_servers is provided, it overrides settings from .claude.json
            kwargs["mcp_servers"] = self._config.mcp_servers
        if self._config.extra_args:
            kwargs["extra_args"] = self._config.extra_args

        # Strip CLAUDECODE to allow launching SDK sessions from within a
        # Claude Code process (e.g. VSCode extension or the wrapper itself).
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if self._config.ssh_host and self._config.ssh_claude_config_dir:
            # Override CLAUDE_CONFIG_DIR so the remote claude writes its JSONL
            # to the correct path on the target machine.
            env["CLAUDE_CONFIG_DIR"] = self._config.ssh_claude_config_dir
        kwargs["env"] = env

        # Capture stderr so errors are visible in logs instead of being swallowed
        def _log_stderr(line: str) -> None:
            logger.error("claude CLI stderr [%s]: %s", self._local_id, line.rstrip())

        kwargs["stderr"] = _log_stderr

        if self._config.ssh_host:
            # ── Path B: SSH remote execution ──────────────────────────────
            # The SDK calls:  <cli_path> --output-format stream-json [flags...]
            # We set cli_path to a temp shell script that SSHes into the remote
            # host, cd's into the project dir, and execs `claude "$@"` so all
            # SDK-supplied flags pass through unchanged.
            kwargs["cli_path"] = self._write_ssh_wrapper()
            # cwd must exist locally; the real working dir is set on the remote side
            kwargs["cwd"] = str(Path.home())
        else:
            kwargs["cwd"] = self._config.project_dir

        return ClaudeAgentOptions(**kwargs)

    def _write_ssh_wrapper(self) -> str:
        """Write a temp shell script that SSHes into the remote host and runs claude.

        The SDK takes a ``cli_path`` and then invokes ``<cli_path> arg1
        arg2 ...`` itself, so we can't intercept its argv from Python.
        The wrapper handles ``"$@"`` forwarding (see
        :func:`manager._ssh.write_ssh_wrapper_script` for the details of
        the SSH single-argument trick).  Returns the path to the script.
        """
        target = SshTarget(
            host=self._config.ssh_host or "",
            user=self._config.ssh_user,
            key=self._config.ssh_key,
            control_path_prefix="claude",
        )
        remote_claude = resolve_remote_cli_path(
            "claude",
            target,
            extra_search_paths=[
                "~/.local/bin/claude",
                "/usr/local/bin/claude",
                "/usr/bin/claude",
            ],
        )
        env: dict[str, str] = {}
        if self._config.ssh_claude_config_dir:
            env["CLAUDE_CONFIG_DIR"] = self._config.ssh_claude_config_dir
        remote_cmd = RemoteCommand(
            project_dir=self._config.project_dir,
            remote_cli=remote_claude,
            env=env,
        ).render_shell()
        path = write_ssh_wrapper_script(
            ssh_argv=build_ssh_argv(target),
            remote_cmd=remote_cmd,
            prefix="claude",
        )
        self._ssh_wrapper_path = path
        return path

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
            data = msg.data if isinstance(msg.data, dict) else {}
            # The "init" system message carries the SDK session_id as its first
            # message after connect. Capture it eagerly so sessions that are
            # still mid-tool (no ResultMessage yet) are still addressable by
            # their SDK id — otherwise they vanish from /api/sessions on refresh.
            sid = data.get("session_id")
            if sid and not self._provider_session_id:
                self._provider_session_id = sid
            if msg.subtype == "compact":
                trigger = data.get("trigger", "manual")
                summary = data.get("summary", "")
                yield CompactComplete(trigger=trigger, summary=summary)

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
            # User messages with tool_use_result contain tool output.
            # The SDK normally hands us a dict, but some tools (notably the
            # bundled web search/fetch path on certain claude-cli versions)
            # send the raw stdout as a plain string.  Treat that string as
            # the output rather than dropping the result silently — losing
            # a tool_result leaves the UI showing a perpetual spinner.
            if msg.tool_use_result:
                result = msg.tool_use_result
                if isinstance(result, dict):
                    content = result.get("content", "")
                    if isinstance(content, list):
                        content = json.dumps(content)
                    yield ToolResult(
                        tool_use_id=result.get("tool_use_id", "")
                            or (msg.parent_tool_use_id or ""),
                        output=str(content),
                        is_error=result.get("is_error", False),
                    )
                elif isinstance(result, str):
                    yield ToolResult(
                        tool_use_id=msg.parent_tool_use_id or "",
                        output=result,
                        is_error=False,
                    )
                else:
                    logger.warning(
                        "UserMessage.tool_use_result is unsupported type: %r",
                        type(result),
                    )

        elif isinstance(msg, ResultMessage):
            self._turns += msg.num_turns
            if msg.total_cost_usd is not None:
                self._cost += msg.total_cost_usd
            # Always capture the SDK session ID from ResultMessage
            if msg.session_id:
                self._provider_session_id = msg.session_id
            yield TurnComplete(
                cost=msg.total_cost_usd,
                usage=msg.usage or {},
                num_turns=msg.num_turns,
                session_id=msg.session_id,
                is_error=msg.is_error,
                result=msg.result,
            )


# Backward-compat alias — the historical name is widely imported.
SessionManager = ClaudeSessionManager
