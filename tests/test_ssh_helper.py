"""Tests for ``manager._ssh`` — the shared SSH primitives both session
managers use to wrap their respective CLIs.

These tests cover the primitives in isolation; provider-specific
integration (e.g. "QwenSessionManager spawns the right SSH argv") lives
in ``test_qwen_session.py`` and ``test_ssh_session_churn.py``.

The two design properties we pin here:

1. **Per-provider isolation** — two providers on the same host get
   distinct ControlMaster sockets and distinct cache entries.
2. **No env-leak via export** — the rendered remote command uses inline
   ``VAR=val exec ...``, never ``export VAR=val``, because the latter
   makes bash dump ``declare -x`` on stdout and corrupts our JSON event
   stream.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from manager._ssh import (
    RemoteCommand,
    SshTarget,
    _REMOTE_CLI_PATH_CACHE,
    build_remote_argv,
    build_ssh_argv,
    cleanup_ssh_wrapper_script,
    clear_remote_cli_path_cache,
    get_cached_remote_cli_path,
    probe_host_reachable,
    resolve_remote_cli_path,
    set_cached_remote_cli_path,
    write_ssh_wrapper_script,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """Every test starts with an empty CLI-path cache."""
    clear_remote_cli_path_cache()
    yield
    clear_remote_cli_path_cache()


# ---------------------------------------------------------------------------
# probe_host_reachable
# ---------------------------------------------------------------------------


def test_probe_empty_host_returns_true():
    """An empty host string means "no SSH" — treat as reachable so the
    local-only code paths don't accidentally short-circuit on probe."""
    assert probe_host_reachable("") is True


def test_probe_returns_true_on_zero_exit():
    """Successful ping (exit 0) → reachable."""
    with patch("manager._ssh.subprocess.run", return_value=MagicMock(returncode=0)):
        assert probe_host_reachable("10.0.0.1") is True


def test_probe_returns_false_on_nonzero_exit():
    """Failed ping (any non-zero) → unreachable."""
    with patch("manager._ssh.subprocess.run", return_value=MagicMock(returncode=1)):
        assert probe_host_reachable("10.254.254.254") is False


def test_probe_returns_false_on_ping_missing():
    """`ping` binary not on PATH (containerized envs, minimal images) →
    unreachable.  Conservative: better to fail fast than open a hung
    SSH connection."""
    with patch("manager._ssh.subprocess.run", side_effect=FileNotFoundError()):
        assert probe_host_reachable("10.0.0.1") is False


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_keyed_per_provider():
    """The cache key is (cli_name, host, user, key) — two providers on
    the same target must not share entries.  If they did, a Claude probe
    that resolved to /usr/local/bin/claude would corrupt Qwen's lookup."""
    set_cached_remote_cli_path("claude", "10.0.0.1", "agent", None, "/r/claude")
    set_cached_remote_cli_path("qwen",   "10.0.0.1", "agent", None, "/r/qwen")

    assert get_cached_remote_cli_path("claude", "10.0.0.1", "agent", None) == "/r/claude"
    assert get_cached_remote_cli_path("qwen",   "10.0.0.1", "agent", None) == "/r/qwen"


def test_cache_clear_all():
    set_cached_remote_cli_path("claude", "10.0.0.1", None, None, "/r/claude")
    set_cached_remote_cli_path("qwen",   "10.0.0.1", None, None, "/r/qwen")
    clear_remote_cli_path_cache()
    assert get_cached_remote_cli_path("claude", "10.0.0.1", None, None) is None
    assert get_cached_remote_cli_path("qwen",   "10.0.0.1", None, None) is None


def test_cache_clear_one_provider():
    """``clear_remote_cli_path_cache("claude")`` flushes only claude rows
    — useful when the operator updates one provider's remote install but
    not the other's."""
    set_cached_remote_cli_path("claude", "10.0.0.1", None, None, "/r/claude")
    set_cached_remote_cli_path("qwen",   "10.0.0.1", None, None, "/r/qwen")
    clear_remote_cli_path_cache("claude")
    assert get_cached_remote_cli_path("claude", "10.0.0.1", None, None) is None
    assert get_cached_remote_cli_path("qwen",   "10.0.0.1", None, None) == "/r/qwen"


# ---------------------------------------------------------------------------
# SshTarget + build_ssh_argv
# ---------------------------------------------------------------------------


def test_build_ssh_argv_carries_multiplexing_flags():
    """ControlMaster + ControlPersist are what kept the Jetson from
    melting under the 2026-04-20 churn — regress-protect them."""
    target = SshTarget(host="10.0.0.1", user="agent", key="/k", control_path_prefix="claude")
    argv = build_ssh_argv(target)

    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "ControlMaster=auto" in argv
    assert "ControlPersist=60s" in argv
    assert any("ControlPath=/tmp/claude-ssh-10.0.0.1-" in s for s in argv)
    assert "-i" in argv and "/k" in argv
    assert argv[-1] == "agent@10.0.0.1"


