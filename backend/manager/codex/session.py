"""CodexSessionManager — drives OpenAI Codex through ``codex app-server``.

Shape: one persistent process per session, like Claude.  ``start()``
spawns ``codex app-server`` (newline-delimited JSON-RPC 2.0 on stdio),
runs the ``initialize`` handshake and opens the conversation with
``thread/start`` (or ``thread/resume`` / ``thread/fork`` for an existing
thread).  Each ``send()`` is one ``turn/start``; the app-server streams
notifications (``item/agentMessage/delta``, ``item/started``,
``turn/completed`` …) that a persistent receive loop translates into the
normalized :mod:`manager.types` events and hands to the active ``send()``
through ``_event_inbox`` — the same subscriber design as
:class:`manager.claude.session.ClaudeSessionManager`, including the
``(stream_id, seq)`` replay ring the pool uses for WS resume.

Ids
---

The Codex **thread id** (a UUIDv7) is the provider session id: it names
the rollout file (``sessions/YYYY/MM/DD/rollout-<ts>-<thread id>.jsonl``),
it is what ``thread/resume`` takes, and the pool keys history on it.

Per-run configuration
---------------------

Nothing is written to ``config.toml``.  Fixed overrides go on argv
(``-c project_doc_max_bytes=…`` so the 51 KB ``AGENTS.md`` is not
truncated, ``--disable plugins --disable apps --disable memories`` so the user's ChatGPT
plugins don't add ~10 KB of instructions to every turn); the harness
options become thread ``config`` overrides (``model_verbosity``,
``web_search``) or ``turn/start`` params (``effort``, ``summary``).  The
model is always passed explicitly: the chosen one, else the account's
default from ``model/list`` — a stale ``model`` in the user's
``config.toml`` must not break Archie.  In the Archie repo a new thread
also gets Archie's memory index as ``developerInstructions``
(:mod:`manager.memory_context`).

Approvals
---------

Like the Qwen and Gemini harnesses Codex runs unattended: approval policy
``never`` + sandbox ``danger-full-access`` (Archie trusts the cwd).  An
approval request that still arrives is accepted and logged.  There is no
``PermissionRequest`` gating for Codex yet.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NamedTuple

from .._ssh import (
    RemoteCommand,
    RemoteHostUnreachableError,
    SshTarget,
    build_remote_argv,
    probe_host_reachable,
    resolve_remote_cli_path,
)
from ..base_session import BaseSessionManager, SessionDeadError, TurnAbandoned
from ..config import ManagerConfig
from ..harness_catalog import EFFORT
from ..memory_context import memory_instructions
from ..types import (
    CompactComplete,
    Event,
    SessionStalled,
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
from . import catalog as cat
from . import home, items
from .rpc import CodexConnectionClosed, CodexRpc, CodexRpcError

logger = logging.getLogger(__name__)

# Same watchdog policy as the other harnesses.
_STALL_FIRST_NOTICE_S = 120.0
_STALL_REPEAT_INTERVAL_S = 60.0
_TURN_ABANDON_S = 240.0

_CONNECT_TIMEOUT_S = 60.0       # initialize + thread/start (first spawn can be slow)
_REQUEST_TIMEOUT_S = 30.0
_INTERRUPT_WAIT_S = 10.0
# app-server lines can be large (thread/resume echoes the thread; we pass
# excludeTurns, but command output and diffs still arrive in one line).
_STREAM_LIMIT = 64 * 1024 * 1024
_REPLAY_BUFFER_SIZE = 500

# Fixed per-process overrides (see module docstring).
_PROJECT_DOC_MAX_BYTES = 131_072
_DISABLED_FEATURES = ("plugins", "apps", "memories")

SANDBOX = "danger-full-access"
APPROVAL_POLICY = "never"


class CodexAbandoned(TurnAbandoned):
    """A Codex turn produced no events for so long the request never landed."""


class SequencedEvent(NamedTuple):
    """An event plus its dispatch seq — same contract as Claude's."""

    seq: int
    event: Event


