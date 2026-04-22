"""Regression tests for the 2026-04-20 SSH session-churn crash.

Three defenses are exercised here:
1. The remote-claude path probe in ``_write_ssh_wrapper`` is cached per
   (host, user, key), so a burst of concurrent starts opens at most one
   probe SSH per target.
2. ``SessionPool.create`` serializes concurrent calls that target the same
   remote SSH host, so a reconnect storm turns into sequential attempts
   instead of a thundering herd.
3. The ``No conversation found`` fallback in ``SessionPool.create`` retries
   at most once, with a short backoff.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from api.pool import SessionPool
from manager.config import ManagerConfig
from manager.session import (
    SessionManager,
    _REMOTE_CLAUDE_PATH_CACHE,
    clear_remote_claude_path_cache,
)


# ----------------------------------------------------------------------
# Fix 1 — remote-claude path is probed once per target, not per start
# ----------------------------------------------------------------------


def _ssh_config(host: str = "10.0.0.1") -> ManagerConfig:
    return ManagerConfig(
        project_dir="/remote/project",
        ssh_host=host,
        ssh_user="agent",
        ssh_claude_config_dir="/remote/project/.claude_config",
    )


def test_remote_claude_path_cached_between_wrapper_writes():
    clear_remote_claude_path_cache()
    cfg = _ssh_config()
    mock_result = MagicMock(stdout="/home/agent/.local/bin/claude\n")

    with patch("manager.session.subprocess.run", return_value=mock_result) as run:
        sm1 = SessionManager(config=cfg)
        sm2 = SessionManager(config=cfg)
        path1 = sm1._write_ssh_wrapper()
        path2 = sm2._write_ssh_wrapper()

    # subprocess.run was called exactly once — second SessionManager hit the cache.
    assert run.call_count == 1
    assert _REMOTE_CLAUDE_PATH_CACHE[(cfg.ssh_host, cfg.ssh_user, cfg.ssh_key)] == (
        "/home/agent/.local/bin/claude"
    )
    # Both wrappers were still written successfully (different temp files).
    assert path1 != path2


def test_remote_claude_path_cache_keyed_per_host():
    clear_remote_claude_path_cache()
    cfg_a = _ssh_config("10.0.0.1")
    cfg_b = _ssh_config("10.0.0.2")
    mock_result = MagicMock(stdout="/usr/local/bin/claude\n")

    with patch("manager.session.subprocess.run", return_value=mock_result) as run:
        SessionManager(config=cfg_a)._write_ssh_wrapper()
        SessionManager(config=cfg_a)._write_ssh_wrapper()
        SessionManager(config=cfg_b)._write_ssh_wrapper()
        SessionManager(config=cfg_b)._write_ssh_wrapper()

    # Two unique targets => two probes, regardless of how many starts.
    assert run.call_count == 2


def test_remote_claude_path_cache_remembers_fallback():
    """A failed probe (TimeoutError) should cache the 'claude' fallback, not retry."""
    clear_remote_claude_path_cache()
    cfg = _ssh_config()

    with patch("manager.session.subprocess.run", side_effect=TimeoutError()) as run:
        SessionManager(config=cfg)._write_ssh_wrapper()
        SessionManager(config=cfg)._write_ssh_wrapper()
        SessionManager(config=cfg)._write_ssh_wrapper()

    # Only one probe, despite all failing.
    assert run.call_count == 1
    assert _REMOTE_CLAUDE_PATH_CACHE[(cfg.ssh_host, cfg.ssh_user, cfg.ssh_key)] == "claude"


# ----------------------------------------------------------------------
# Fix 2 — SessionPool.create serializes concurrent SSH-host creates
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_serializes_same_host_concurrent_creates():
    """10 concurrent create()s on the same SSH host ⇒ only one active at a time."""
    pool = SessionPool()
    cfg = _ssh_config()

    concurrent_count = 0
    max_concurrent = 0
    active_lock = asyncio.Lock()

    async def fake_start(self):
        nonlocal concurrent_count, max_concurrent
        async with active_lock:
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
        # Simulate the SDK spawn taking a moment.
        await asyncio.sleep(0.05)
        async with active_lock:
            concurrent_count -= 1
        self._sdk_session_id = f"sdk-{self._local_id}"
        return self._local_id

    with patch.object(SessionManager, "start", fake_start):
        results = await asyncio.gather(
            *[pool.create(cfg) for _ in range(10)]
        )

    assert len(results) == 10
    assert len(set(results)) == 10, "each call should produce a distinct local_id"
    assert max_concurrent == 1, (
        f"expected serialized access but saw {max_concurrent} concurrent starts"
    )


@pytest.mark.asyncio
async def test_pool_does_not_serialize_across_different_hosts():
    """Two different SSH hosts should be able to run in parallel."""
    pool = SessionPool()
    cfg_a = _ssh_config("10.0.0.1")
    cfg_b = _ssh_config("10.0.0.2")

    observed_concurrency = 0
    max_concurrent = 0
    active_lock = asyncio.Lock()

    async def fake_start(self):
        nonlocal observed_concurrency, max_concurrent
        async with active_lock:
            observed_concurrency += 1
            max_concurrent = max(max_concurrent, observed_concurrency)
        await asyncio.sleep(0.05)
        async with active_lock:
            observed_concurrency -= 1
        self._sdk_session_id = f"sdk-{self._local_id}"
        return self._local_id

    with patch.object(SessionManager, "start", fake_start):
        await asyncio.gather(pool.create(cfg_a), pool.create(cfg_b))

    # Different hosts should NOT be serialized — we expect 2 in flight together.
    assert max_concurrent == 2


@pytest.mark.asyncio
async def test_pool_local_sessions_are_not_serialized():
    """Local (non-SSH) creates must not be held behind an SSH lock."""
    pool = SessionPool()
    local_cfg = ManagerConfig(project_dir="/local/project")  # no ssh_host

    max_concurrent = 0
    active = 0
    active_lock = asyncio.Lock()

    async def fake_start(self):
        nonlocal active, max_concurrent
        async with active_lock:
            active += 1
            max_concurrent = max(max_concurrent, active)
        await asyncio.sleep(0.02)
        async with active_lock:
            active -= 1
        self._sdk_session_id = f"sdk-{self._local_id}"
        return self._local_id

    with patch.object(SessionManager, "start", fake_start):
        await asyncio.gather(*[pool.create(local_cfg) for _ in range(5)])

    # 5 local starts should run fully in parallel.
    assert max_concurrent == 5


# ----------------------------------------------------------------------
# Fix 3 — "No conversation found" fallback backs off and retries at most once
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_resume_fallback_retries_only_once_with_backoff():
    """If the fresh-session retry also fails, the error must surface."""
    pool = SessionPool()
    cfg = ManagerConfig(project_dir="/local/project")

    call_count = 0

    async def always_fail(self):
        nonlocal call_count
        call_count += 1
        err = RuntimeError("Process failed: No conversation found for session")
        err.stderr = "No conversation found for session"
        raise err

    with patch.object(SessionManager, "start", always_fail):
        with patch("api.pool.asyncio.sleep") as sleep_mock:
            with pytest.raises(RuntimeError):
                await pool.create(cfg, resume_sdk_id="missing-id")

    # Exactly two start() attempts: the original resume, then one retry.
    assert call_count == 2
    # The retry must be preceded by a backoff sleep in the 0.5–1.0s range.
    assert sleep_mock.await_count == 1
    slept = sleep_mock.await_args.args[0]
    assert 0.5 <= slept <= 1.0