def test_build_ssh_argv_per_provider_control_path():
    """Per-provider ControlMaster socket prevents one provider's
    ControlPersist timeout from tearing down the other's in-flight
    multiplexed connection."""
    claude = build_ssh_argv(SshTarget(host="h", control_path_prefix="claude"))
    qwen   = build_ssh_argv(SshTarget(host="h", control_path_prefix="qwen"))

    assert any("/tmp/claude-ssh-h-" in s for s in claude)
    assert any("/tmp/qwen-ssh-h-"   in s for s in qwen)
    # And the two don't collide:
    assert not any("/tmp/qwen-ssh-h-"   in s for s in claude)
    assert not any("/tmp/claude-ssh-h-" in s for s in qwen)


def test_build_ssh_argv_without_user():
    """No user → SSH config decides (per-host User in ~/.ssh/config etc.)."""
    argv = build_ssh_argv(SshTarget(host="h"))
    assert argv[-1] == "h"
    assert all("@" not in s for s in argv if s != "h")


# ---------------------------------------------------------------------------
# RemoteCommand
# ---------------------------------------------------------------------------


def test_remote_command_renders_with_env_prefix():
    """KEY=val prefix, not `export KEY=val`.  See the docstring on
    RemoteCommand for why this matters."""
    cmd = RemoteCommand(
        project_dir="/r/proj",
        remote_cli="/r/bin/claude",
        env={"CLAUDE_CONFIG_DIR": "/r/proj/.claude_config"},
    )
    rendered = cmd.render_shell()

    assert "export" not in rendered
    assert "cd '/r/proj'" in rendered
    assert "CLAUDE_CONFIG_DIR='/r/proj/.claude_config'" in rendered
    assert "exec '/r/bin/claude'" in rendered


def test_remote_command_quotes_path_with_special_chars():
    """A project_dir with a single quote in it shouldn't break the shell."""
    cmd = RemoteCommand(
        project_dir="/r/it's-a-project",
        remote_cli="/r/bin/qwen",
    )
    rendered = cmd.render_shell()
    # The break-and-rejoin form is `'...'\''...'` — the path becomes
    # /r/it's-a-project after the shell consumes the quoting.
    assert "/r/it'\\''s-a-project" in rendered


def test_remote_command_no_env_renders_clean():
    """No env vars → no leading env-prefix garbage."""
    cmd = RemoteCommand(project_dir="/p", remote_cli="/c")
    rendered = cmd.render_shell()
    assert rendered == "cd '/p' && exec '/c'"


# ---------------------------------------------------------------------------
# resolve_remote_cli_path
# ---------------------------------------------------------------------------


def test_resolve_uses_cache_on_second_call():
    """A burst of N concurrent starts on the same target should fire at
    most one SSH probe (the rest hit the cache).  This is what kept the
    laptop alive during reconnect storms."""
    target = SshTarget(host="10.0.0.1", user="agent", control_path_prefix="qwen")
    mock_run = MagicMock(return_value=MagicMock(stdout="/r/bin/qwen\n"))
    with patch("manager._ssh.probe_host_reachable", return_value=True), \
         patch("manager._ssh.subprocess.run", mock_run):
        a = resolve_remote_cli_path("qwen", target)
        b = resolve_remote_cli_path("qwen", target)
        c = resolve_remote_cli_path("qwen", target)
    assert a == b == c == "/r/bin/qwen"
    assert mock_run.call_count == 1


def test_resolve_unreachable_host_returns_fallback_without_cache():
    """Failed ICMP probe → bare cli_name fallback, NOT cached.  When the
    host comes back online we want a real probe, not stuck-on-fallback."""
    target = SshTarget(host="10.254.254.254", control_path_prefix="qwen")
    with patch("manager._ssh.probe_host_reachable", return_value=False), \
         patch("manager._ssh.subprocess.run") as mock_run:
        out = resolve_remote_cli_path("qwen", target)
    assert out == "qwen"
    assert mock_run.call_count == 0
    # No cache entry — next call should re-probe.
    assert get_cached_remote_cli_path("qwen", "10.254.254.254", None, None) is None


def test_resolve_failed_probe_caches_fallback():
    """If ICMP succeeds but the `which` probe times out / errors, we DO
    cache the "qwen" fallback — the host is up but misconfigured, and
    re-probing won't help."""
    target = SshTarget(host="10.0.0.1", control_path_prefix="qwen")
    with patch("manager._ssh.probe_host_reachable", return_value=True), \
         patch("manager._ssh.subprocess.run", side_effect=TimeoutError):
        resolve_remote_cli_path("qwen", target)
    assert get_cached_remote_cli_path("qwen", "10.0.0.1", None, None) == "qwen"


