"""GeminiSessionManager — wraps a single Google Gemini CLI conversation.

Shape: spawn-per-turn, just like Qwen.  Each ``send()`` spawns a fresh
``gemini -p '<prompt>' --output-format stream-json --session-id <uuid>``
subprocess, parses its stdout line-by-line, and yields normalized events.

Why not stream-json on stdin like Qwen?  The Gemini CLI's headless
``--prompt`` flag takes the prompt directly on argv (no stdin protocol)
and emits the same stream-json shape on stdout.  Argv is the simplest
path — ``asyncio.create_subprocess_exec`` doesn't shell-interpret args,
so even prompts with quotes/newlines survive.

SSH remote execution
--------------------

When ``ManagerConfig.ssh_host`` is set the CLI runs on the remote host,
wrapped by an ``ssh ...`` argv produced via :mod:`manager._ssh` — same
pattern as :class:`manager.qwen.session.QwenSessionManager`.  The
remote argv shape Gemini needs (``gemini --prompt 'text' --skip-trust
...``) is identical to Qwen's in structure (we build the whole argv
ourselves, no ``"$@"`` forwarding), so :func:`build_remote_argv` does
the right thing without provider-specific glue.

Trust prompt
------------

Gemini CLI defaults to refusing headless runs in directories it doesn't
"trust."  We pass ``--skip-trust`` on every invocation so the wrapper
doesn't hang waiting for a confirmation that has no UI.  Same outcome as
setting ``GEMINI_CLI_TRUST_WORKSPACE=true``; the flag is simpler.

Resume + session ids
--------------------

We generate session ids ourselves (UUIDv4) and pass them via
``--session-id`` so the CLI uses ours instead of inventing one.  On
subsequent turns we pass ``--resume <session-id>``.  The CLI happily
re-uses the same id across spawns because that's what its own
``--list-sessions`` machinery expects.

Storage layout
--------------

The CLI writes session JSONL to
``~/.gemini/tmp/<project-label>/chats/session-<short-iso>-<uuid-prefix>.jsonl``.
``<project-label>`` is the value the CLI assigns to ``cwd`` inside
``~/.gemini/projects.json``.  This means our session manager and the
JSONL adapter must agree on cwd: we pass ``self._config.project_dir``
as the subprocess cwd so the CLI lands files in the directory we expect.
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
    Event,
    SessionStalled,
    SessionStatus,
    TextComplete,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
)

logger = logging.getLogger(__name__)


# Same watchdog policy as Qwen: warn after 2 min of silence, repeat every 60s.
_STALL_FIRST_NOTICE_S = 120.0
_STALL_REPEAT_INTERVAL_S = 60.0
# Abandoned-turn detection — produced zero events for this long → give up.
_TURN_ABANDON_S = 240.0


class GeminiAbandoned(TurnAbandoned):
    """Raised when a Gemini turn produced no events for so long the request
    almost certainly never landed.

    Inherits :class:`manager.base_session.TurnAbandoned` so catch sites
    that want to handle all providers uniformly can do so with a single
    ``except TurnAbandoned`` clause.
    """


def _gemini_executable() -> str:
    """Resolve the path to the ``gemini`` CLI.

    Honors ``GEMINI_CLI_PATH`` if set; otherwise relies on ``$PATH`` resolution.
    """
    return os.environ.get("GEMINI_CLI_PATH", "gemini")


class GeminiSessionManager(BaseSessionManager):
    """Manage a single Google Gemini CLI conversation.

    Because ``gemini -p`` is one-shot, the lifecycle here is much smaller
    than Claude's: ``start()`` just records that the session exists; each
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
        # The currently-running ``gemini`` subprocess for an in-flight turn.
        # None when idle.
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def provider_name(self) -> str:
        return "gemini"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _run_lifecycle(self) -> None:
        """Gemini has no persistent connection — the lifecycle is just bookkeeping.

        Mirror of Qwen's lifecycle: mark IDLE, signal connect_done, block
        on _stop_requested, reap on shutdown.  If no session id was passed
        in (fresh session), we generate one here so the very first send()
        can pin it via ``--session-id``.

        Remote (SSH) sessions get an ICMP reachability pre-probe before
        IDLE so a hibernated/offline target fails fast at start() instead
        of hanging on the first turn's SSH TCP timeout.  Same rationale
        as Qwen's lifecycle and Claude's ``_assert_ssh_reachable``.

        We also pre-warm two slow things before signaling connect_done,
        so the cost lands at tab-open rather than on the user's first
        prompt.  See :meth:`_prewarm` for the rationale.
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
            else:
                # Generate up front so the first send() can stamp it via
                # ``--session-id`` on argv.  Without this the CLI would
                # invent its own id, and we'd lose track of where the
                # JSONL landed until we sniffed the stream-json init event.
                self._provider_session_id = str(uuid.uuid4())
            # Move the slow first-prompt costs (remote `which` probe,
            # local Node startup) here so start() pays them once instead
            # of the user staring at an unresponsive prompt.  Failures
            # are logged but NOT raised — a flaky warmup shouldn't block
            # the session from opening.
            await self._prewarm()
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
        """Terminate any in-flight gemini subprocess.  Idempotent."""
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
                    "gemini subprocess pid=%s did not exit after SIGKILL", proc.pid,
                )
        self._proc = None

    async def interrupt(self) -> None:
        """Send SIGINT to the in-flight gemini subprocess."""
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
        """PID of the in-flight gemini subprocess (only set while a turn
        is running, since Gemini is one-shot per turn)."""
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    async def send(self, prompt: str) -> AsyncIterator[Event]:
        """Send a prompt by spawning a fresh ``gemini`` subprocess.

        Yields the same normalized :class:`Event` types as the other
        harnesses.  The subprocess is killed automatically if the
        iterator is closed mid-stream.
        """
        if self._status == SessionStatus.DISCONNECTED:
            raise RuntimeError(
                "GeminiSessionManager is not connected — call start() first"
            )

        # If a previous turn's subprocess is still alive (e.g. from a
        # previous send() that wasn't fully drained), reap it first.
        if self._proc is not None and self._proc.returncode is None:
            await self._kill_proc()

        self._status = SessionStatus.STREAMING

        local_argv = self._build_argv(prompt)
        env = self._build_env()

        # SSH or local?  Mirrors Qwen's _maybe_wrap_with_ssh contract:
        # returns the argv that will actually be exec'd plus the local
        # cwd (project_dir for local sessions, None for SSH where the
        # remote `cd` is embedded in the SSH command).
        argv, cwd = self._maybe_wrap_with_ssh(local_argv)

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
            # argv[0] is either the local gemini path or "ssh".  Either
            # way the missing binary points to a misconfiguration.
            raise RuntimeError(
                f"Executable not found ({argv[0]!r}). For local sessions, "
                "set GEMINI_CLI_PATH or install gemini via `npm install -g "
                "@google/gemini-cli`.  For SSH sessions, make sure the "
                "local `ssh` client is installed."
            ) from e

        self._proc = proc

        # Notify the pool that a new PID is alive.
        if self._on_pid_spawn is not None:
            try:
                self._on_pid_spawn(proc.pid)
            except Exception:
                logger.exception("on_pid_spawn callback raised for pid=%d", proc.pid)

        # Close stdin immediately — the prompt is on argv via --prompt.
        # If we leave stdin open the CLI might wait for input that won't
        # come.
        try:
            assert proc.stdin is not None
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        stderr_task = asyncio.create_task(
            self._drain_stderr(proc), name="gemini-stderr",
        )

        # The whole thing is wrapped so we always reap the subprocess.
        try:
            async for event in self._stream_events(proc):
                yield event
        finally:
            # Stop the stderr drainer.
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

            # Reap.
            if proc.returncode is None:
                await self._kill_proc()
            else:
                self._proc = None

            if self._on_pid_exit is not None:
                try:
                    self._on_pid_exit(proc.pid)
                except Exception:
                    logger.exception(
                        "on_pid_exit callback raised for pid=%d", proc.pid,
                    )

            self._turns += 1
            if self._status != SessionStatus.INTERRUPTED:
                self._status = SessionStatus.IDLE

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Drain stderr in the background, logging anything noisy.

        The Gemini CLI is chatty on stderr (terminal-color warnings,
        ripgrep-not-found, rate-limit retries).  We log everything but
        don't surface it to the caller — the stream-json on stdout is
        authoritative.
        """
        assert proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                # Log everything except known-benign noise.
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                # Filter out the terminal-warning lines that don't carry
                # useful debugging info.
                low = text.lower()
                if (
                    "256-color" in low
                    or "ripgrep" in low
                    or "yolo mode" in low
                    or "shell cwd was reset" in low
                ):
                    logger.debug("gemini stderr: %s", text)
                else:
                    logger.info("gemini stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("gemini stderr drain failed")

    async def _stream_events(
        self, proc: asyncio.subprocess.Process,
    ) -> AsyncIterator[Event]:
        """Consume ``proc.stdout`` line-by-line and yield normalized events.

        Includes the same stall/abandon watchdog the other harnesses have.
        """
        assert proc.stdout is not None
        loop = asyncio.get_running_loop()

        turn_started_at = loop.time()
        last_event_at = turn_started_at
        stall_notified_at: float | None = None
        events_received = 0

        # Streaming-text accumulator — Gemini sends assistant text in
        # multiple ``{"type":"message", "role":"assistant", "delta":true}``
        # events; we yield TextDelta for each chunk and TextComplete at
        # the end of the turn.
        text_buffer: list[str] = []

        # Track tool-use ids by name so tool_result lines (which carry
        # only the id) can be paired up if needed.
        tool_uses_in_flight: dict[str, str] = {}  # tool_id → tool_name

        async def _read_one_line() -> bytes:
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
                    raise GeminiAbandoned(now - turn_started_at)
                last_tool_name = (
                    next(iter(tool_uses_in_flight.values()), None)
                    if tool_uses_in_flight
                    else None
                )
                last_tool_use_id = (
                    next(iter(tool_uses_in_flight.keys()), None)
                    if tool_uses_in_flight
                    else None
                )
                yield SessionStalled(
                    elapsed_seconds=now - last_event_at,
                    last_tool_name=last_tool_name,
                    last_tool_use_id=last_tool_use_id,
                )
                stall_notified_at = now
                continue

            if not line:
                # EOF — gemini exited.  Flush any pending streaming text
                # as a TextComplete so the UI sees the final assistant
                # message even if the result event was missing.
                if text_buffer:
                    yield TextComplete(text="".join(text_buffer))
                    text_buffer.clear()
                rc = await proc.wait()
                if rc != 0 and self._status != SessionStatus.INTERRUPTED:
                    logger.warning(
                        "gemini exited with non-zero status %d for session %s",
                        rc, self._local_id,
                    )
                break

            last_event_at = loop.time()
            stall_notified_at = None
            events_received += 1

            line_text = line.decode("utf-8", errors="replace").strip()
            if not line_text:
                continue

            # Skip non-JSON stderr-like lines that occasionally end up on
            # stdout (the CLI prints a "Shell cwd was reset" trailer on
            # stdout in some builds).
            if not line_text.startswith("{"):
                logger.debug("gemini stdout (non-JSON): %s", line_text[:200])
                continue

            try:
                obj = json.loads(line_text)
            except json.JSONDecodeError:
                logger.warning(
                    "Could not parse gemini stdout line: %r", line_text[:200],
                )
                continue

            for ev in self._translate_event(obj, text_buffer, tool_uses_in_flight):
                yield ev

    def _translate_event(
        self,
        obj: dict,
        text_buffer: list[str],
        tool_uses_in_flight: dict[str, str],
    ) -> list[Event]:
        """Translate one Gemini stream-json event into zero or more events.

        Event vocabulary
        ----------------
        - ``init``: session id + model.  Capture session id if we didn't
          already pin it via ``--session-id``.
        - ``message`` (``role="user"``): echo of the user prompt — ignore
          (the wrapper already broadcast it).
        - ``message`` (``role="assistant"``, ``delta=true``): one streamed
          text chunk.
        - ``tool_use``: model wants to call a tool.
        - ``tool_result``: tool returned a value.
        - ``result``: terminal event; yield TextComplete (if text was
          accumulated) and TurnComplete.
        """
        out: list[Event] = []
        obj_type = obj.get("type", "")

        if obj_type == "init":
            sid = obj.get("session_id")
            if sid and not self._provider_session_id:
                self._provider_session_id = sid
            return out

        if obj_type == "message":
            role = obj.get("role", "")
            if role == "user":
                # Wrapper already broadcast the user message — skip.
                return out
            if role == "assistant":
                content = obj.get("content", "")
                if not isinstance(content, str) or not content:
                    return out
                self._status = SessionStatus.STREAMING
                text_buffer.append(content)
                out.append(TextDelta(text=content))
            return out

        if obj_type == "tool_use":
            tool_name = obj.get("tool_name", "")
            tool_id = obj.get("tool_id", "")
            params = obj.get("parameters", {}) or {}
            if tool_id:
                tool_uses_in_flight[tool_id] = tool_name
            # Flush any accumulated text first so the UI shows
            # "thinking text…" then the tool call rather than the call
            # showing up before the text it was preceded by.
            if text_buffer:
                out.append(TextComplete(text="".join(text_buffer)))
                text_buffer.clear()
            out.append(ToolUse(
                tool_use_id=tool_id,
                tool_name=tool_name,
                tool_input=params if isinstance(params, dict) else {},
            ))
            return out

        if obj_type == "tool_result":
            tool_id = obj.get("tool_id", "")
            status = obj.get("status", "success")
            is_error = status == "error"
            # Output / error fields differ shape.  ``output`` for
            # success, ``error.message`` for errors.
            if is_error:
                err = obj.get("error", {})
                output = (
                    err.get("message", "") if isinstance(err, dict) else str(err)
                )
            else:
                output = obj.get("output", "")
            tool_uses_in_flight.pop(tool_id, None)
            out.append(ToolResult(
                tool_use_id=tool_id,
                output=str(output) if output is not None else "",
                is_error=is_error,
            ))
            return out

        if obj_type == "result":
            # End of turn.  Flush any accumulated text and emit
            # TurnComplete with usage stats if present.
            if text_buffer:
                out.append(TextComplete(text="".join(text_buffer)))
                text_buffer.clear()
            stats = obj.get("stats", {}) or {}
            usage = {}
            if isinstance(stats, dict):
                # Normalize a few common keys.  The full stats blob is
                # noisy (per-model breakdowns); we surface only the
                # rolled-up tokens.
                if "input_tokens" in stats:
                    usage["input_tokens"] = stats.get("input_tokens", 0)
                if "output_tokens" in stats:
                    usage["output_tokens"] = stats.get("output_tokens", 0)
                if "total_tokens" in stats:
                    usage["total_tokens"] = stats.get("total_tokens", 0)
                if "cached" in stats:
                    usage["cache_read_input_tokens"] = stats.get("cached", 0)
            out.append(TurnComplete(usage=usage))
            return out

        # Anything else is informational; log and skip.
        logger.debug("Unhandled gemini event type: %s", obj_type)
        return out

    # ------------------------------------------------------------------
    # Argv / env construction
    # ------------------------------------------------------------------

    def _build_argv(self, prompt: str) -> list[str]:
        """Construct the ``gemini`` argv for this turn."""
        argv: list[str] = [
            _gemini_executable(),
            # ``--prompt`` runs in non-interactive headless mode.  Prompt
            # is the next positional argument.
            "--prompt", prompt,
            # ``--skip-trust`` so the CLI doesn't refuse to run headless
            # in directories it doesn't know about.  We trust the cwd
            # ourselves at the wrapper level.
            "--skip-trust",
            "--output-format", "stream-json",
            # Tool approval is enforced at the wrapper level via the
            # conversational-checkpoint policy; let the CLI auto-approve
            # everything so it doesn't hang waiting for stdin input.
            "--approval-mode", "yolo",
        ]

        if self._provider_session_id:
            # On the first turn we PIN the id we generated in
            # _run_lifecycle; on subsequent turns we additionally pass
            # --resume so the CLI knows to load prior turns from disk.
            if self._turns == 0:
                argv += ["--session-id", self._provider_session_id]
            else:
                argv += ["--resume", self._provider_session_id]

        if self._config.model:
            argv += ["--model", self._config.model]

        return argv

    def _build_env(self) -> dict[str, str]:
        """Construct the env for the gemini subprocess."""
        env = dict(os.environ)
        # Belt + suspenders: also set the trust env var (in case the
        # CLI's --skip-trust flag is ever renamed/removed).
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        # Strip markers from other harnesses so gemini doesn't get
        # confused if the wrapper itself was launched from inside one.
        env.pop("CLAUDECODE", None)
        return env

    def _maybe_wrap_with_ssh(
        self, local_argv: list[str],
    ) -> tuple[list[str], str | None]:
        """Return ``(argv, cwd)`` to feed ``asyncio.create_subprocess_exec``.

        Local sessions: returns *local_argv* and the configured
        ``project_dir`` as cwd — no SSH involvement.

        SSH sessions: swaps ``local_argv[0]`` (the local gemini path)
        for the resolved remote path and wraps everything in an
        ``ssh ... "cd '<remote_dir>' && exec '<remote_gemini>' ..."``
        argv.  cwd is irrelevant in that case (the remote cwd is set
        inside the SSH command), so we return ``None`` and let
        ``create_subprocess_exec`` inherit the parent's cwd.

        Mirror of :meth:`manager.qwen.session.QwenSessionManager._maybe_wrap_with_ssh`
        — the two providers share the same argv shape (full argv built
        by the wrapper, no ``"$@"`` forwarding), so
        :func:`build_remote_argv` works for both.  The Claude path is
        different because the SDK builds its own argv, hence the
        wrapper-script detour in :mod:`manager._ssh`.

        Gemini spawns a fresh subprocess per turn, which means each
        turn opens an SSH connection.  ``ControlMaster=auto`` +
        ``ControlPersist=60s`` (set in :func:`_ssh.build_ssh_argv`)
        keep a single TCP connection alive across a burst — without
        that we'd pay the SSH handshake on every turn.
        """
        if not self._config.ssh_host:
            return local_argv, self._config.project_dir

        target = SshTarget(
            host=self._config.ssh_host,
            user=self._config.ssh_user,
            key=self._config.ssh_key,
            # Distinct ControlMaster socket per provider so two providers
            # on the same host don't share lifetimes — one's
            # ControlPersist timeout would otherwise tear down the other.
            control_path_prefix="gemini",
        )
        remote_gemini = resolve_remote_cli_path(
            "gemini",
            target,
            extra_search_paths=[
                "~/.local/bin/gemini",
                "/usr/local/bin/gemini",
                "/usr/bin/gemini",
            ],
        )
        remote_cmd = RemoteCommand(
            project_dir=self._config.project_dir,
            remote_cli=remote_gemini,
            # No env forwarding: the remote host has its own .env (e.g.
            # GEMINI_API_KEY) set up at install time.  Forwarding the
            # local env over SSH would either leak local credentials
            # (visible in `ps` on the remote) or miss vars the remote
            # setup expects.  Same rationale as Qwen.
        )
        # ``local_argv[0]`` is the LOCAL gemini path; the remote path is
        # already embedded inside ``remote_cmd``.  Drop it and pass the
        # rest of the flags as positional args — ``build_remote_argv``
        # shell-quotes each one so prompts with spaces/quotes/newlines
        # survive across SSH intact.
        argv = build_remote_argv(
            target=target,
            remote_cmd=remote_cmd,
            remote_args=local_argv[1:],
        )
        return argv, None

    async def _prewarm(self) -> None:
        """Pre-pay the slow first-prompt costs at session start.

        Without this, the FIRST send() on a fresh session blocks the
        user for several seconds while we either:

        1. **Remote sessions**: open SSH, run ``which gemini`` (2-10s
           for the first call, cached for subsequent calls — see
           :func:`manager._ssh.resolve_remote_cli_path`).
        2. **Local sessions**: cold-start the Node runtime that backs
           the ``gemini`` CLI.  On a fresh boot the Node binary + the
           CLI's JS modules aren't in the OS page cache, so the first
           invocation pays a multi-second I/O hit; later invocations
           are warm.

        Running both probes here moves that latency off the user's
        first prompt and onto the session-open step (which already
        shows a spinner / connecting indicator).

        Best-effort — exceptions are logged and swallowed.  If the
        warmup itself fails, the user will simply pay the cost on the
        real first turn instead, which is no worse than today.

        Mirror of :meth:`manager.qwen.session.QwenSessionManager._prewarm`.
        """
        if self._config.ssh_host:
            try:
                target = SshTarget(
                    host=self._config.ssh_host,
                    user=self._config.ssh_user,
                    key=self._config.ssh_key,
                    control_path_prefix="gemini",
                )
                # resolve_remote_cli_path is synchronous (runs `ssh ...
                # which gemini` via subprocess.run with a 10s timeout) —
                # offload to a worker thread so we don't block the loop.
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: resolve_remote_cli_path(
                        "gemini",
                        target,
                        extra_search_paths=[
                            "~/.local/bin/gemini",
                            "/usr/local/bin/gemini",
                            "/usr/bin/gemini",
                        ],
                    ),
                )
            except Exception:
                logger.exception(
                    "Gemini remote CLI path warmup failed for %s; first turn "
                    "will pay the resolution cost instead.",
                    self._local_id,
                )
            return

        # Local-only Node-runtime warmup.  Spawn `gemini --version` with
        # a short timeout — its only purpose is to fault in the Node
        # binary and the CLI's JS bundle so the FS page cache is hot
        # before the user's first real turn.  We don't care about the
        # exit code or output.
        try:
            proc = await asyncio.create_subprocess_exec(
                _gemini_executable(), "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                # Warmup overran our budget — kill and move on.  The
                # real turn might still be slow but at least we won't
                # delay session-open further.
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                logger.warning(
                    "Gemini local CLI warmup exceeded 10s for %s; skipping.",
                    self._local_id,
                )
        except FileNotFoundError:
            # Missing CLI surfaces on the real first turn with a clearer
            # error message; no point duplicating it here.
            pass
        except Exception:
            logger.exception(
                "Gemini local CLI warmup failed for %s; first turn will "
                "pay the cold-start cost instead.",
                self._local_id,
            )
