"""Tests for the per-session ``voice_start`` lock in
``api/routes/orchestrator``.

These guard against the duplicate-handshake bug observed 2026-06-04:
three back-to-back ``voice_start`` calls for the same local_id raced
through the handler and opened three Google Live WS handshakes against
the same stale ``sessionResumption`` handle, all subsequently 1008'd.
"""

from __future__ import annotations

import asyncio

import pytest

from api.routes.orchestrator import (
    _VOICE_START_LOCKS,
    _voice_start_lock_for,
)


@pytest.fixture(autouse=True)
def _clear_lock_registry():
    """Each test starts with an empty lock dict so they don't bleed."""
    _VOICE_START_LOCKS.clear()
    yield
    _VOICE_START_LOCKS.clear()


def test_same_local_id_returns_same_lock_instance():
    a = _voice_start_lock_for("session-abc")
    b = _voice_start_lock_for("session-abc")
    assert a is b


def test_distinct_local_ids_get_distinct_locks():
    a = _voice_start_lock_for("session-aaa")
    b = _voice_start_lock_for("session-bbb")
    assert a is not b


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_acquisitions():
    """Holding the lock blocks a second async waiter until release."""
    lock = _voice_start_lock_for("session-serial")
    order: list[str] = []

    async def waiter(label: str, hold_ms: int):
        async with lock:
            order.append(f"enter:{label}")
            await asyncio.sleep(hold_ms / 1000)
            order.append(f"exit:{label}")

    # Schedule three competing acquisitions. The first one in wins and
    # the others must wait for it; ordering should be strictly nested,
    # never interleaved.
    await asyncio.gather(
        waiter("A", 30),
        waiter("B", 5),
        waiter("C", 5),
    )

    # Walk the sequence: every "enter:X" must be immediately followed by
    # "exit:X" (no other enter in between) — i.e. serial, not concurrent.
    for i in range(0, len(order), 2):
        enter_label = order[i].split(":")[1]
        exit_label = order[i + 1].split(":")[1]
        assert order[i].startswith("enter:")
        assert order[i + 1].startswith("exit:")
        assert enter_label == exit_label, (
            f"interleaved acquisition: {order[i]!r} followed by {order[i + 1]!r} "
            f"(full sequence: {order})"
        )