def test_resolve_includes_extra_search_paths_in_probe():
    """The fallback chain (~/.local/bin/qwen, /usr/local/bin/qwen, ...)
    is what catches setups where `which qwen` runs without the user's
    full PATH (cron-style shells).  Make sure they end up in the probe."""
    target = SshTarget(host="10.0.0.1", control_path_prefix="qwen")
    captured = {}
    def fake_run(argv, **kw):
        captured["argv"] = argv
        return MagicMock(stdout="/r/bin/qwen\n")
    with patch("manager._ssh.probe_host_reachable", return_value=True), \
         patch("manager._ssh.subprocess.run", side_effect=fake_run):
        resolve_remote_cli_path(
            "qwen", target,
            extra_search_paths=["~/.local/bin/qwen", "/usr/local/bin/qwen"],
        )
    probe_cmd = captured["argv"][-1]
    assert "~/.local/bin/qwen" in probe_cmd
    assert "/usr/local/bin/qwen" in probe_cmd


# ---------------------------------------------------------------------------
# build_remote_argv (Qwen-style direct argv)
# ---------------------------------------------------------------------------


def test_build_remote_argv_emits_single_quoted_command():
    """SSH must see ONE trailing argument (the whole remote command), not
    one per word — otherwise the remote shell splits things at spaces
    and `cd` runs in the wrong place.  See _ssh.write_ssh_wrapper_script
    for the long explanation; this is the same trick, applied to argv-
    style invocation."""
    target = SshTarget(host="h", control_path_prefix="qwen")
    cmd = RemoteCommand(project_dir="/p", remote_cli="/r/qwen")
    argv = build_remote_argv(
        target=target,
        remote_cmd=cmd,
        remote_args=["--input-format", "stream-json", "--resume", "sid-abc"],
    )

    # First N items: ssh + flags + target.  Last item: the whole remote command.
    assert argv[0] == "ssh"
    assert argv[-2] == "h"  # the host argument
    remote_cmd_str = argv[-1]
    # cd + exec + args all in one trailing string
    assert remote_cmd_str.startswith("cd '/p' && exec '/r/qwen'")
    assert "'--input-format'" in remote_cmd_str
    assert "'stream-json'" in remote_cmd_str
    assert "'--resume'" in remote_cmd_str
    assert "'sid-abc'" in remote_cmd_str


def test_build_remote_argv_quotes_args_with_spaces():
    """Pathological arg values shouldn't break the remote parse."""
    target = SshTarget(host="h", control_path_prefix="qwen")
    cmd = RemoteCommand(project_dir="/p", remote_cli="/r/qwen")
    argv = build_remote_argv(
        target=target,
        remote_cmd=cmd,
        remote_args=["--system-prompt", "hello world; rm -rf /"],
    )
    # `; rm -rf /` should be quoted, not interpreted as a separator.
    assert "'hello world; rm -rf /'" in argv[-1]


# ---------------------------------------------------------------------------
# write_ssh_wrapper_script / cleanup_ssh_wrapper_script
# ---------------------------------------------------------------------------


def test_wrapper_script_is_executable_and_owner_only(tmp_path):
    """0o700: owner-only execute, no world-readable secrets in the
    inevitable race window before cleanup."""
    target = SshTarget(host="h", control_path_prefix="claude")
    cmd = RemoteCommand(project_dir="/p", remote_cli="/r/claude").render_shell()
    path = write_ssh_wrapper_script(
        ssh_argv=build_ssh_argv(target),
        remote_cmd=cmd,
        prefix="claude",
    )
    try:
        from pathlib import Path
        import stat as st
        mode = Path(path).stat().st_mode & 0o777
        assert mode == 0o700, f"wrapper should be 0o700, got {oct(mode)}"
        assert st.S_ISREG(Path(path).stat().st_mode)
    finally:
        cleanup_ssh_wrapper_script(path)


def test_wrapper_script_forwards_args_via_dollar_at():
    """The wrapper's whole job is forwarding ``"$@"`` to the remote;
    pin that the generated script actually contains the trick."""
    target = SshTarget(host="h", control_path_prefix="claude")
    cmd = RemoteCommand(project_dir="/p", remote_cli="/r/claude").render_shell()
    path = write_ssh_wrapper_script(
        ssh_argv=build_ssh_argv(target),
        remote_cmd=cmd,
        prefix="claude",
    )
    try:
        from pathlib import Path
        content = Path(path).read_text()
        assert "#!/bin/sh" in content
        # The shell-quoting loop:
        assert 'for _a in "$@"' in content
        # Single-double-quote-around-the-remote-cmd trick:
        assert 'exec ssh' in content
        assert '${_q}' in content
    finally:
        cleanup_ssh_wrapper_script(path)


def test_cleanup_is_idempotent(tmp_path):
    """``finally: cleanup_ssh_wrapper_script(self._path)`` should never
    raise — even when called twice (e.g. on both error and finally)."""
    target = SshTarget(host="h", control_path_prefix="claude")
    cmd = RemoteCommand(project_dir="/p", remote_cli="/r/claude").render_shell()
    path = write_ssh_wrapper_script(
        ssh_argv=build_ssh_argv(target),
        remote_cmd=cmd,
        prefix="claude",
    )
    cleanup_ssh_wrapper_script(path)
    cleanup_ssh_wrapper_script(path)   # second call must not raise
    cleanup_ssh_wrapper_script(None)   # nor a None
