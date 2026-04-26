"""Regression tests for the 2026-04-26 leaked-claude-subprocess incident.

On the Jetson we observed two `claude` subprocesses (PIDs 9614 and 9615)
that the SDK had spawned for sessions which the user later closed in the
UI.  After close the sessions disappeared from the pool, but the bundled
claude binaries kept running — accumulating ~5% sustained CPU between
them and spinning the fan.

Root cause was a chain of three problems:

* The SDK's transport.close() does ``self._process.terminate()`` followed
  by an unbounded ``await self._process.wait()`` — if claude ignores
  SIGTERM (it can take seconds to flush JSONL on slow storage), the wait
  blocks forever.
* Because the cancel-scope RuntimeError was raised earlier in the chain,
  ``transport.close()`` was sometimes never called at all (already fixed
  in the lifecycle-task rework).
* ``pool.close()``'s 10s ``asyncio.wait_for`` cancels the request
  handler's wait but doesn't cancel the lifecycle task itself, so the
  hung disconnect just keeps running in the background.

Two defenses are exercised here:

1. **Per-session SIGKILL fallback** in ``SessionManager._lifecycle``: if
   ``client.disconnect()`` exceeds 8s, we capture the bundled-claude PID
   ourselves (via ``client._transport._process.pid``) and force-kill.

2. **Pool orphan reaper**: a background task that periodically scans
   tracked PIDs and SIGKILLs any whose owning session has been gone from
   the pool for more than ``orphan_grace_seconds`` — last-line defense
   in case the per-session path was bypassed (lifecycle task cancelled
   hard, SDK refactored its private transport shape, etc.).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from api.pool import SessionPool
from manager.config import ManagerConfig
from manager.session import SessionManager


# ----------------------------------------------------------------------
# Fix 2 — pool's orphan reaper
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_tracks_pid_when_session_created():
    """When create() succeeds and SessionManager.subprocess_pid is set,
    the pool registers it in _tracked_pids."""
    pool = SessionPool()
    cfg = ManagerConfig(project_dir="/local/project")

    async def fake_start(self):
        self._sdk_session_id = f"sdk-{self._local_id}"
        self._subprocess_pid = 12345

    with patch.object(SessionManager, "start", fake_start):
        sid = await pool.create(cfg)

    assert 12345 in pool._tracked_pids
    assert pool._tracked_pids[12345][0] == sid


@pytest.mark.asyncio
async def test_reaper_skips_tracking_when_pid_unavailable():
    """If the SDK's private transport shape changed and we couldn't
    capture a pid, _tracked_pids stays empty — the reaper relies on
    other signals (none in this case) but we still don't crash."""
    pool = SessionPool()
    cfg = ManagerConfig(project_dir="/local/project")

    async def fake_start(self):
        self._sdk_session_id = f"sdk-{self._local_id}"
        self._subprocess_pid = None  # SDK shape changed

    with patch.object(SessionManager, "start", fake_start):
        await pool.create(cfg)

    assert pool._tracked_pids == {}


@pytest.mark.asyncio
async def test_reaper_moves_pid_to_closed_set_on_close():
    """When close() runs, the pid migrates from _tracked_pids to
    _closed_session_pids so the reaper can apply the grace period."""
    pool = SessionPool()
    cfg = ManagerConfig(project_dir="/local/project")

    async def fake_start(self):
        self._sdk_session_id = f"sdk-{self._local_id}"
        self._subprocess_pid = 54321

    async def fake_stop(self):
        pass

    with patch.object(SessionManager, "start", fake_start), \
         patch.object(SessionManager, "stop", fake_stop):
        sid = await pool.create(cfg)
        await pool.close(sid)

    assert 54321 not in pool._tracked_pids
    assert sid in pool._closed_session_pids
    assert pool._closed_session_pids[sid][0] == 54321


def test_reaper_pass_kills_orphan_after_grace_period():
    """The reaper should SIGKILL a tracked closed-session pid whose
    grace period expired AND that's still alive AND looks like claude."""
    pool = SessionPool()
    # Manually plant an "expired" closed session
    pool._closed_session_pids["sess-x"] = (99001, time.monotonic() - 60.0)

    with patch("api.pool._process_alive", return_value=True), \
         patch("api.pool._looks_like_claude", return_value=True), \
         patch("api.pool.kill_claude_subprocess", return_value=True) as kill_mock:
        pool._reap_orphans_once(orphan_grace_seconds=30.0)

    kill_mock.assert_called_once_with(99001)
    # And the bookkeeping is cleaned up
    assert "sess-x" not in pool._closed_session_pids


