"""Event-loop liveness watchdog.

A background daemon thread heartbeats the asyncio event loop every few seconds
by scheduling a no-op coroutine via ``call_soon_threadsafe``.  If the loop
fails to execute that callback within a generous deadline, the thread calls
``os._exit(1)`` so systemd restarts the service.

This is a last-line defense against bugs that pin the event loop at 100% CPU
with no progress — most notably claude-agent-sdk#378 / anyio#695, where a
cancel scope inside the SDK's task group reschedules ``_deliver_cancellation``
via ``call_soon`` forever.  In that state the loop is technically still
running, so a passive heartbeat suffices to detect it: our scheduled callback
sits in the queue and never gets its slot.

Why a thread (not an asyncio task): a coroutine on the same starved loop
can't detect the wedge — it's competing with the busy callback for runtime.
A daemon thread on the GIL is fine because the runaway is asyncio callbacks,
not Python bytecode in the watchdog's window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)


def start_loop_watchdog(
    loop: asyncio.AbstractEventLoop,
    *,
    interval_seconds: float = 5.0,
    deadline_seconds: float = 30.0,
) -> threading.Thread:
    """Spawn a daemon thread that os._exit(1)s if the loop stops servicing
    ``call_soon_threadsafe`` callbacks within ``deadline_seconds``.

    Returns the thread (already started) for visibility / introspection;
    callers don't need to hold the reference.
    """

    def _heartbeat() -> None:
        # Sleep before first probe so startup work (imports, prewarm) doesn't
        # trigger a false positive.
        time.sleep(deadline_seconds)
        while True:
            ack = threading.Event()

            def _ping() -> None:
                ack.set()

            try:
                loop.call_soon_threadsafe(_ping)
            except RuntimeError:
                # Loop is closed (normal shutdown) — exit cleanly.
                return

            if not ack.wait(timeout=deadline_seconds):
                # The loop accepted our callback (call_soon_threadsafe didn't
                # raise) but failed to execute it within the deadline.
                # Something is monopolizing the loop.  Force restart.
                msg = (
                    f"event-loop watchdog: no callback execution in "
                    f"{deadline_seconds:.0f}s — exiting so systemd can restart"
                )
                logger.critical(msg)
                # Print to stderr too in case logging itself is wedged.
                print(msg, file=sys.stderr, flush=True)
                os._exit(1)

            time.sleep(interval_seconds)

    thread = threading.Thread(
        target=_heartbeat,
        name="event-loop-watchdog",
        daemon=True,
    )
    thread.start()
    logger.info(
        "event-loop watchdog started (interval=%.1fs, deadline=%.1fs)",
        interval_seconds,
        deadline_seconds,
    )
    return thread
