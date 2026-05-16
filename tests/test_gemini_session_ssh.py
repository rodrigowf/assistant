"""Tests for ``GeminiSessionManager._maybe_wrap_with_ssh`` and the
session lifecycle's SSH reachability probe.

Mirror of ``test_qwen_session_ssh.py`` — the two providers share the
same SSH wrapping shape, so the property tests are identical except
for the CLI name and the per-provider ``ControlPath`` prefix.

The wrapping logic is the only Gemini-specific SSH code; the shared
primitives are exercised in ``test_ssh_helper.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manager._ssh import RemoteHostUnreachableError, clear_remote_cli_path_cache
from manager.config import ManagerConfig
from manager.gemini.session import GeminiSessionManager


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_remote_cli_path_cache()
    yield
    clear_remote_cli_path_cache()


def _local_cfg() -> ManagerConfig:
    return ManagerConfig(provider="gemini", project_dir="/local/project")


def _ssh_cfg() -> ManagerConfig:
    return ManagerConfig(
        provider="gemini",
        project_dir="/remote/project",
        ssh_host="10.0.0.2",
        ssh_user="agent",
    )


# ---------------------------------------------------------------------------
# _maybe_wrap_with_ssh — local pass-through
# ---------------------------------------------------------------------------


def test_local_session_passes_argv_through_unchanged():
    sm = GeminiSessionManager(config=_local_cfg())
    local_argv = ["/local/gemini", "--prompt", "hi", "--skip-trust"]
    argv, cwd = sm._maybe_wrap_with_ssh(local_argv)
    assert argv == local_argv
    assert cwd == "/local/project"


def test_local_session_does_not_probe_or_open_ssh():
    """No CLI-path probe should fire for a local session — that would
    be wasted work and would error if the network is down."""
    sm = GeminiSessionManager(config=_local_cfg())
    with patch("manager.gemini.session.resolve_remote_cli_path") as mock_resolve, \
         patch("manager._ssh.subprocess.run") as mock_run:
        sm._maybe_wrap_with_ssh(["/local/gemini", "--flag"])
    mock_resolve.assert_not_called()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _maybe_wrap_with_ssh — SSH wrapping
# ---------------------------------------------------------------------------


def test_ssh_session_wraps_argv_with_ssh_prefix():
    sm = GeminiSessionManager(config=_ssh_cfg())
    local_argv = [
        "/local/gemini",
        "--prompt", "hi there",
        "--skip-trust",
        "--output-format", "stream-json",
    ]
    with patch(
        "manager.gemini.session.resolve_remote_cli_path",
        return_value="/remote/.local/bin/gemini",
    ):
        argv, cwd = sm._maybe_wrap_with_ssh(local_argv)

    # cwd is irrelevant when the SSH command sets `cd` itself.
    assert cwd is None
    # SSH multiplexing flags should be present.
    assert argv[0] == "ssh"
    assert "ControlMaster=auto" in argv
    # ControlPath prefix is per-provider so Gemini and Qwen don't share
    # a socket on the same host.
    assert any("/tmp/gemini-ssh-10.0.0.2-" in s for s in argv)
    # The target user@host.
    assert "agent@10.0.0.2" in argv
    # The remote command substitutes the LOCAL gemini path with the
    # resolved REMOTE path and forwards the rest of the flags.
    remote_cmd = argv[-1]
    assert remote_cmd.startswith(
        "cd '/remote/project' && exec '/remote/.local/bin/gemini'"
    )
    assert "'--prompt'" in remote_cmd
    # Embedded space survives shell quoting across SSH.
    assert "'hi there'" in remote_cmd
    assert "'--skip-trust'" in remote_cmd
    # And critically: the local gemini path doesn't leak through into the
    # remote command (the remote doesn't have /local/gemini).
    assert "/local/gemini" not in remote_cmd


def test_ssh_wrapping_resolves_remote_path_with_extra_search_paths():
    """When ``which gemini`` returns nothing (cron-style shell), the
    fallback chain must be searched."""
    sm = GeminiSessionManager(config=_ssh_cfg())
    captured: dict = {}

    def fake_resolve(cli_name, target, *, extra_search_paths=None):
        captured["cli_name"] = cli_name
        captured["extra_search_paths"] = extra_search_paths
        return "/r/gemini"

    with patch(
        "manager.gemini.session.resolve_remote_cli_path", side_effect=fake_resolve,
    ):
        sm._maybe_wrap_with_ssh(["/local/gemini", "--flag"])

    assert captured["cli_name"] == "gemini"
    assert captured["extra_search_paths"] == [
        "~/.local/bin/gemini",
        "/usr/local/bin/gemini",
        "/usr/bin/gemini",
    ]


def test_ssh_wrapping_does_not_forward_local_env():
    """Forwarding the local env over SSH would either leak credentials
    on the remote (visible in `ps`) or miss vars the remote setup expects.
    The remote should rely entirely on its own .env."""
    sm = GeminiSessionManager(config=_ssh_cfg())
    with patch(
        "manager.gemini.session.resolve_remote_cli_path", return_value="/r/gemini",
    ):
        argv, _ = sm._maybe_wrap_with_ssh(["/local/gemini"])

    remote_cmd = argv[-1]
    # No KEY=value prefix.  Shape is exactly: cd '...' && exec '...'
    assert "GEMINI_API_KEY=" not in remote_cmd
    assert "GEMINI_CLI_TRUST_WORKSPACE=" not in remote_cmd
    assert "exec '/r/gemini'" in remote_cmd


# ---------------------------------------------------------------------------
# Lifecycle reachability probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_raises_fast_when_ssh_host_unreachable():
    """Hibernated/offline target → start() fails in ~2s with
    RemoteHostUnreachableError, not a 30s SSH TCP timeout."""
    sm = GeminiSessionManager(config=_ssh_cfg())
    with patch("manager.gemini.session.probe_host_reachable", return_value=False):
        with pytest.raises(RemoteHostUnreachableError):
            await sm.start()


@pytest.mark.asyncio
async def test_start_proceeds_when_ssh_host_reachable():
    """Reachable host → start() completes normally, session goes IDLE.

    Mocks ``resolve_remote_cli_path`` so the prewarm path doesn't spawn
    a real ssh subprocess against the dummy host.
    """
    sm = GeminiSessionManager(config=_ssh_cfg())
    with patch("manager.gemini.session.probe_host_reachable", return_value=True), \
         patch(
            "manager.gemini.session.resolve_remote_cli_path",
            return_value="/r/gemini",
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

    The local prewarm path spawns ``gemini --version`` — we mock
    ``create_subprocess_exec`` so the test doesn't actually exec it.
    """
    sm = GeminiSessionManager(config=_local_cfg())
    fake_proc = MagicMock()
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.kill = MagicMock()
    with patch("manager.gemini.session.probe_host_reachable") as mock_probe, \
         patch(
            "manager.gemini.session.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
         ):
        await sm.start()
    mock_probe.assert_not_called()
    await sm.stop()