def test_reaper_respects_grace_period():
    """Within the grace window the reaper must NOT touch the pid — it
    gives the per-session SIGKILL path a chance to run first."""
    pool = SessionPool()
    pool._closed_session_pids["sess-y"] = (99002, time.monotonic() - 5.0)

    with patch("api.pool._process_alive", return_value=True), \
         patch("api.pool._looks_like_claude", return_value=True), \
         patch("api.pool.kill_claude_subprocess", return_value=True) as kill_mock:
        pool._reap_orphans_once(orphan_grace_seconds=30.0)

    kill_mock.assert_not_called()
    # Bookkeeping retained for the next pass
    assert "sess-y" in pool._closed_session_pids


def test_reaper_skips_pid_that_no_longer_looks_like_claude():
    """If the pid was reused by the kernel for an unrelated process
    after the bundled claude exited cleanly, we must NOT kill it."""
    pool = SessionPool()
    pool._closed_session_pids["sess-z"] = (99003, time.monotonic() - 60.0)

    with patch("api.pool._process_alive", return_value=True), \
         patch("api.pool._looks_like_claude", return_value=False), \
         patch("api.pool.kill_claude_subprocess") as kill_mock:
        pool._reap_orphans_once(orphan_grace_seconds=30.0)

    kill_mock.assert_not_called()
    # Bookkeeping is dropped — the pid is no longer ours to track
    assert "sess-z" not in pool._closed_session_pids


def test_reaper_drops_dead_tracked_pids():
    """For pids in _tracked_pids whose process has already exited
    (clean shutdown via the per-session path), the reaper just removes
    them from bookkeeping — no signals, no log spam."""
    pool = SessionPool()
    pool._tracked_pids[99004] = ("sess-live", time.monotonic())

    with patch("api.pool._process_alive", return_value=False), \
         patch("api.pool.kill_claude_subprocess") as kill_mock:
        pool._reap_orphans_once(orphan_grace_seconds=30.0)

    kill_mock.assert_not_called()
    assert 99004 not in pool._tracked_pids


def test_reaper_kills_pid_for_session_that_vanished_without_close():
    """Defensive case: if a session is removed from _sessions WITHOUT
    going through close() (test code, accidental pop), the reaper still
    treats its tracked pid as orphaned and kills it."""
    pool = SessionPool()
    pool._tracked_pids[99005] = ("sess-vanished", time.monotonic() - 60.0)
    # Note: sess-vanished is intentionally NOT in pool._sessions

    with patch("api.pool._process_alive", return_value=True), \
         patch("api.pool._looks_like_claude", return_value=True), \
         patch("api.pool.kill_claude_subprocess", return_value=True) as kill_mock:
        pool._reap_orphans_once(orphan_grace_seconds=30.0)

    kill_mock.assert_called_once_with(99005)
    assert 99005 not in pool._tracked_pids


@pytest.mark.asyncio
async def test_reaper_start_stop_idempotent():
    """start_orphan_reaper() called twice spawns one task; stop is safe
    to call when never started."""
    pool = SessionPool()

    await pool.start_orphan_reaper(interval_seconds=3600)  # never fires
    first = pool._reaper_task
    await pool.start_orphan_reaper(interval_seconds=3600)
    second = pool._reaper_task
    assert first is second  # second call no-oped

    await pool.stop_orphan_reaper()
    assert pool._reaper_task is None
    # Stopping twice is safe
    await pool.stop_orphan_reaper()


@pytest.mark.asyncio
async def test_reaper_loop_actually_calls_reap():
    """Smoke test the loop body — sleep is short, then we cancel."""
    pool = SessionPool()
    call_count = 0

    def counting_reap(grace):
        nonlocal call_count
        call_count += 1

    with patch.object(pool, "_reap_orphans_once", side_effect=counting_reap):
        await pool.start_orphan_reaper(interval_seconds=0.05)
        await asyncio.sleep(0.18)  # ~3 iterations
        await pool.stop_orphan_reaper()

    assert call_count >= 2, f"reaper should have fired at least twice, was {call_count}"
