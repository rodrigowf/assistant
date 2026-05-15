"""Shared SSH primitives for remote session execution.

The two session managers (``ClaudeSessionManager``, ``QwenSessionManager``)
both want to run their respective agent CLIs on a remote host over SSH.
The mechanics — ICMP pre-probe, ``ControlMaster`` multiplexing flags,
remote-CLI absolute-path caching, the inline-prefix env-var trick to avoid
``export`` leaking ``declare -x`` onto stdout — are identical across
providers, and they grow only when a new provider lands.  This module is
the single place that knows how to talk SSH; the session managers just
compose the primitives.

The two providers differ in *how* they hand the remote command to a
subprocess:

* **Claude** — the SDK takes a ``cli_path`` argument and runs
  ``<cli_path> --flag1 v1 --flag2 v2 ...`` itself.  The argv is built at
  SDK runtime and includes user-supplied flags.  We can't intercept that
  argv from inside Python, so we make ``cli_path`` point at a temp shell
  wrapper that re-quotes ``"$@"`` and embeds it in a single SSH argument.
  See :func:`write_ssh_wrapper_script`.

* **Qwen** — argv is built by us (``QwenSessionManager._build_argv``) and
  passed directly to ``asyncio.create_subprocess_exec``.  There's no
  ``"$@"`` forwarding problem.  We just prepend ``ssh ...`` and emit one
  argv that exec's the remote command.  See :func:`build_ssh_argv` plus
  :func:`render_remote_command`.

The reachability + caching + arg-quoting pieces are shared.  The
per-provider invocation shape isn't — and trying to abstract it would just
hide the only real difference between the two paths.

Caching: ``_REMOTE_CLI_PATH_CACHE`` is keyed by
``(cli_name, host, user, key)`` so two providers on the same host don't
share entries (a stale Claude path doesn't pollute Qwen's lookup, and
vice versa).  Concurrent session starts use a single cached probe; this
is what kept the laptop from melting the 2026-04-20 SSH churn.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


class RemoteHostUnreachableError(RuntimeError):
    """Raised when an SSH target fails the ICMP reachability pre-probe.

    Distinguished from generic ``RuntimeError`` so callers can decide
    whether to retry (offline laptop coming back online) or fail fast
    (operator typo in the SSH host field).
    """


def probe_host_reachable(host: str, timeout_s: float = 2.0) -> bool:
    """Return ``True`` if *host* replies to a single ICMP ping within *timeout_s*.

    This is a cheap pre-flight so we don't let the SDK (or our own path
    probe) open an SSH connection to an unreachable host, which then
    hangs in TCP retransmit for ~30 s while holding file descriptors and
    burning CPU cycles in the SSH client.  When the laptop hibernates with
    an in-flight remote session, this check turns a slow timeout cascade
    into an instant clean failure.

    The function is deliberately synchronous and non-awaitable — callers
    that care about event-loop latency should run it in a thread.  The
    2-second default is long enough for normal LAN jitter and short enough
    that a typical failed probe costs less than one SSH TCP timeout.

    An empty host string is treated as "reachable" so the local
    (non-SSH) code paths don't accidentally invoke this and short-circuit.
    """
    if not host:
        return True
    try:
        # -c 1: one packet.  -W <s>: overall deadline for the reply.
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(max(1, timeout_s))), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s + 1.0,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # If `ping` isn't available or the subprocess itself times out,
        # assume unreachable rather than silently falling through — the
        # whole point is to short-circuit bad paths.
        return False


# ---------------------------------------------------------------------------
# Remote-CLI path cache
# ---------------------------------------------------------------------------


# Cache of resolved remote ``<cli>`` absolute paths, keyed by a tuple that
# identifies a unique (cli, ssh-target) pair.  Two providers on the same
# host don't share entries — a stale claude path doesn't pollute qwen's
# lookup.  Concurrent starts on the same target hit a single cached probe
# instead of opening N parallel SSH handshakes (the cause of the
# 2026-04-20 session-churn crash on the Jetson).
_RemoteCliCacheKey = tuple[str, str | None, str | None, str | None]
_REMOTE_CLI_PATH_CACHE: dict[_RemoteCliCacheKey, str] = {}
_REMOTE_CLI_PATH_LOCK = threading.Lock()


def _cache_key(
    cli_name: str,
    host: str | None,
    user: str | None,
    key: str | None,
) -> _RemoteCliCacheKey:
    return (cli_name, host, user, key)


def get_cached_remote_cli_path(
    cli_name: str,
    host: str | None,
    user: str | None,
    key: str | None,
) -> str | None:
    """Return a cached remote path for *(cli_name, host, user, key)* or ``None``."""
    with _REMOTE_CLI_PATH_LOCK:
        return _REMOTE_CLI_PATH_CACHE.get(_cache_key(cli_name, host, user, key))


def set_cached_remote_cli_path(
    cli_name: str,
    host: str | None,
    user: str | None,
    key: str | None,
    path: str,
) -> None:
    """Store a resolved remote path for *(cli_name, host, user, key)*."""
    with _REMOTE_CLI_PATH_LOCK:
        _REMOTE_CLI_PATH_CACHE[_cache_key(cli_name, host, user, key)] = path


def clear_remote_cli_path_cache(cli_name: str | None = None) -> None:
    """Forget cached remote paths.

    With no argument, clears the whole cache.  Passing *cli_name* clears
    only entries for that provider — useful when a single provider's
    remote install moves but the others stay put.  Intended for tests and
    for the rare operator override after upgrading a remote CLI.
    """
    with _REMOTE_CLI_PATH_LOCK:
        if cli_name is None:
            _REMOTE_CLI_PATH_CACHE.clear()
            return
        for key in [k for k in _REMOTE_CLI_PATH_CACHE if k[0] == cli_name]:
            _REMOTE_CLI_PATH_CACHE.pop(key, None)


# ---------------------------------------------------------------------------
# SSH argv construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SshTarget:
    """Everything needed to address a remote host.

    Bundled so callers don't have to thread four positional args through
    every helper.  ``control_path_prefix`` is *per-provider* so two
    providers on the same host get distinct ControlMaster sockets:
    ``/tmp/claude-ssh-host-%r`` vs ``/tmp/qwen-ssh-host-%r``.  Sharing a
    socket would couple their lifetimes — one provider's ControlPersist
    timeout would tear down the other's in-flight session.
    """
    host: str
    user: str | None = None
    key: str | None = None
    control_path_prefix: str = "ssh"

    def cache_key(self, cli_name: str) -> _RemoteCliCacheKey:
        return _cache_key(cli_name, self.host, self.user, self.key)


def build_ssh_argv(target: SshTarget) -> list[str]:
    """Build the SSH command prefix as an argv list.

    Includes ``BatchMode=yes`` (no interactive prompts ever — we never
    want SSH to block on a passphrase prompt mid-session) and
    ``StrictHostKeyChecking=accept-new`` so first-connect doesn't error
    on an unknown host.  ``ControlMaster=auto`` + ``ControlPersist=60s``
    multiplexes a single TCP connection across the burst of SSHes that
    happens during session creation and per-turn spawns.
    """
    argv = [
        "ssh",
        "-T",                                       # no pseudo-TTY
        "-o", "BatchMode=yes",                      # never prompt
        "-o", "StrictHostKeyChecking=accept-new",   # auto-accept on first connect
        "-o", "ControlMaster=auto",                 # multiplex over one TCP conn
        "-o", "ControlPersist=60s",
        "-o", f"ControlPath=/tmp/{target.control_path_prefix}-ssh-{target.host}-%r",
    ]
    if target.key:
        argv += ["-i", str(target.key)]
    if target.user:
        argv.append(f"{target.user}@{target.host}")
    else:
        argv.append(target.host)
    return argv


# ---------------------------------------------------------------------------
# Remote CLI path resolution
# ---------------------------------------------------------------------------


def resolve_remote_cli_path(
    cli_name: str,
    target: SshTarget,
    *,
    extra_search_paths: list[str] | None = None,
) -> str:
    """Resolve the absolute path of *cli_name* on the remote host.

    Returns the cached value if present; otherwise opens an SSH connection
    and runs ``which <cli_name>`` (with a fallback to common install
    locations).  If the host fails the ping pre-probe, returns the bare
    *cli_name* as a fallback **without caching** — that way the next
    attempt re-probes after the host comes back online instead of being
    stuck on the fallback forever.

    *extra_search_paths* is an ordered list of absolute paths to try as a
    fallback when ``which`` returns nothing (e.g. ``~/.local/bin/claude``,
    ``/usr/local/bin/claude``).  Useful when ``which`` runs without the
    user's full PATH (cron-like shell).
    """
    cached = get_cached_remote_cli_path(cli_name, target.host, target.user, target.key)
    if cached is not None:
        logger.debug("Remote %s path (cached): %s", cli_name, cached)
        return cached

    # Pre-probe: don't open SSH to an unreachable host.  We deliberately
    # do NOT cache the fallback in this branch — when the host comes back
    # online we want a real probe, not a stale "claude" lookup.
    if not probe_host_reachable(target.host, 2.0):
        logger.warning(
            "SSH host %r unreachable; using %r fallback (will re-probe "
            "next time, no cache).",
            target.host, cli_name,
        )
        return cli_name

    fallback_chain = " ".join(extra_search_paths or [])
    fallback_clause = (
        f" || ls {fallback_chain} 2>/dev/null | head -1"
        if fallback_chain
        else ""
    )
    probe_cmd = (
        f"bash -c '. ~/.profile 2>/dev/null; . ~/.bashrc 2>/dev/null;"
        f" which {cli_name} 2>/dev/null{fallback_clause}'"
    )
    ssh_argv = build_ssh_argv(target)
    try:
        result = subprocess.run(
            ssh_argv + [probe_cmd],
            capture_output=True, text=True, timeout=10,
        )
        resolved = result.stdout.strip().splitlines()[0] if result.stdout.strip() else cli_name
    except Exception as e:
        logger.warning("Could not resolve remote %s path: %s", cli_name, e)
        resolved = cli_name
    set_cached_remote_cli_path(cli_name, target.host, target.user, target.key, resolved)
    logger.debug("Remote %s path resolved to: %s (cached)", cli_name, resolved)
    return resolved


# ---------------------------------------------------------------------------
# Remote command rendering
# ---------------------------------------------------------------------------


def _shell_single_quote(s: str) -> str:
    """POSIX-safe single-quoting: wraps *s* in single quotes, escaping
    embedded ones via the break-and-rejoin trick (``'`` → ``'\\''``)."""
    return "'" + s.replace("'", "'\\''") + "'"


@dataclass
class RemoteCommand:
    """Specification of "what to run on the remote, in what directory,
    with what env."

    Renders to a single shell string of the form::

        cd '<project_dir>' && KEY1='v1' KEY2='v2' exec '<remote_cli>'

    Two design choices worth flagging:

    * **No** ``export VAR=val``.  When a ``bash -c`` command containing
      ``export`` is run over SSH from a Python subprocess, bash emits a
      full ``declare -x`` environment dump on stdout — corrupting the
      JSON stream we read for events.  The inline-assignment prefix
      ``VAR=val exec cmd`` sets the variable in the child's environment
      without triggering this.

    * **No trailing** ``"$@"``.  Each provider either appends args itself
      (Qwen — argv known up front) or uses a wrapper script that
      shell-quotes ``"$@"`` locally and embeds the result in the SSH
      argument (Claude — argv supplied by the SDK at runtime).
    """
    project_dir: str
    remote_cli: str
    env: dict[str, str] = field(default_factory=dict)

    def render_shell(self) -> str:
        env_prefix = "".join(
            f"{k}={_shell_single_quote(v)} " for k, v in self.env.items()
        )
        return (
            "cd " + _shell_single_quote(self.project_dir)
            + " && " + env_prefix
            + "exec " + _shell_single_quote(self.remote_cli)
        )


# ---------------------------------------------------------------------------
# Claude-style wrapper script
# ---------------------------------------------------------------------------


def write_ssh_wrapper_script(
    *,
    ssh_argv: list[str],
    remote_cmd: str,
    prefix: str,
) -> str:
    """Write a ``#!/bin/sh`` wrapper that SSHes into the remote and runs *remote_cmd*.

    Used by callers (e.g. the Claude SDK) that hand us a ``cli_path`` and
    then invoke ``<cli_path> arg1 arg2 ...`` themselves — we can't
    intercept their argv, so the wrapper has to forward ``"$@"``.

    KEY INSIGHT: SSH joins all its trailing word-arguments into ONE remote
    command string.  ``ssh host bash -c 'script' _ arg1 arg2`` arrives at
    the remote as ``bash -c script _ arg1 arg2`` (one string), which is
    re-parsed as bash with a single ``-c`` value of ``script _ arg1
    arg2``.  That silently breaks ``cd`` and the ``"$@"`` inside.

    Fix: expand ``"$@"`` locally in the wrapper, shell-quoting each arg,
    then pass the entire remote command — cd, env vars, exec, and all
    args — as ONE double-quoted string to SSH.  SSH forwards that single
    string to the remote shell, which parses and executes it correctly.

    Generated wrapper::

        #!/bin/sh
        _q=''
        for _a in "$@"; do
          _q="${_q} '$(printf '%s' "$_a" | sed "s/'/'\\''/g")'"
        done
        exec ssh ... "<remote_cmd>${_q}"

    *prefix* is included in the tempfile name so the orphan reaper and
    operators can tell which provider's wrapper a stray ``/tmp/...sh``
    belongs to.  Returns the path; **caller owns cleanup** (typically in
    its session ``finally`` block).
    """
    ssh_cmd = shlex.join(ssh_argv)
    # remote_cmd uses single quotes internally (via RemoteCommand /
    # _shell_single_quote), so embedding it in a double-quoted SSH arg is
    # safe — we just need to keep it free of any literal ``"`` characters,
    # which our renderers never emit.
    script = (
        "#!/bin/sh\n"
        "_q=''\n"
        "for _a in \"$@\"; do\n"
        "  _q=\"${_q} '$(printf '%s' \"$_a\" | sed \"s/'/'\\''/g\")'\"\n"
        "done\n"
        "exec " + ssh_cmd + " \"" + remote_cmd + "${_q}\"\n"
    )

    fd, path = tempfile.mkstemp(prefix=f"{prefix}-ssh-", suffix=".sh")
    try:
        os.write(fd, script.encode())
    finally:
        os.close(fd)
    os.chmod(path, stat.S_IRWXU)  # 0o700 — owner execute only
    logger.debug("SSH wrapper script written to %s", path)
    return path


def cleanup_ssh_wrapper_script(path: str | None) -> None:
    """Remove a wrapper script written by :func:`write_ssh_wrapper_script`.

    Idempotent and exception-safe — the caller's ``finally`` block can
    invoke this without an outer try/except.
    """
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        # Cleanup is best-effort.  A leaked wrapper is harmless: it's a
        # mode-0700 shell script in /tmp that nothing references.
        pass


# ---------------------------------------------------------------------------
# Qwen-style "argv that starts with ssh"
# ---------------------------------------------------------------------------


def build_remote_argv(
    *,
    target: SshTarget,
    remote_cmd: RemoteCommand,
    remote_args: list[str],
) -> list[str]:
    """Build an argv that runs ``<remote_cli> <remote_args...>`` over SSH.

    For callers that build their own argv and ``exec`` it directly via
    ``asyncio.create_subprocess_exec`` (Qwen, future provider X).  No
    temp wrapper script is needed because we control the argv shape end
    to end — there's no ``"$@"`` to forward.

    The trailing args are shell-single-quoted and joined onto the
    remote_cmd's exec line, so values containing spaces or shell
    metacharacters survive intact on the remote side.
    """
    arg_suffix = "".join(" " + _shell_single_quote(a) for a in remote_args)
    full_remote = remote_cmd.render_shell() + arg_suffix
    return build_ssh_argv(target) + [full_remote]


__all__ = [
    "RemoteHostUnreachableError",
    "probe_host_reachable",
    "get_cached_remote_cli_path",
    "set_cached_remote_cli_path",
    "clear_remote_cli_path_cache",
    "SshTarget",
    "build_ssh_argv",
    "resolve_remote_cli_path",
    "RemoteCommand",
    "write_ssh_wrapper_script",
    "cleanup_ssh_wrapper_script",
    "build_remote_argv",
]
