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


@pytest.mark.timeout(5)
def test_query_close_patched_to_bound_aexit() -> None:
    """Importing manager.claude.session must monkey-patch the SDK's
    Query.close() with the bounded variant (claude-agent-sdk#378 defense)."""
    # Trigger import (idempotent if already loaded).
    import manager.claude.session  # noqa: F401
    from claude_agent_sdk._internal import query as q

    assert getattr(q.Query.close, "_bounded_patched", False), (
        "Query.close() was not patched — anyio busy-loop defense is missing"
    )