def kill_codex_subprocess(pid: int, *, sigterm_grace_s: float = 0.5) -> bool:
    from .._proc import kill_subprocess
    return kill_subprocess(pid, comm_prefix="codex", sigterm_grace_s=sigterm_grace_s)


class CodexSessionManager(BaseSessionManager):
    """Manage one Codex thread through a dedicated ``codex app-server``."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        local_id: str | None = None,
        fork: bool = False,
        config: ManagerConfig | None = None,
    ) -> None:
        super().__init__(session_id=session_id, local_id=local_id, fork=fork, config=config)
        self._proc: asyncio.subprocess.Process | None = None
        self._rpc: CodexRpc | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._codex_home: Path | None = None
        self._thread_id: str | None = None
        self._model: str | None = None
        # model id → reasoning efforts it accepts (from model/list).
        self._model_efforts: dict[str, tuple[str, ...]] = {}

        # Receive loop (rpc.run) + termination signal read by the pool reaper.
        self._receive_task: asyncio.Task[None] | None = None
        self._receive_loop_done: asyncio.Event = asyncio.Event()
        self._termination_reason: TerminationReason | None = None
        self._termination_detail: str | None = None
        self._stopping = False

        # Replay ring + resume protocol (see ClaudeSessionManager).
        self._replay_buffer: deque[tuple[int, Event]] = deque(maxlen=_REPLAY_BUFFER_SIZE)
        self._stream_id: str | None = None
        self._next_seq: int = 0
        self._last_yielded_seq: int | None = None

        # Turn state, owned by the notification handler.
        self._active_turn_id: str | None = None
        self._turn_idle: asyncio.Event = asyncio.Event()
        self._turn_idle.set()
        self._turn_started: asyncio.Event = asyncio.Event()
        self._turn_usage: dict[str, Any] = {}
        self._turn_error: str | None = None
        self._last_agent_text: str | None = None
        self._open_texts: dict[str, list[str]] = {}
        self._open_reasoning: dict[str, dict[str, Any]] = {}
        self._open_tools: dict[str, str] = {}
        self._compacted_turn: str | None = None
        self._last_activity_at: float = 0.0

    @property
    def provider_name(self) -> str:
        return "codex"

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _pre_start_check(self) -> None:
        """Fail fast for an SSH host that does not answer ping."""
        if not self._config.ssh_host:
            return
        reachable = await asyncio.get_running_loop().run_in_executor(
            None, probe_host_reachable, self._config.ssh_host, 2.0,
        )
        if not reachable:
            raise RemoteHostUnreachableError(
                f"SSH host {self._config.ssh_host!r} did not reply to ICMP ping; "
                "refusing to open SSH connection."
            )

    async def _run_lifecycle(self) -> None:
        try:
            await self._connect()
        except BaseException as e:
            await self._teardown()
            self._connect_error = e
            self._connect_done.set()
            return
        self._connect_done.set()
        try:
            await self._stop_requested.wait()
        finally:
            await self._teardown()
            self._status = SessionStatus.DISCONNECTED

    async def _connect(self) -> None:
        argv, cwd, env = await self._spawn_spec()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=_STREAM_LIMIT,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"Executable not found ({argv[0]!r}). Install Codex with "
                "`npm install -g @openai/codex` or set CODEX_CLI_PATH."
            ) from e
        self._proc = proc
        assert proc.stdout is not None and proc.stdin is not None
        self._rpc = CodexRpc(
            proc.stdout, proc.stdin,
            on_notification=self._on_notification,
            on_request=self._on_server_request,
            name=f"codex[{self._local_id[:8]}]",
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc), name="codex-stderr")
        self._receive_loop_done.clear()
        self._receive_task = asyncio.create_task(
            self._receive_loop(), name=f"sm-receive-{self._local_id}",
        )

        rpc = self._rpc
        await rpc.request("initialize", {
            "clientInfo": {"name": home.CLIENT_NAME, "title": "Archie", "version": "1"},
        }, timeout=_CONNECT_TIMEOUT_S)
        await rpc.notify("initialized")

        default_model = await self._load_models()
        configured = self._config.model
        if configured and configured.lower().startswith("claude"):
            # ``.manager.json``'s ``model`` (a Claude id) reaches every
            # harness when no harness model is set; Codex would fail every
            # turn with "model is not supported".
            logger.warning(
                "Codex session %s: ignoring non-Codex model %r; using %s",
                self._local_id, configured, default_model,
            )
            configured = None
        self._model = configured or default_model

        params = self._thread_params()
        if not self._resume_id:
            # Archie's memory index (context/memory/MEMORY.md), read live at
            # thread start the way Claude Code's auto-memory loads it.  Codex
            # records it once, as a developer message at the head of the
            # rollout; a resumed or forked thread already carries it.
            memory = memory_instructions(self._config.project_dir)
            if memory:
                params["developerInstructions"] = memory
        if self._resume_id and self._fork:
            result = await rpc.request("thread/fork", {
                "threadId": self._resume_id, "excludeTurns": True, **params,
            }, timeout=_CONNECT_TIMEOUT_S)
        elif self._resume_id:
            result = await rpc.request("thread/resume", {
                "threadId": self._resume_id, "excludeTurns": True, **params,
            }, timeout=_CONNECT_TIMEOUT_S)
        else:
            result = await rpc.request("thread/start", params, timeout=_CONNECT_TIMEOUT_S)
        thread = (result or {}).get("thread") or {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError(f"Codex did not return a thread id: {result!r:.300}")
        self._thread_id = thread_id
        self._provider_session_id = thread_id
        self._model = (result or {}).get("model") or self._model

        self._stream_id = f"{self._local_id}:{int(time.time() * 1000)}"
        self._next_seq = 0
        self._replay_buffer.clear()
        self._status = SessionStatus.IDLE
        logger.info(
            "Codex session %s: thread %s (%s) model=%s home=%s",
            self._local_id, thread_id,
            "fork" if self._fork else "resume" if self._resume_id else "new",
            self._model, self._codex_home,
        )

    async def _load_models(self) -> str | None:
        """Ask ``model/list``; remember per-model efforts; return the default model."""
        assert self._rpc is not None
        try:
            result = await self._rpc.request(
                "model/list", {"includeHidden": True}, timeout=_REQUEST_TIMEOUT_S,
            )
        except (CodexRpcError, asyncio.TimeoutError) as e:
            logger.warning("Codex model/list failed for %s: %s", self._local_id, e)
            return None
        rows = (result or {}).get("data") or []
        models, default = cat.models_from_model_list(rows, include_hidden=True)
        self._model_efforts = {m.id: tuple(m.efforts or ()) for m in models}
        visible = [r for r in rows if isinstance(r, dict) and r.get("isDefault")]
        if visible:
            return visible[0].get("model") or visible[0].get("id")
        return default

    def _thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": self._config.project_dir,
            "sandbox": SANDBOX,
            "approvalPolicy": APPROVAL_POLICY,
        }
        if self._model:
            params["model"] = self._model
        overrides = self._config_overrides()
        if overrides:
            params["config"] = overrides
        return params

    def _options(self) -> dict[str, Any]:
        return dict(self._config.harness_options or {})

    def _config_overrides(self) -> dict[str, Any]:
        """Harness options that map onto Codex config keys."""
        opts = self._options()
        out: dict[str, Any] = {}
        if opts.get(cat.VERBOSITY) in cat.VERBOSITY_CHOICES:
            out["model_verbosity"] = opts[cat.VERBOSITY]
        if opts.get(cat.WEB_SEARCH) in cat.WEB_SEARCH_CHOICES:
            out["web_search"] = opts[cat.WEB_SEARCH]
        return out

    def _turn_params(self, prompt: str) -> dict[str, Any]:
        opts = self._options()
        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
        }
        effort = opts.get(EFFORT)
        if isinstance(effort, str) and effort:
            allowed = self._model_efforts.get(self._model or "")
            if allowed and effort not in allowed:
                logger.warning(
                    "Codex session %s: effort %r not supported by %s (%s); using the model default",
                    self._local_id, effort, self._model, ", ".join(allowed),
                )
            else:
                params["effort"] = effort
        summary = opts.get(cat.REASONING_SUMMARY)
        params["summary"] = summary if summary in cat.SUMMARY_CHOICES else cat.DEFAULT_SUMMARY
        return params

    async def _spawn_spec(self) -> tuple[list[str], str | None, dict[str, str]]:
        """``(argv, cwd, env)`` for the app-server process (local or over SSH)."""
        flags = ["app-server", "--listen", "stdio://", "-c", f"project_doc_max_bytes={_PROJECT_DOC_MAX_BYTES}"]
        for feature in _DISABLED_FEATURES:
            flags += ["--disable", feature]
        if not self._config.ssh_host:
            self._codex_home = (
                home.home_for_thread(self._resume_id) if self._resume_id else home.codex_home()
            )
            return [home.codex_executable(), *flags], self._config.project_dir, home.codex_env(self._codex_home)

        target = SshTarget(
            host=self._config.ssh_host,
            user=self._config.ssh_user,
            key=self._config.ssh_key,
            control_path_prefix="codex",
        )
        remote_codex = await asyncio.get_running_loop().run_in_executor(
            None, lambda: resolve_remote_cli_path("codex", target),
        )
        argv = build_remote_argv(
            target=target,
            remote_cmd=RemoteCommand(project_dir=self._config.project_dir, remote_cli="/bin/sh"),
            remote_args=["-c", _REMOTE_BOOTSTRAP, remote_codex, *flags],
        )
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        return argv, None, env

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.info("codex stderr [%s]: %s", self._local_id[:8], text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex stderr drain failed")

    async def _receive_loop(self) -> None:
        """Own the RPC read loop for the session's lifetime.

        When it ends without a ``stop()`` the app-server died: flag the
        termination so the pool's dead-session reaper closes the session
        and clients can offer a resume (the rollout on disk is intact).
        """
        assert self._rpc is not None
        try:
            await self._rpc.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("codex receive loop for %s crashed", self._local_id)
            self._termination_reason = TerminationReason.SUBPROCESS_CRASHED
            self._termination_detail = str(exc) or type(exc).__name__
        else:
            if not self._stopping and self._termination_reason is None:
                rc = None
                if self._proc is not None:
                    try:
                        rc = await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        rc = None
                tail = " | ".join(list(self._stderr_tail)[-3:])
                self._termination_reason = TerminationReason.SUBPROCESS_CRASHED
                self._termination_detail = (
                    f"codex app-server exited (code {rc})" + (f": {tail}" if tail else "")
                )
                logger.warning("Codex session %s: %s", self._local_id, self._termination_detail)
        finally:
            self._receive_loop_done.set()
            self._status = SessionStatus.DISCONNECTED
            self._stop_requested.set()
            self._turn_idle.set()
            inbox = self._event_inbox
            if inbox is not None:
                try:
                    inbox.put_nowait(TurnComplete(
                        session_id=self._thread_id or "",
                        is_error=True, result="receive_loop_exited",
                    ))
                except asyncio.QueueFull:
                    pass

    async def _teardown(self) -> None:
        """Close stdin, then SIGTERM / SIGKILL the process group.  Idempotent."""
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
                    try:
                        os.killpg(proc.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        try:
                            proc.send_signal(sig)
                        except ProcessLookupError:
                            break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=grace)
                        break
                    except asyncio.TimeoutError:
                        continue
        if self._receive_task is not None and not self._receive_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._receive_task), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._receive_task.cancel()
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        self._receive_task = None
        self._stderr_task = None

    async def interrupt(self) -> None:
        """``turn/interrupt`` the running turn; the process stays up."""
        rpc, thread_id = self._rpc, self._thread_id
        if rpc is not None and thread_id and not rpc.closed and not self._turn_idle.is_set():
            # Set before the request: turn/completed (which flips the status
            # back to IDLE) can arrive before the interrupt response does.
            self._status = SessionStatus.INTERRUPTED
            if self._active_turn_id is None:
                try:
                    await asyncio.wait_for(self._turn_started.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            turn_id = self._active_turn_id
            if turn_id:
                try:
                    await rpc.request(
                        "turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                        timeout=_REQUEST_TIMEOUT_S,
                    )
                except (CodexRpcError, CodexConnectionClosed, asyncio.TimeoutError) as e:
                    logger.warning("Codex turn/interrupt failed for %s: %s", self._local_id, e)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    @property
    def subprocess_pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    def _dead_error(self) -> SessionDeadError:
        return SessionDeadError(
            reason=self._termination_reason or TerminationReason.SUBPROCESS_CRASHED,
            detail=self._termination_detail,
        )

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        """Run one turn (``turn/start``) and yield its events until it completes."""
        if self._rpc is None or self._thread_id is None:
            raise RuntimeError("CodexSessionManager is not connected — call start() first")
        if self._receive_loop_done.is_set() or self._rpc.closed:
            raise self._dead_error()

        # A previous turn whose caller stopped listening may still be
        # running; stop it so its turn/completed can't end this send().
        if not self._turn_idle.is_set():
            await self.interrupt()
            try:
                await asyncio.wait_for(self._turn_idle.wait(), timeout=_INTERRUPT_WAIT_S)
            except asyncio.TimeoutError:
                logger.warning("Codex session %s: previous turn did not stop", self._local_id)

        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue[SequencedEvent | Event] = asyncio.Queue()
        self._event_inbox = inbox
        self._begin_turn()

        try:
            try:
                result = await self._rpc.request(
                    "turn/start", self._turn_params(prompt), timeout=_REQUEST_TIMEOUT_S,
                )
            except CodexConnectionClosed:
                raise self._dead_error()
            except (CodexRpcError, asyncio.TimeoutError) as e:
                message = str(e) or type(e).__name__
                self._turn_idle.set()
                for ev in self._error_events(message):
                    yield ev
                yield TurnComplete(session_id=self._thread_id, is_error=True, result=message, num_turns=1)
                return
            turn = (result or {}).get("turn") or {}
            if turn.get("id") and self._active_turn_id is None:
                self._active_turn_id = turn["id"]
                self._turn_started.set()
            if self._status != SessionStatus.DISCONNECTED:
                self._status = SessionStatus.STREAMING

            turn_started_at = loop.time()
            last_msg_at = turn_started_at
            stall_notified_at: float | None = None
            received = 0
            last_tool: tuple[str, str] | None = None
            while True:
                now = loop.time()
                if stall_notified_at is None:
                    wait = _STALL_FIRST_NOTICE_S - (now - last_msg_at)
                else:
                    wait = _STALL_REPEAT_INTERVAL_S - (now - stall_notified_at)
                try:
                    item = await asyncio.wait_for(inbox.get(), timeout=max(wait, 0.5))
                except asyncio.TimeoutError:
                    now = loop.time()
                    # Notifications that produce no event (command output
                    # deltas, MCP progress) still prove the turn is alive.
                    if self._last_activity_at > last_msg_at:
                        last_msg_at = self._last_activity_at
                        stall_notified_at = None
                        continue
                    if received == 0 and now - turn_started_at >= _TURN_ABANDON_S:
                        raise CodexAbandoned(now - turn_started_at)
                    self._last_yielded_seq = None
                    yield SessionStalled(
                        elapsed_seconds=now - last_msg_at,
                        last_tool_name=last_tool[1] if last_tool else None,
                        last_tool_use_id=last_tool[0] if last_tool else None,
                    )
                    stall_notified_at = now
                    continue

                last_msg_at = loop.time()
                stall_notified_at = None
                received += 1
                if isinstance(item, SequencedEvent):
                    event = item.event
                    self._last_yielded_seq = item.seq
                else:
                    event = item
                    self._last_yielded_seq = None
                if isinstance(event, ToolUse):
                    last_tool = (event.tool_use_id, event.tool_name)
                elif isinstance(event, (ToolResult, TurnComplete)):
                    last_tool = None
                yield event
                if isinstance(event, TurnComplete):
                    break
        finally:
            if self._event_inbox is inbox:
                self._event_inbox = None
            self._drain_pending_permissions()
            self._turns += 1
            if self._status not in (SessionStatus.INTERRUPTED, SessionStatus.DISCONNECTED):
                self._status = SessionStatus.IDLE

    def _begin_turn(self) -> None:
        self._active_turn_id = None
        self._turn_started.clear()
        self._turn_idle.clear()
        self._turn_usage = {}
        self._turn_error = None
        self._last_agent_text = None
        self._open_texts.clear()
        self._open_reasoning.clear()
        self._open_tools.clear()

    @staticmethod
    def _error_events(message: str) -> list[Event]:
        text = f"Codex error: {message}"
        return [TextDelta(text=text), TextComplete(text=text)]

    # ------------------------------------------------------------------
    # Replay / resume protocol (same as ClaudeSessionManager)
    # ------------------------------------------------------------------

    def _inject_event(self, event: Event) -> None:
        seq = self._next_seq
        self._next_seq += 1
        self._replay_buffer.append((seq, event))
        inbox = self._event_inbox
        if inbox is not None:
            try:
                inbox.put_nowait(SequencedEvent(seq=seq, event=event))
            except asyncio.QueueFull:
                logger.warning("send() inbox full for session %s; event dropped", self._local_id)

    def replay_recent_events(self) -> list[tuple[int, Event]]:
        return list(self._replay_buffer)

    def replay_after(
        self, stream_id: str | None, after_seq: int | None,
    ) -> tuple[str, list[tuple[int, Event]]]:
        if stream_id is None or after_seq is None or self._stream_id is None:
            return "ok", []
        if stream_id != self._stream_id:
            return "mismatch", []
        if not self._replay_buffer:
            return ("mismatch", []) if after_seq >= self._next_seq else ("ok", [])
        oldest, latest = self._replay_buffer[0][0], self._replay_buffer[-1][0]
        if after_seq >= latest:
            return "ok", []
        if after_seq < oldest - 1:
            return "overflow", []
        return "ok", [(s, e) for s, e in self._replay_buffer if s > after_seq]

    @property
    def stream_id(self) -> str | None:
        return self._stream_id

    @property
    def last_yielded_seq(self) -> int | None:
        return self._last_yielded_seq

    # ------------------------------------------------------------------
    # Inbound: notifications → events
    # ------------------------------------------------------------------

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if thread_id and self._thread_id and thread_id != self._thread_id:
            return  # another thread (e.g. a sub-agent) on this server
        self._last_activity_at = asyncio.get_running_loop().time()
        for event in self._translate(method, params):
            if isinstance(event, TextDelta):
                self._status = SessionStatus.STREAMING
            elif isinstance(event, ThinkingDelta):
                self._status = SessionStatus.THINKING
            elif isinstance(event, ToolUse):
                self._status = SessionStatus.TOOL_USE
            self._inject_event(event)
            if isinstance(event, TurnComplete) and self._status != SessionStatus.DISCONNECTED:
                self._status = SessionStatus.IDLE

    def _translate(self, method: str, params: dict[str, Any]) -> list[Event]:
        out: list[Event] = []
        if method == "item/agentMessage/delta" or method == "item/plan/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                self._open_texts.setdefault(str(params.get("itemId")), []).append(delta)
                out.append(TextDelta(text=delta))
        elif method == "item/reasoning/summaryTextDelta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                state = self._open_reasoning.setdefault(str(params.get("itemId")), {"summary": False, "parts": 0})
                state["summary"] = True
                out.append(ThinkingDelta(text=delta))
        elif method == "item/reasoning/summaryPartAdded":
            state = self._open_reasoning.setdefault(str(params.get("itemId")), {"summary": False, "parts": 0})
            state["parts"] += 1
            if state["parts"] > 1:
                out.append(ThinkingDelta(text="\n\n"))
        elif method == "item/reasoning/textDelta":
            delta = params.get("delta")
            state = self._open_reasoning.setdefault(str(params.get("itemId")), {"summary": False, "parts": 0})
            if isinstance(delta, str) and delta and not state["summary"]:
                out.append(ThinkingDelta(text=delta))
        elif method == "item/started":
            item = params.get("item") or {}
            call = items.tool_call(item)
            if call is not None and item.get("id"):
                out.append(ToolUse(tool_use_id=item["id"], tool_name=call[0], tool_input=call[1]))
                self._open_tools[item["id"]] = call[0]
        elif method == "item/completed":
            out.extend(self._item_completed(params.get("item") or {}))
        elif method == "turn/plan/updated":
            tool_id = f"plan-{params.get('turnId', '')}-{self._next_seq}"
            out.append(ToolUse(tool_use_id=tool_id, tool_name="TodoWrite", tool_input=items.plan_todos(params.get("plan"))))
            out.append(ToolResult(tool_use_id=tool_id, output=params.get("explanation") or "Plan updated"))
        elif method == "turn/started":
            turn = params.get("turn") or {}
            if turn.get("id"):
                self._active_turn_id = turn["id"]
                self._turn_started.set()
        elif method == "thread/tokenUsage/updated":
            usage = (params.get("tokenUsage") or {})
            last = usage.get("last") or {}
            cached = int(last.get("cachedInputTokens") or 0)
            self._turn_usage = {
                "input_tokens": max(int(last.get("inputTokens") or 0) - cached, 0),
                "cache_read_input_tokens": cached,
                "output_tokens": int(last.get("outputTokens") or 0),
                "reasoning_output_tokens": int(last.get("reasoningOutputTokens") or 0),
                "total_tokens": int(last.get("totalTokens") or 0),
            }
            if usage.get("modelContextWindow"):
                self._turn_usage["context_window"] = usage["modelContextWindow"]
        elif method == "error":
            err = params.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else str(err)
            if params.get("willRetry"):
                logger.info("Codex session %s: retrying after error: %s", self._local_id, message)
            else:
                self._turn_error = message or "unknown error"
                logger.warning("Codex session %s error: %s", self._local_id, message)
        elif method == "thread/compacted":
            out.extend(self._compacted())
        elif method == "turn/completed":
            out.extend(self._turn_completed(params.get("turn") or {}))
        elif method == "model/rerouted":
            logger.info("Codex session %s: model rerouted: %s", self._local_id, params)
        return out

    def _item_completed(self, item: dict[str, Any]) -> list[Event]:
        out: list[Event] = []
        item_id = str(item.get("id") or "")
        kind = items.item_kind(item)
        if kind in ("agentMessage", "plan"):
            self._open_texts.pop(item_id, None)
            text = items.message_text(item)
            if text:
                out.append(TextComplete(text=text))
                if kind == "agentMessage":
                    self._last_agent_text = text
        elif kind == "reasoning":
            self._open_reasoning.pop(item_id, None)
            text = items.reasoning_text(item)
            if text:
                out.append(ThinkingComplete(text=text))
        elif kind == "contextCompaction":
            out.extend(self._compacted())
        else:
            call = items.tool_call(item)
            if call is not None and item_id:
                if item_id not in self._open_tools:
                    out.append(ToolUse(tool_use_id=item_id, tool_name=call[0], tool_input=call[1]))
                self._open_tools.pop(item_id, None)
                output, is_error = items.tool_result(item)
                out.append(ToolResult(tool_use_id=item_id, output=output, is_error=is_error))
        return out

    def _compacted(self) -> list[Event]:
        key = self._active_turn_id or "-"
        if self._compacted_turn == key:
            return []
        self._compacted_turn = key
        return [CompactComplete(trigger="auto")]

    def _turn_completed(self, turn: dict[str, Any]) -> list[Event]:
        out: list[Event] = []
        status = str(turn.get("status") or "completed")
        # Flush what the turn left open so no card spins forever.
        for chunks in self._open_texts.values():
            if chunks:
                out.append(TextComplete(text="".join(chunks)))
        self._open_texts.clear()
        self._open_reasoning.clear()
        for tool_id in list(self._open_tools):
            out.append(ToolResult(tool_use_id=tool_id, output=status, is_error=True))
        self._open_tools.clear()

        err = turn.get("error")
        error_message = (err.get("message") if isinstance(err, dict) else None) or self._turn_error
        is_error = status == "failed"
        if is_error:
            error_message = error_message or "turn failed"
            out.extend(self._error_events(error_message))
            result: str | None = error_message
        elif status == "interrupted":
            result = "interrupted"
        else:
            result = self._last_agent_text
        out.append(TurnComplete(
            cost=None,
            usage=dict(self._turn_usage),
            num_turns=1,
            session_id=self._thread_id or "",
            is_error=is_error,
            result=result,
        ))
        self._active_turn_id = None
        self._turn_idle.set()
        return out

    # ------------------------------------------------------------------
    # Inbound: server → client requests
    # ------------------------------------------------------------------

    async def _on_server_request(self, method: str, params: dict[str, Any]) -> Any:
        """Answer approvals automatically (approval policy is ``never``, so
        these should not arrive) and decline anything interactive."""
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            logger.warning("Codex session %s: auto-accepting %s (%s)", self._local_id, method,
                           params.get("command") or params.get("reason") or params.get("itemId"))
            return {"decision": "accept"}
        if method in ("execCommandApproval", "applyPatchApproval"):
            logger.warning("Codex session %s: auto-approving %s", self._local_id, method)
            return {"decision": "approved"}
        if method == "item/permissions/requestApproval":
            logger.warning("Codex session %s: auto-granting permissions %s", self._local_id,
                           params.get("permissions"))
            return {"permissions": params.get("permissions") or {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            logger.warning("Codex session %s: no UI for requestUserInput; answering empty", self._local_id)
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            logger.warning("Codex session %s: declining MCP elicitation", self._local_id)
            return {"action": "decline"}
        logger.warning("Codex session %s: unsupported server request %s", self._local_id, method)
        raise CodexRpcError(f"Archie does not support {method}", -32601)


# Remote bootstrap for SSH sessions: run Codex with the remote's dedicated
# Archie home when it holds a login, and put the CLI's own directory on PATH
# (the npm shim needs ``node``, which lives next to it under nvm).  ``$0`` is
# the remote codex path; the remaining args are the app-server flags.
_REMOTE_BOOTSTRAP = (
    'h="$HOME/.codex-archie"; d=$(dirname "$0"); '
    'if [ -f "$h/auth.json" ]; then CODEX_HOME="$h" PATH="$d:$PATH" exec "$0" "$@"; '
    'else PATH="$d:$PATH" exec "$0" "$@"; fi'
)
