"""SessionManager — wraps a single Claude Code session via claude-agent-sdk."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import stat
import subprocess
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache of resolved remote `claude` absolute paths, keyed by a tuple that
# identifies a unique SSH target. Avoids opening a fresh SSH connection to
# probe `which claude` on every session start — under concurrent starts
# those probes were the main source of session churn on the remote host.
# Entry is whatever `_resolve_remote_claude` returned (including the
# fallback string "claude") so we don't re-probe failures either.
_REMOTE_CLAUDE_PATH_CACHE: dict[tuple[str | None, str | None, str | None], str] = {}
_REMOTE_CLAUDE_PATH_LOCK = threading.Lock()


def clear_remote_claude_path_cache() -> None:
    """Forget every cached remote-claude path.

    Intended for tests and for the rare case an operator wants to force a
    re-probe after updating the remote `claude` install.
    """
    with _REMOTE_CLAUDE_PATH_LOCK:
        _REMOTE_CLAUDE_PATH_CACHE.clear()


class RemoteHostUnreachableError(RuntimeError):
    """Raised when an SSH target fails the ICMP reachability pre-probe."""


def _probe_ssh_host_reachable(host: str, timeout_s: float = 2.0) -> bool:
    """Return True if ``host`` replies to a single ICMP ping within *timeout_s*.

    This is a cheap pre-flight so we don't let the SDK (or our own
    version-probe) open an SSH connection to an unreachable host, which then
    hangs in TCP retransmit for ~30 s while holding file descriptors and
    burning CPU cycles in the SSH client.  When the laptop hibernates with
    an in-flight remote session, this check turns a slow timeout cascade
    into an instant clean failure.

    The function is deliberately synchronous and non-awaitable — callers
    that care about event-loop latency should run it in a thread.  The
    2-second default is long enough for normal LAN jitter and short enough
    that a typical failed probe costs less than one SSH TCP timeout.
    """
    if not host:
        return True  # Nothing to probe; treat as reachable.
    try:
        # -c 1: one packet.  -W <s>: overall deadline for the reply.
        # stdout/stderr discarded; we only care about the exit code.
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(max(1, timeout_s))), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s + 1.0,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # If ping isn't available or the subprocess itself times out, assume
        # unreachable rather than silently falling through — the whole point
        # is to short-circuit bad paths.
        return False

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
        local_id: str | None = None,
        fork: bool = False,
        config: ManagerConfig | None = None,
    ) -> None:
        self._config = config or ManagerConfig.load()
        self._local_id = local_id or str(uuid.uuid4())
        self._resume_id = session_id  # SDK session ID for resume
        self._fork = fork
        self._sdk_session_id: str | None = None
        self._client: ClaudeSDKClient | None = None
        self._status = SessionStatus.DISCONNECTED
        self._cost: float = 0.0
        self._turns: int = 0
        self._ssh_wrapper_path: str | None = None  # temp script for SSH sessions

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
        """Connect to Claude Code and return the local session ID.

        The local ID is stable and never changes.  The real SDK session ID
        is captured from ``server_info`` (if available) or from the first
        ``ResultMessage`` after a query, and stored as ``sdk_session_id``.
        """
        # Cheap reachability pre-probe for remote sessions.  One ICMP ping,
        # 2 s deadline — if the host is asleep/offline we raise immediately
        # instead of letting the SDK sit in a 30 s SSH TCP timeout (which
        # historically pinned the CPU and spun the fan on the Jetson while
        # the laptop was hibernating).
        if self._config.ssh_host:
            reachable = await asyncio.get_running_loop().run_in_executor(
                None, _probe_ssh_host_reachable, self._config.ssh_host, 2.0
            )
            if not reachable:
                raise RemoteHostUnreachableError(
                    f"SSH host {self._config.ssh_host!r} did not reply to ICMP ping; "
                    "refusing to open SSH connection (prevents stuck sessions and "
                    "fan/CPU churn while the remote is offline)."
                )

        options = self._build_options()
        self._client = ClaudeSDKClient(options)
        await self._client.connect()

        # Capture the SDK session ID if available at connect time.
        if self._resume_id:
            self._sdk_session_id = self._resume_id
        else:
            server_info = await self._client.get_server_info()
            if server_info:
                self._sdk_session_id = server_info.get("session_id")

        self._status = SessionStatus.IDLE
        return self._local_id

    async def stop(self) -> None:
        """Disconnect from Claude Code."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        self._status = SessionStatus.DISCONNECTED
        # Clean up SSH wrapper script if present
        if self._ssh_wrapper_path:
            try:
                Path(self._ssh_wrapper_path).unlink(missing_ok=True)
            except Exception:
                pass
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
        """
        if self._client is None:
            raise RuntimeError("SessionManager is not connected — call start() first")

        self._status = SessionStatus.STREAMING
        await self._client.query(prompt)

        async for msg in self._client.receive_response():
            # Skip None messages (from patched parser ignoring unknown types)
            if msg is None:
                continue
            async for event in self._process_message(msg):
                yield event

        self._status = SessionStatus.IDLE

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def local_id(self) -> str:
        """Stable local identifier (never changes)."""
        return self._local_id

    @property
    def session_id(self) -> str:
        """Alias for local_id — the stable session identifier."""
        return self._local_id

    @property
    def sdk_session_id(self) -> str | None:
        """The Claude Code SDK session ID (may arrive later via ResultMessage)."""
        return self._sdk_session_id

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

    @property
    def is_resumed(self) -> bool:
        """True if this session was resumed from an existing SDK session."""
        return self._resume_id is not None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_options(self) -> ClaudeAgentOptions:
        """Build SDK options from our config."""
        kwargs: dict = {
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

        The script forwards all arguments ("$@") so the SDK flags land on the
        remote claude process unchanged.  Returns the path to the script.
        """
        remote_path = self._config.project_dir.replace("'", "'\\''")

        ssh_parts = ["ssh"]
        ssh_parts += ["-T"]                                     # no pseudo-TTY
        ssh_parts += ["-o", "BatchMode=yes"]                    # no interactive prompts
        ssh_parts += ["-o", "StrictHostKeyChecking=accept-new"] # auto-accept on first connect
        ssh_parts += ["-o", "ControlMaster=auto",               # connection multiplexing
                      "-o", "ControlPersist=60s",
                      "-o", f"ControlPath=/tmp/claude-ssh-{self._config.ssh_host}-%r"]
        if self._config.ssh_key:
            ssh_parts += ["-i", str(self._config.ssh_key)]
        if self._config.ssh_user:
            ssh_parts.append(f"{self._config.ssh_user}@{self._config.ssh_host}")
        else:
            ssh_parts.append(self._config.ssh_host)

        ssh_cmd = shlex.join(ssh_parts)

        # Resolve the absolute path of `claude` on the remote machine — cached.
        #
        # Each probe opens an SSH connection and runs `which claude` on the
        # remote.  Historically this ran on every SessionManager.start(), so
        # a burst of N concurrent starts (e.g. browser reconnect storm)
        # produced N SSH handshakes *before* the ControlMaster socket could
        # be established.  On the remote side each handshake = one PAM
        # session = one systemd --user manager, and the rapid open/close
        # cycle is what triggered the 2026-04-20 session-churn crash.
        #
        # Cache key is (host, user, key) — changing any of these warrants a
        # new probe.  The fallback result ("claude") is cached too so a
        # broken/unreachable host doesn't re-probe on every retry.
        cache_key = (
            self._config.ssh_host,
            self._config.ssh_user,
            self._config.ssh_key,
        )
        with _REMOTE_CLAUDE_PATH_LOCK:
            cached = _REMOTE_CLAUDE_PATH_CACHE.get(cache_key)
        if cached is not None:
            remote_claude = cached
            logger.debug("Remote claude path (cached): %s", remote_claude)
        else:
            # Before opening an SSH connection for the path probe, ping-probe
            # the host.  If unreachable, use the "claude" fallback *without*
            # caching it — when the host comes back online we want to probe
            # properly rather than being stuck on the fallback forever.
            if not _probe_ssh_host_reachable(self._config.ssh_host or "", 2.0):
                logger.warning(
                    "SSH host %r unreachable; using 'claude' fallback for this "
                    "wrapper without caching (will re-probe next time)",
                    self._config.ssh_host,
                )
                remote_claude = "claude"
            else:
                try:
                    result = subprocess.run(
                        ssh_parts + [
                            "bash -c '. ~/.profile 2>/dev/null; . ~/.bashrc 2>/dev/null;"
                            " which claude 2>/dev/null"
                            " || ls ~/.local/bin/claude /usr/local/bin/claude /usr/bin/claude 2>/dev/null | head -1'"
                        ],
                        capture_output=True, text=True, timeout=10,
                    )
                    remote_claude = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "claude"
                except Exception as e:
                    logger.warning("Could not resolve remote claude path: %s", e)
                    remote_claude = "claude"
                with _REMOTE_CLAUDE_PATH_LOCK:
                    _REMOTE_CLAUDE_PATH_CACHE[cache_key] = remote_claude
                logger.debug("Remote claude path resolved to: %s (cached for future)", remote_claude)

        # Shell-escape helper: wraps a value in single quotes, escaping any embedded
        # single quotes using the break-and-rejoin technique (e.g. ' → '\'' ).
        def sq(s: str) -> str:
            return "'" + s.replace("'", "'\\''") + "'"

        # Build the remote bash -c script body:
        #   cd <sq(dir)> && CLAUDE_CONFIG_DIR=<sq(dir)> exec <sq(claude)> "$@"
        # The "$@" is INSIDE the bash -c body so bash expands it to positional args.
        # Inside single quotes in the wrapper script, "$@" is literal (not expanded by sh).
        #
        # NOTE: We intentionally avoid `export VAR=val` here. When a `bash -c` command
        # containing `export` is run over SSH from a Python subprocess, bash emits a
        # full `declare -x` environment dump on stdout — corrupting the SDK's JSON stream.
        # Using the inline assignment prefix `VAR=val exec cmd` sets the variable in
        # the child process environment without triggering this behaviour.
        parts = []
        parts.append("cd " + sq(self._config.project_dir))
        exec_prefix = ""
        if self._config.ssh_claude_config_dir:
            exec_prefix += "CLAUDE_CONFIG_DIR=" + sq(self._config.ssh_claude_config_dir) + " "
        # No "$@" here — args are appended via ${_q} in the wrapper
        parts.append(exec_prefix + "exec " + sq(remote_claude))
        remote_cmd = " && ".join(parts)

        # Build the wrapper script.
        #
        # KEY INSIGHT: SSH joins all its trailing word-arguments into ONE remote command
        # string.  "ssh host bash -c 'script' _ arg1 arg2" arrives at the remote shell
        # as "bash -c script _ arg1 arg2" (one string), which it re-parses as bash with
        # a single -c value of "script _ arg1 arg2".  This makes `_` and the args part
        # of the -c body rather than positional params, silently breaking the `cd` and
        # the "$@" expansion inside the script.
        #
        # Fix: expand "$@" locally in the wrapper, shell-quoting each arg, then pass
        # the entire remote command — cd, env vars, exec, and all args — as ONE
        # double-quoted string to SSH.  SSH forwards that single string to the remote
        # shell, which parses and executes it correctly with cd working as expected.
        #
        # Generated wrapper:
        #   #!/bin/sh
        #   _q=''
        #   for _a in "$@"; do
        #     _q="${_q} '$(printf '%s' "$_a" | sed "s/'/'\\''/g")'"
        #   done
        #   exec ssh ... "cd '/proj' && [CLAUDE_CONFIG_DIR='/cfg'] exec '/claude'${_q}"

        # remote_cmd already has sq()-quoted paths. We embed it in a double-quoted SSH
        # arg. Since remote_cmd uses single quotes internally, no further escaping needed
        # as long as remote_cmd itself contains no double quotes (it never does).
        script = (
            "#!/bin/sh\n"
            "_q=''\n"
            "for _a in \"$@\"; do\n"
            "  _q=\"${_q} '$(printf '%s' \"$_a\" | sed \"s/'/'\\''/g\")'\"\n"
            "done\n"
            "exec " + ssh_cmd + " \"" + remote_cmd + "${_q}\"\n"
        )

        fd, path = tempfile.mkstemp(prefix="claude-ssh-", suffix=".sh")
        try:
            os.write(fd, script.encode())
        finally:
            os.close(fd)
        os.chmod(path, stat.S_IRWXU)  # 0o700 — owner execute only
        self._ssh_wrapper_path = path
        logger.debug("SSH wrapper script written to %s", path)
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
            if sid and not self._sdk_session_id:
                self._sdk_session_id = sid
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
            # Always capture the SDK session ID from ResultMessage
            if msg.session_id:
                self._sdk_session_id = msg.session_id
            yield TurnComplete(
                cost=msg.total_cost_usd,
                usage=msg.usage or {},
                num_turns=msg.num_turns,
                session_id=msg.session_id,
                is_error=msg.is_error,
                result=msg.result,
            )
