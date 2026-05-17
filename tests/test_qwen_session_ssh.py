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
    assert remote_cmd.startswith("cd '/remote/project' && exec '/remote/.local/bin/qwen'")
    assert "'--input-format'" in remote_cmd
    assert "'stream-json'" in remote_cmd
    assert "'--resume'" in remote_cmd
    assert "'sid-abc'" in remote_cmd
    # And critically: the local qwen path doesn't leak through into the
    # remote command (the remote doesn't have /local/qwen).
    assert "/local/qwen" not in remote_cmd


def test_ssh_wrapping_resolves_remote_path_with_extra_search_paths():
    """When ``which qwen`` returns nothing (cron-style shell), the
    fallback chain must be searched."""
    sm = QwenSessionManager(config=_ssh_cfg())
    captured: dict = {}

    def fake_resolve(cli_name, target, *, extra_search_paths=None):
        captured["cli_name"] = cli_name
        captured["extra_search_paths"] = extra_search_paths
        return "/r/qwen"

    with patch("manager.qwen.session.resolve_remote_cli_path", side_effect=fake_resolve):
        sm._maybe_wrap_with_ssh(["/local/qwen", "--flag"])

    assert captured["cli_name"] == "qwen"
    assert captured["extra_search_paths"] == [
        "~/.local/bin/qwen",
        "/usr/local/bin/qwen",
        "/usr/bin/qwen",
    ]


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
