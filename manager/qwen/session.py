"""QwenSessionManager — wraps a single Qwen Code conversation.

Unlike Claude Code, the bundled ``qwen`` CLI is one-shot per turn: it
reads stream-json from stdin, runs the agent loop, writes stream-json
to stdout, and exits.  Multi-turn conversations are stitched together
by passing ``--resume <session-id>`` on subsequent invocations.

This file implements the same :class:`BaseSessionManager` contract as
:class:`manager.claude.session.ClaudeSessionManager`, so the pool and
WebSocket layer don't need provider-specific code paths.

Event format: Qwen's ``--output-format stream-json`` emits Anthropic-style
events — ``system.init``, ``stream_event.{message_start, content_block_*}``,
``assistant`` (complete message), ``result``.  We translate them into the
normalized :mod:`manager.types` event hierarchy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from .._ssh import (
    RemoteCommand,
    RemoteHostUnreachableError,
    SshTarget,
    build_remote_argv,
    probe_host_reachable,
    resolve_remote_cli_path,
)
from ..base_session import BaseSessionManager, TurnAbandoned
from ..config import ManagerConfig
from ..types import (
    CompactComplete,
    Event,
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

logger = logging.getLogger(__name__)


# Same watchdog policy as Claude: warn after 2 min of silence, repeat every 60s.
_STALL_FIRST_NOTICE_S = 120.0
_STALL_REPEAT_INTERVAL_S = 60.0
# Abandoned-turn detection — produced zero events for this long → give up.
_TURN_ABANDON_S = 240.0


class QwenAbandoned(TurnAbandoned):
    """Raised when a Qwen turn produced no events for so long the request
    almost certainly never landed.

    Inherits :class:`manager.base_session.TurnAbandoned` so catch sites
    that want to handle both Claude and Qwen abandoned turns can do so
    with a single ``except TurnAbandoned`` clause.
    """


def _qwen_executable() -> str:
    """Resolve the path to the ``qwen`` CLI.

    Honors ``QWEN_CLI_PATH`` if set; otherwise relies on ``$PATH`` resolution.
    """
    return os.environ.get("QWEN_CLI_PATH", "qwen")


class QwenSessionManager(BaseSessionManager):
    """Manage a single Qwen Code conversation.

    Because ``qwen`` is one-shot, the lifecycle here is much smaller than
    Claude's: ``start()`` just records that the session exists; each
    ``send()`` spawns a fresh subprocess for the turn.  Resume is handled
    transparently via ``--resume <session-id>`` after the first turn.
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
        # The currently-running ``qwen`` subprocess for an in-flight turn.
        # None when idle.
        self._proc: asyncio.subprocess.Process | None = None
        # Optional handle to the reader task; used by the watchdog only.
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def provider_name(self) -> str:
        return "qwen"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _run_lifecycle(self) -> None:
        """Qwen has no persistent connection — the lifecycle is just bookkeeping.

        We mark the session IDLE immediately, signal connect_done, then
        block on ``_stop_requested``.  ``stop()`` triggers it, the finally
        block reaps any subprocess that's somehow still alive, and we exit.

        Remote (SSH) sessions get an ICMP reachability pre-probe before
        IDLE so a hibernated/offline target fails fast at start() instead
        of hanging on the first turn's SSH TCP timeout.  Mirrors Claude's
        ``_assert_ssh_reachable`` behavior; same rationale.
        """
        try:
            if self._config.ssh_host:
                reachable = await asyncio.get_running_loop().run_in_executor(
                    None, probe_host_reachable, self._config.ssh_host, 2.0,
                )
                if not reachable:
                    raise RemoteHostUnreachableError(
                        f"SSH host {self._config.ssh_host!r} did not reply to "
                        "ICMP ping; refusing to open SSH connection."
                    )
            if self._resume_id:
                self._provider_session_id = self._resume_id
            self._status = SessionStatus.IDLE
        except BaseException as e:
            self._connect_error = e
            self._connect_done.set()
            return

        self._connect_done.set()
        try:
            await self._stop_requested.wait()
        finally:
            await self._kill_proc()
            self._status = SessionStatus.DISCONNECTED

    async def _kill_proc(self) -> None:
        """Terminate any in-flight qwen subprocess.  Idempotent."""
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is not None:
            self._proc = None
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            self._proc = None
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "qwen subprocess pid=%s did not exit after SIGKILL", proc.pid,
                )
        self._proc = None

    async def interrupt(self) -> None:
        """Send SIGINT to the in-flight qwen subprocess.

        ``qwen`` (like most Node CLIs) treats SIGINT as a clean cancel,
        flushing whatever it has and exiting.  If no turn is running,
        no-op.
        """
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        self._status = SessionStatus.INTERRUPTED

    # ------------------------------------------------------------------
    # Sending messages
    # ------------------------------------------------------------------

    @property
    def subprocess_pid(self) -> int | None:
        """PID of the in-flight qwen subprocess, for the pool's orphan reaper.

        Note: Qwen's subprocess is short-lived (one per turn), so this is
        only non-None while a turn is actively running.
        """
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        """Send a prompt by spawning a fresh ``qwen`` subprocess for the turn.

        Yields the same normalized :class:`Event` types as Claude.  The
        subprocess is killed automatically if the iterator is closed mid-stream.
        """
        if self._status == SessionStatus.DISCONNECTED:
            raise RuntimeError("QwenSessionManager is not connected — call start() first")

        # If a previous turn's subprocess is still alive (e.g. from a
        # previous send() that wasn't fully drained), reap it first.
        if self._proc is not None and self._proc.returncode is None:
            await self._kill_proc()

        self._status = SessionStatus.STREAMING

        local_argv = self._build_argv()
        env = self._build_env()

        # SSH or local?  _maybe_wrap_with_ssh returns the argv that will
        # actually be exec'd plus the local cwd to spawn from (which is
        # the project_dir for local, irrelevant for SSH since the remote
        # cwd is set inside the SSH command via `cd`).
        argv, cwd = self._maybe_wrap_with_ssh(local_argv)

        # Pipe the prompt as a single stream-json line on stdin.
        stdin_payload = self._render_prompt(prompt).encode("utf-8")

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError as e:
            self._status = SessionStatus.IDLE
            # argv[0] is either the local qwen path or "ssh".  Either way
            # the missing binary points to a misconfiguration: qwen CLI
            # not installed locally, or ssh binary absent.
            raise RuntimeError(
                f"Executable not found ({argv[0]!r}). For local sessions, "
                "set QWEN_CLI_PATH or install qwen via npm.  For SSH "
                "sessions, make sure the local `ssh` client is installed."
            ) from e

        self._proc = proc

        # Notify the pool (if it installed a callback) that a new PID is
        # alive.  The pool tracks it for the orphan reaper so we can
        # SIGKILL leaks if this turn's normal cleanup paths get bypassed
        # (e.g. caller cancels the lifecycle task hard).
        if self._on_pid_spawn is not None:
            try:
                self._on_pid_spawn(proc.pid)
            except Exception:
                logger.exception("on_pid_spawn callback raised for pid=%d", proc.pid)

        # Feed stdin in a background task so we can stream stdout
        # concurrently.  Close stdin after writing so qwen knows there's
        # no more input.
        async def _feed_stdin() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(stdin_payload)
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # qwen exited before we finished writing

        stdin_task = asyncio.create_task(_feed_stdin(), name="qwen-stdin")
        stderr_task = asyncio.create_task(
            self._drain_stderr(proc), name="qwen-stderr",
        )

        last_tool_name: str | None = None
        last_tool_use_id: str | None = None
        text_buffer: list[str] = []
        thinking_buffer: list[str] = []

        # The whole thing is wrapped so we always reap the subprocess.
        try:
            async for event in self._stream_events(proc, prompt):
                if isinstance(event, ToolUse):
                    last_tool_name = event.tool_name
                    last_tool_use_id = event.tool_use_id
                elif isinstance(event, (ToolResult, TurnComplete)):
                    last_tool_name = None
                    last_tool_use_id = None
                elif isinstance(event, TextDelta):
                    text_buffer.append(event.text)
                elif isinstance(event, ThinkingDelta):
                    thinking_buffer.append(event.text)
                elif isinstance(event, SessionStalled):
                    # Attach the in-flight tool to the stall event.
                    event = SessionStalled(
                        elapsed_seconds=event.elapsed_seconds,
                        last_tool_name=last_tool_name,
                        last_tool_use_id=last_tool_use_id,
                    )
                yield event
        finally:
            stdin_task.cancel()
            try:
                await stdin_task
            except (asyncio.CancelledError, Exception):
                pass
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            if proc.returncode is None:
                await self._kill_proc()
            else:
                self._proc = None
            # Tell the pool the PID is done — keeps _tracked_pids clean
            # so the reaper doesn't have to scan dead pids every iteration.
            if self._on_pid_exit is not None:
                try:
                    self._on_pid_exit(proc.pid)
                except Exception:
                    logger.exception("on_pid_exit callback raised for pid=%d", proc.pid)
            self._event_inbox = None
            self._drain_pending_permissions()
            self._status = SessionStatus.IDLE

    async def _stream_events(
        self,
        proc: asyncio.subprocess.Process,
        prompt: str,
    ) -> AsyncIterator[Event]:
        """Consume ``proc.stdout`` line-by-line and yield normalized events.

        Includes the same stall/abandon watchdog Claude has.
        """
        assert proc.stdout is not None
        loop = asyncio.get_running_loop()

        # Adapter state for translating Anthropic-style stream events.
        text_buffer: list[str] = []
        thinking_buffer: list[str] = []
        # Each content_block_start carries metadata we need at content_block_stop time.
        block_meta: dict[int, dict] = {}

        turn_started_at = loop.time()
        last_event_at = turn_started_at
        stall_notified_at: float | None = None
        events_received = 0

        async def _read_one_line() -> bytes | None:
            return await proc.stdout.readline()

        while True:
            now = loop.time()
            if stall_notified_at is None:
                next_notice_in = max(0.0, _STALL_FIRST_NOTICE_S - (now - last_event_at))
            else:
                next_notice_in = max(
                    0.0, _STALL_REPEAT_INTERVAL_S - (now - stall_notified_at),
                )

            try:
                line = await asyncio.wait_for(
                    _read_one_line(), timeout=max(next_notice_in, 0.5),
                )
            except asyncio.TimeoutError:
                now = loop.time()
                if events_received == 0 and (now - turn_started_at) >= _TURN_ABANDON_S:
                    raise QwenAbandoned(now - turn_started_at)
                yield SessionStalled(
                    elapsed_seconds=now - last_event_at,
                    last_tool_name=None,
                    last_tool_use_id=None,
                )
                stall_notified_at = now
                continue

            if not line:
                # EOF — qwen exited.
                rc = await proc.wait()
                if rc != 0:
                    logger.warning(
                        "qwen exited with non-zero status %d for session %s",
                        rc, self._local_id,
                    )
                break

            last_event_at = loop.time()
            stall_notified_at = None
            events_received += 1

            try:
                obj = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                logger.warning("Could not parse qwen stdout line: %r", line[:200])
                continue

            for ev in self._translate_event(obj, block_meta, text_buffer, thinking_buffer):
                yield ev

    def _translate_event(
        self,
        obj: dict,
        block_meta: dict[int, dict],
        text_buffer: list[str],
        thinking_buffer: list[str],
    ) -> list[Event]:
        """Translate one qwen JSONL event into zero or more normalized events.

        Returned as a list so a single source event can fan out (e.g.
        content_block_stop → TextComplete + reset buffer).
        """
        out: list[Event] = []
        obj_type = obj.get("type", "")

        if obj_type == "system":
            subtype = obj.get("subtype", "")
            if subtype == "init":
                sid = obj.get("session_id")
                if sid and not self._provider_session_id:
                    self._provider_session_id = sid
            elif subtype == "compact":
                out.append(CompactComplete(
                    trigger=obj.get("trigger", "manual"),
                    summary=obj.get("summary", ""),
                ))
            return out

        if obj_type == "stream_event":
            event = obj.get("event", {})
            evt_type = event.get("type", "")

            if evt_type == "content_block_start":
                index = event.get("index", 0)
                block = event.get("content_block", {}) or {}
                block_meta[index] = block
                # Reset buffers for fresh blocks.
                if block.get("type") == "text":
                    text_buffer.clear()
                elif block.get("type") == "thinking":
                    thinking_buffer.clear()

            elif evt_type == "content_block_delta":
                delta = event.get("delta", {}) or {}
                dtype = delta.get("type", "")
                if dtype == "text_delta":
                    self._status = SessionStatus.STREAMING
                    text = delta.get("text", "")
                    if text:
                        text_buffer.append(text)
                        out.append(TextDelta(text=text))
                elif dtype == "thinking_delta":
                    self._status = SessionStatus.THINKING
                    text = delta.get("thinking", "") or delta.get("text", "")
                    if text:
                        thinking_buffer.append(text)
                        out.append(ThinkingDelta(text=text))
                # input_json_delta intentionally ignored — we use the
                # complete tool_use block emitted with the assistant message.

            elif evt_type == "content_block_stop":
                index = event.get("index", 0)
                meta = block_meta.pop(index, {})
                btype = meta.get("type", "")
                if btype == "text" and text_buffer:
                    out.append(TextComplete(text="".join(text_buffer)))
                    text_buffer.clear()
                elif btype == "thinking" and thinking_buffer:
                    out.append(ThinkingComplete(text="".join(thinking_buffer)))
                    thinking_buffer.clear()

            return out

        if obj_type == "assistant":
            # Complete assistant message — extract any tool_use blocks that
            # weren't streamed via stream_event (qwen sends them whole here).
            message = obj.get("message", {})
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    self._status = SessionStatus.TOOL_USE
                    out.append(ToolUse(
                        tool_use_id=block.get("id", ""),
                        tool_name=block.get("name", ""),
                        tool_input=block.get("input", {}) or {},
                    ))
                # text / thinking are already covered by stream_event deltas.
            return out

        if obj_type == "user":
            # Tool results arrive as user messages with tool_result blocks.
            message = obj.get("message", {})
            content = message.get("content", [])
            if not isinstance(content, list):
                return out
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        parts: list[str] = []
                        for item in result_content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                parts.append(item.get("text", ""))
                            elif isinstance(item, str):
                                parts.append(item)
                        result_content = "\n".join(parts)
                    out.append(ToolResult(
                        tool_use_id=block.get("tool_use_id", ""),
                        output=str(result_content) if result_content else "",
                        is_error=block.get("is_error", False),
                    ))
            return out

        if obj_type == "result":
            usage = obj.get("usage", {}) or {}
            num_turns = obj.get("num_turns", 0)
            self._turns += num_turns
            sid = obj.get("session_id")
            if sid:
                self._provider_session_id = sid
            out.append(TurnComplete(
                cost=None,  # qwen doesn't report cost
                usage=usage,
                num_turns=num_turns,
                session_id=sid or "",
                is_error=obj.get("is_error", False),
                result=obj.get("result"),
            ))
            return out

        return out

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Forward qwen stderr to the logger for visibility."""
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.warning("qwen stderr [%s]: %s", self._local_id, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("qwen stderr drain failed for %s", self._local_id)

    # ------------------------------------------------------------------
    # Subprocess construction
    # ------------------------------------------------------------------

    def _build_argv(self) -> list[str]:
        """Construct the ``qwen`` argv for this turn."""
        argv: list[str] = [
            _qwen_executable(),
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            # We approve tools via our own gate; tell qwen to auto-approve
            # everything else (the wrapper enforces the gate at a higher
            # level via the conversational checkpoint policy).
            "--approval-mode", "yolo",
            # Tag the channel so qwen's logs distinguish wrapper-driven runs.
            "--channel", "SDK",
        ]

        if self._provider_session_id:
            argv += ["--resume", self._provider_session_id]
        elif self._fork:
            # No native fork concept in qwen — closest analogue is a
            # fresh session with no --resume, which we already do.
            pass

        if self._config.model:
            argv += ["--model", self._config.model]

        if self._config.max_turns is not None:
            argv += ["--max-session-turns", str(self._config.max_turns)]

        return argv

    def _build_env(self) -> dict[str, str]:
        """Construct the env for the qwen subprocess."""
        env = dict(os.environ)
        # Strip Claude-specific markers so qwen doesn't get confused if
        # the wrapper itself was launched from inside Claude Code.
        env.pop("CLAUDECODE", None)
        return env

    def _maybe_wrap_with_ssh(
        self, local_argv: list[str],
    ) -> tuple[list[str], str | None]:
        """Return ``(argv, cwd)`` to feed ``asyncio.create_subprocess_exec``.

        For local sessions this is a no-op: returns *local_argv* and the
        configured ``project_dir`` as cwd.

        For SSH sessions, swaps ``local_argv[0]`` (the local qwen path)
        for the resolved remote path and wraps everything in an
        ``ssh ... "cd '<remote_dir>' && exec '<remote_qwen>' ..."`` argv.
        cwd is irrelevant in that case (the remote cwd is set by the
        SSH command itself), so we return ``None`` and let
        ``create_subprocess_exec`` inherit the parent's cwd.

        Qwen spawns a fresh subprocess per turn, which means each turn
        opens an SSH connection.  ``ControlMaster=auto`` +
        ``ControlPersist=60s`` (set in :func:`_ssh.build_ssh_argv`) keep
        a single TCP connection alive across the burst — without that
        we'd pay the SSH handshake on every turn.
        """
        if not self._config.ssh_host:
            return local_argv, self._config.project_dir

        target = SshTarget(
            host=self._config.ssh_host,
            user=self._config.ssh_user,
            key=self._config.ssh_key,
            control_path_prefix="qwen",
        )
        remote_qwen = resolve_remote_cli_path(
            "qwen",
            target,
            extra_search_paths=[
                "~/.local/bin/qwen",
                "/usr/local/bin/qwen",
                "/usr/bin/qwen",
            ],
        )
        remote_cmd = RemoteCommand(
            project_dir=self._config.project_dir,
            remote_cli=remote_qwen,
            # No env to forward: the remote machine has its own .env
            # (DASHSCOPE_API_KEY, ASSISTANT_PROVIDER, …) set up at install
            # time, just like Claude's remote installs.  Forwarding the
            # local env over SSH would either leak the local DASHSCOPE
            # key (visible in `ps` on the remote) or quietly miss other
            # vars the remote setup expects.
        )
        # ``local_argv[0]`` is the LOCAL qwen path (resolved by
        # :func:`_qwen_executable`); on the remote machine that path is
        # meaningless.  We drop it here — the remote path goes into
        # ``remote_cmd`` (which renders ``... exec '<remote_qwen>'``)
        # and the rest of the flags pass through as-is.
        # ``build_remote_argv`` shell-quotes each arg before joining, so
        # values with spaces or metacharacters survive across SSH.
        argv = build_remote_argv(
            target=target,
            remote_cmd=remote_cmd,
            remote_args=local_argv[1:],
        )
        return argv, None

    @staticmethod
    def _render_prompt(prompt: str) -> str:
        """Serialize a user prompt as a single stream-json line."""
        return json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }) + "\n"
