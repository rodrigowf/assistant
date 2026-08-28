"""Tests for ``QwenSessionManager._maybe_wrap_with_ssh`` and the
session lifecycle's SSH reachability probe.

The wrapping logic is the only Qwen-specific SSH code; the shared
primitives are exercised in ``test_ssh_helper.py``.  Two properties
we want to pin here:

1. **Local sessions are pass-through** — no SSH binary, no CLI-path
   probe, original argv unchanged.
2. **Remote sessions** replace argv[0] with the resolved remote qwen
   path and prepend the ssh wrapper, with cwd=None (the remote cwd is
   set inside the SSH command).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manager._ssh import RemoteHostUnreachableError, clear_remote_cli_path_cache
from manager.config import ManagerConfig
from manager.qwen.session import QwenSessionManager


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_remote_cli_path_cache()
    yield
    clear_remote_cli_path_cache()


def _local_cfg() -> ManagerConfig:
    return ManagerConfig(provider="qwen", project_dir="/local/project")


def _ssh_cfg() -> ManagerConfig:
    return ManagerConfig(
        provider="qwen",
        project_dir="/remote/project",
        ssh_host="10.0.0.1",
        ssh_user="agent",
    )


# ---------------------------------------------------------------------------
# _maybe_wrap_with_ssh — local pass-through
# ---------------------------------------------------------------------------


def test_local_session_passes_argv_through_unchanged():
    sm = QwenSessionManager(config=_local_cfg())
    local_argv = ["/local/qwen", "--input-format", "stream-json"]
    argv, cwd = sm._maybe_wrap_with_ssh(local_argv)
    assert argv == local_argv
    assert cwd == "/local/project"


def test_local_session_does_not_probe_or_open_ssh():
    """No CLI-path probe should fire for a local session — that would
    be wasted work and would error if the network is down."""
    sm = QwenSessionManager(config=_local_cfg())
    with patch("manager.qwen.session.resolve_remote_cli_path") as mock_resolve, \
         patch("manager._ssh.subprocess.run") as mock_run:
        sm._maybe_wrap_with_ssh(["/local/qwen", "--flag"])
    mock_resolve.assert_not_called()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _maybe_wrap_with_ssh — SSH wrapping
# ---------------------------------------------------------------------------


def test_ssh_session_wraps_argv_with_ssh_prefix():
    sm = QwenSessionManager(config=_ssh_cfg())
    local_argv = [
        "/local/qwen",
        "--input-format", "stream-json",
        "--resume", "sid-abc",
    ]
    with patch(
        "manager.qwen.session.resolve_remote_cli_path",
        return_value="/remote/.local/bin/qwen",
    ):
        argv, cwd = sm._maybe_wrap_with_ssh(local_argv)

    # cwd is irrelevant when the SSH command sets `cd` itself.
    assert cwd is None
    # SSH multiplexing flags should be present.
    assert argv[0] == "ssh"
    assert "ControlMaster=auto" in argv
    assert any("/tmp/qwen-ssh-10.0.0.1-" in s for s in argv)
    # The target user@host.
    assert "agent@10.0.0.1" in argv
    # The remote command (last arg) substitutes the LOCAL qwen path with
    # the resolved REMOTE path and forwards the rest of the flags.
    remote_cmd = argv[-1]
    # PATH prepends the CLI's own dir so its `#!/usr/bin/env node` shebang
    # resolves on a non-interactive remote shell -- see
    # test_remote_command_puts_cli_dir_on_path.
    assert remote_cmd.startswith(
        "cd '/remote/project' && PATH=/remote/.local/bin:$PATH "
        "exec '/remote/.local/bin/qwen'",
    )
    assert "'--input-format'" in remote_cmd
    assert "'stream-json'" in remote_cmd
    assert "'--resume'" in remote_cmd
    assert "'sid-abc'" in remote_cmd
    # And critically: the local qwen path doesn't leak through into the
    # remote command (the remote doesn't have /local/qwen).
    assert "/local/qwen" not in remote_cmd


def test_ssh_wrapping_resolves_remote_path_for_qwen():
    """The SSH wrap must resolve the *remote* qwen path, keyed by cli name.

    The fallback search chain itself is no longer passed from here — it
    lives in :func:`manager._ssh.default_cli_search_paths` so every harness
    shares one list (see the dedicated tests below).  This test pins only
    what the call site still owns: the cli name.
    """
    sm = QwenSessionManager(config=_ssh_cfg())
    captured: dict = {}

    def fake_resolve(cli_name, target, *, extra_search_paths=None):
        captured["cli_name"] = cli_name
        captured["extra_search_paths"] = extra_search_paths
        return "/r/qwen"

    with patch("manager.qwen.session.resolve_remote_cli_path", side_effect=fake_resolve):
        sm._maybe_wrap_with_ssh(["/local/qwen", "--flag"])

    assert captured["cli_name"] == "qwen"


def test_default_search_paths_include_nvm_glob():
    """When ``which qwen`` returns nothing, the fallback chain must still
    find an nvm-installed CLI.

    Regression test for the 2026-08-28 outage: the probe shell is
    non-interactive, and Ubuntu's stock ``~/.bashrc`` returns early for
    non-interactive shells *before* the nvm block loads.  So ``which``
    finds nothing for an nvm-installed qwen, the probe fell through to the
    bare name ``"qwen"``, and every remote turn died with exit 127
    (``qwen: command not found``).  The glob entry is what fixes it, and it
    must stay a glob so a later ``nvm install`` doesn't re-break it.
    """
    from manager._ssh import default_cli_search_paths

    for cli in ("qwen", "gemini", "claude"):
        paths = default_cli_search_paths(cli)
        assert f"~/.nvm/versions/node/*/bin/{cli}" in paths
        # Every entry must be for this CLI -- no cross-harness leakage.
        assert all(p.endswith(f"/{cli}") for p in paths)


def test_probe_command_sorts_nvm_matches_newest_first():
    """The fallback ``ls`` must use ``-t`` (newest mtime first).

    With several node versions installed, a plain ``ls`` sorts lexically
    and ``head -1`` picks the OLDEST (v16 before v22) -- usually a stale
    CLI or a dead path.  ``-t`` picks what the newest ``nvm install`` wrote.
    """
    import subprocess as _sp
    from manager import _ssh

    captured: dict = {}

    class _Result:
        returncode = 0
        stdout = "/r/qwen\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Result()

    target = _ssh.SshTarget(
        host="h", user="u", key=None, control_path_prefix="qwen",
    )
    with patch.object(_ssh, "probe_host_reachable", return_value=True), \
            patch.object(_sp, "run", side_effect=fake_run), \
            patch.object(_ssh, "get_cached_remote_cli_path", return_value=None), \
            patch.object(_ssh, "set_cached_remote_cli_path"):
        _ssh.resolve_remote_cli_path("qwen", target)

    probe_cmd = captured["argv"][-1]
    assert "ls -t " in probe_cmd
    assert "~/.nvm/versions/node/*/bin/qwen" in probe_cmd


def test_ssh_wrapping_does_not_forward_local_env():
    """Forwarding the local env over SSH would either leak DASHSCOPE_API_KEY
    on the remote (visible in `ps`) or miss vars the remote setup expects.
    The remote should rely entirely on its own .env.  Pin that the
    rendered command contains no env-prefix."""
    sm = QwenSessionManager(config=_ssh_cfg())
    with patch("manager.qwen.session.resolve_remote_cli_path", return_value="/r/qwen"):
        argv, _ = sm._maybe_wrap_with_ssh(["/local/qwen"])

    remote_cmd = argv[-1]
    # No KEY=value prefix.  The shape is exactly: cd '...' && exec '...'
    # (followed by zero or more single-quoted args).
    assert "DASHSCOPE_API_KEY=" not in remote_cmd
    assert "exec '/r/qwen'" in remote_cmd


# ---------------------------------------------------------------------------
# Lifecycle reachability probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_raises_fast_when_ssh_host_unreachable():
    """Hibernated/offline target → start() fails in ~2s with
    RemoteHostUnreachableError, not a 30s SSH TCP timeout."""
    sm = QwenSessionManager(config=_ssh_cfg())
    with patch("manager.qwen.session.probe_host_reachable", return_value=False):
        with pytest.raises(RemoteHostUnreachableError):
            await sm.start()


@pytest.mark.asyncio
async def test_start_proceeds_when_ssh_host_reachable():
    """Reachable host → start() completes normally, session goes IDLE.

    We also mock ``resolve_remote_cli_path`` to keep this test hermetic:
    the prewarm path (added to amortize first-prompt latency) would
    otherwise spawn a real ``ssh`` subprocess against the dummy host
    and add a couple of seconds of timeout to the test.
    """
    sm = QwenSessionManager(config=_ssh_cfg())
    with patch("manager.qwen.session.probe_host_reachable", return_value=True), \
         patch(
            "manager.qwen.session.resolve_remote_cli_path",
            return_value="/r/qwen",
         ):
        await sm.start()
    try:
        from manager.types import SessionStatus
        assert sm.status == SessionStatus.IDLE
    finally:
        await sm.stop()


@pytest.mark.asyncio
async def test_local_session_skips_probe():
    """No ssh_host → no probe call, no network dependency at start time.

    Also patches ``create_subprocess_exec`` because the local prewarm
    path spawns ``qwen --version`` to warm the OS file cache; we don't
    want that to actually run during the test.
    """
    sm = QwenSessionManager(config=_local_cfg())
    fake_proc = MagicMock()
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.kill = MagicMock()
    with patch("manager.qwen.session.probe_host_reachable") as mock_probe, \
         patch(
            "manager.qwen.session.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
         ):
        await sm.start()
    mock_probe.assert_not_called()
    await sm.stop()


def test_remote_command_puts_cli_dir_on_path():
    """The remote command must prepend the CLI's own dir to PATH.

    Second half of the 2026-08-28 outage.  Resolving the absolute CLI path
    was necessary but not sufficient: these CLIs are Node scripts with a
    ``#!/usr/bin/env node`` shebang, and for an nvm install ``node`` lives
    in the *same* bin dir as the CLI.  The remote shell is non-interactive
    so nvm never loaded, and exec'ing the correctly-resolved qwen still
    died with::

        /usr/bin/env: 'node': No such file or directory   (exit 127)

    -- indistinguishable, from the logs, from the CLI itself being missing.
    """
    from manager._ssh import RemoteCommand

    cli = "/home/rodrigo/.nvm/versions/node/v22.21.1/bin/qwen"
    rendered = RemoteCommand(project_dir="/p", remote_cli=cli).render_shell()

    assert "PATH=/home/rodrigo/.nvm/versions/node/v22.21.1/bin:$PATH" in rendered
    # $PATH must stay UNQUOTED so it expands remotely; quoting it would
    # clobber the inherited PATH and break git/rg lookups from the CLI.
    assert "'$PATH'" not in rendered


def test_remote_command_still_quotes_other_env_values():
    """PATH is special-cased as unquoted; everything else must stay quoted
    so a value containing spaces or quotes can't break out of the command."""
    from manager._ssh import RemoteCommand

    rendered = RemoteCommand(
        project_dir="/p", remote_cli="/usr/bin/qwen", env={"FOO": "ba r"},
    ).render_shell()

    assert "FOO='ba r'" in rendered


def test_remote_command_skips_path_for_bare_cli_name():
    """When the probe fell back to a bare name there's no directory to add,
    and emitting ``PATH=.:$PATH`` would put CWD on PATH -- don't."""
    from manager._ssh import RemoteCommand

    rendered = RemoteCommand(project_dir="/p", remote_cli="qwen").render_shell()

    assert "PATH=" not in rendered
