"""Tests for the event-loop liveness watchdog."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
import time

import pytest


def _wedged_loop_target(deadline: float) -> None:
    """Subprocess target: start a watchdog, then block the loop for >deadline.

    Should exit with code 1 once the watchdog fires.
    """
    # Re-add repo root to sys.path (multiprocessing spawn loses our test env).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from manager.loop_watchdog import start_loop_watchdog

    async def main() -> None:
        loop = asyncio.get_running_loop()
        start_loop_watchdog(loop, interval_seconds=0.2, deadline_seconds=deadline)

        def block() -> None:
            # Spin the loop thread; CPU-bound code never yields to call_soon.
            end = time.time() + deadline * 5
            while time.time() < end:
                pass

        loop.call_soon(block)
        await asyncio.sleep(deadline * 10)  # Should be interrupted by os._exit

    asyncio.run(main())


def _healthy_loop_target(deadline: float, settle: float) -> None:
    """Subprocess target: start a watchdog, sit idle for `settle` seconds.

    Should NOT exit; the parent kills it after a timeout.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from manager.loop_watchdog import start_loop_watchdog

    async def main() -> None:
        loop = asyncio.get_running_loop()
        start_loop_watchdog(loop, interval_seconds=0.2, deadline_seconds=deadline)
        await asyncio.sleep(settle)
        # Signal success by exiting 0.
        sys.exit(0)

    asyncio.run(main())


def _degraded_loop_target(
    interval: float,
    deadline: float,
    degraded_latency: float,
    degraded_strikes: int,
) -> None:
    """Subprocess target: start a watchdog, then schedule a callback that
    monopolizes the loop for longer than ``degraded_latency`` on every
    iteration — fast enough to ack the heartbeat (so liveness passes)
    but slow enough that callback latency stays above the degraded
    threshold consistently.  Should exit with code 1 once degradation is
    detected.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from manager.loop_watchdog import start_loop_watchdog

    async def main() -> None:
        loop = asyncio.get_running_loop()
        start_loop_watchdog(
            loop,
            interval_seconds=interval,
            deadline_seconds=deadline,
            degraded_latency_seconds=degraded_latency,
            degraded_consecutive_strikes=degraded_strikes,
        )

        # CPU-bound callback that chains itself via call_soon — directly
        # mimics anyio's _deliver_cancellation retry-forever pattern.
        # Each hog() iteration burns long enough to GUARANTEE that any
        # heartbeat ping queued during it sees a latency well above the
        # degraded threshold (block 3x threshold gives ~10x headroom over
        # measurement jitter).
        block_for = degraded_latency * 3.0

        def hog() -> None:
            end = time.time() + block_for
            while time.time() < end:
                pass
            loop.call_soon(hog)

        loop.call_soon(hog)
        await asyncio.sleep(deadline * 20)  # Should be interrupted by os._exit

    asyncio.run(main())


@pytest.mark.timeout(15)
def test_watchdog_fires_on_wedged_loop() -> None:
    """If the loop stops servicing callbacks past the deadline, the watchdog
    must force-exit so systemd can restart the process."""
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_wedged_loop_target, args=(1.0,))
    proc.start()
    proc.join(timeout=10)
    assert proc.exitcode is not None, "watchdog did not fire — process still running"
    assert proc.exitcode != 0, f"expected non-zero exit, got {proc.exitcode}"


@pytest.mark.timeout(15)
def test_watchdog_quiet_on_healthy_loop() -> None:
    """A healthy loop must NOT trigger the watchdog within the settle window."""
    ctx = multiprocessing.get_context("spawn")
    # deadline=1.0, settle for 3.0s — watchdog should not fire.
    proc = ctx.Process(target=_healthy_loop_target, args=(1.0, 3.0))
    proc.start()
    proc.join(timeout=10)
    assert proc.exitcode == 0, (
        f"watchdog erroneously fired on healthy loop (exit={proc.exitcode})"
    )


@pytest.mark.timeout(30)
def test_watchdog_fires_on_degraded_loop() -> None:
    """A loop that's alive (callbacks DO execute) but pinned by a runaway
    callback that monopolizes most of every tick should be detected via
    sustained-latency strikes and force a restart.  This is the failure
    mode the original liveness-only check missed when anyio's
    _deliver_cancellation pinned uvicorn at 112% CPU (claude-agent-sdk
    #378 pre-fix)."""
    ctx = multiprocessing.get_context("spawn")
    # interval 0.2s, deadline 2.0s (so liveness check passes; deadline
    # doubles as the warmup pre-probe sleep), latency threshold 0.3s,
    # 3 strikes — degraded loop should fire within ~5s after the
    # warmup window.
    proc = ctx.Process(
        target=_degraded_loop_target,
        args=(0.2, 2.0, 0.3, 3),
    )
    proc.start()
    proc.join(timeout=25)
    assert proc.exitcode is not None, (
        "watchdog did not fire on degraded loop — process still running"
    )
    assert proc.exitcode != 0, (
        f"expected non-zero exit on degraded loop, got {proc.exitcode}"
    )


@pytest.mark.timeout(5)
def test_sdk_query_no_longer_uses_anyio_taskgroup() -> None:
    """Sanity: the installed claude-agent-sdk must be ≥0.1.51, where PR #746
    replaced ``anyio.TaskGroup`` in ``Query`` with ``asyncio.create_task``.
    The TaskGroup was the source of the cross-task ``__aexit__`` wedge that
    pinned the event loop via anyio's ``_deliver_cancellation`` retry loop
    (claude-agent-sdk#378). If a regression downgrades the SDK and this
    test starts failing, restore the monkey-patch in manager/claude/session.py
    until the floor is bumped again."""
    import inspect

    from claude_agent_sdk._internal import query as q

    close_src = inspect.getsource(q.Query.close)
    assert "self._tg" not in close_src and "TaskGroup" not in close_src, (
        "Query.close() still references anyio.TaskGroup — SDK is too old "
        "(need ≥0.1.81 per requirements-claude.txt)"
    )