# ---------------------------------------------------------------------------
# Prewarm behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prewarm_resolves_remote_cli_path_for_ssh_sessions():
    """The prewarm path moves the slow `which gemini` SSH probe off the
    user's first prompt and onto session-open.  This verifies the
    resolver is actually invoked during start() for remote sessions."""
    sm = GeminiSessionManager(config=_ssh_cfg())
    with patch("manager.gemini.session.probe_host_reachable", return_value=True), \
         patch(
            "manager.gemini.session.resolve_remote_cli_path",
            return_value="/r/gemini",
         ) as mock_resolve:
        await sm.start()
    try:
        mock_resolve.assert_called_once()
        call_args = mock_resolve.call_args
        # Positional: cli_name="gemini", target=SshTarget(...).
        assert call_args.args[0] == "gemini"
        assert call_args.args[1].host == "10.0.0.2"
        # Keyword: extra_search_paths should be the same fallback chain
        # the real send() uses, so the cache entry is identical.
        assert call_args.kwargs["extra_search_paths"] == [
            "~/.local/bin/gemini",
            "/usr/local/bin/gemini",
            "/usr/bin/gemini",
        ]
    finally:
        await sm.stop()


@pytest.mark.asyncio
async def test_prewarm_swallows_resolver_errors():
    """A flaky warmup shouldn't block the session from opening — the
    user just pays the cost on the real first turn instead."""
    sm = GeminiSessionManager(config=_ssh_cfg())
    with patch("manager.gemini.session.probe_host_reachable", return_value=True), \
         patch(
            "manager.gemini.session.resolve_remote_cli_path",
            side_effect=RuntimeError("boom"),
         ):
        # Should NOT raise.
        await sm.start()
    try:
        from manager.types import SessionStatus
        assert sm.status == SessionStatus.IDLE
    finally:
        await sm.stop()


@pytest.mark.asyncio
async def test_prewarm_runs_local_version_check_for_local_sessions():
    """Local prewarm spawns `gemini --version` to fault in the Node
    runtime / JS bundle so the FS page cache is hot before the first
    real turn."""
    sm = GeminiSessionManager(config=_local_cfg())
    fake_proc = MagicMock()
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.kill = MagicMock()
    spawn_mock = AsyncMock(return_value=fake_proc)
    with patch(
        "manager.gemini.session.asyncio.create_subprocess_exec", new=spawn_mock,
    ):
        await sm.start()
    try:
        spawn_mock.assert_called_once()
        # First positional arg is the gemini executable, second is "--version".
        args = spawn_mock.call_args.args
        assert args[1] == "--version"
    finally:
        await sm.stop()
