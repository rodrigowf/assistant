"""Increment C — new behavior tests: single reconnect lock + held outbound
queue + setup idempotency guard.

These pin the post-refactor invariants:

1. **Single lock**: ``_try_reconnect`` acquires ``_reconnect_lock``. Under
   N concurrent calls, exactly ONE close→rebuild→handshake sequence runs;
   the rest coalesce and observe the result. This closes the 2026-06-04
   01:11 "duplicate setup frames on reconnect race" bug (plan Bug 5).

2. **Held outbound queue**: While ``_in_reconnect`` is True and
   ``_handshake_complete`` is not set, ``send_event`` appends to
   ``_held_outbound`` rather than calling ``_ws.send``. After the new
   upstream's ``setupComplete``, the queue is flushed in order. Closes
   Bug 6 (frames flushed on dead WS).

3. **Setup idempotency**: A given ``_ws`` instance is recorded in
   ``_setup_sent_for_ws`` (a ``WeakSet``) after the first setup frame is
   shipped. A second ``_open_and_handshake`` on the SAME ``_ws`` is
   refused — even if reconnect logic ever races, the wire stays clean.

4. **Held-queue bounded**: The queue has a soft cap (drop-oldest) so a
   prolonged reconnect doesn't OOM the relay.

Plan §C — these complement the parity tests in
``tests/parity/test_reconnect_*.py`` which pin pre-refactor behavior.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any
from unittest.mock import patch

import pytest

from orchestrator.voice_relay import VoiceRelay
from orchestrator.voice_reconnect import (
    HELD_OUTBOUND_CAP,
    ReconnectReason,
)


# ---------- minimal fakes ---------------------------------------------------


class _FakeWS:
    def __init__(self, frames=None, close_error=None, clean_close=False):
        self._frames: deque[str] = deque(frames or [])
        self._close_error = close_error
        self._clean_close = clean_close
        self.sent: list[str] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Allow tests to push frames mid-stream.
        if self._frames:
            return self._frames.popleft()
        # Yield to the loop so we don't bus the cooperative scheduler.
        await asyncio.sleep(0.01)
        if self._frames:
            return self._frames.popleft()
        if self._close_error is not None:
            err = self._close_error
            self._close_error = None
            raise err
        if self._clean_close:
            raise StopAsyncIteration
        # Park until cancelled.
        await asyncio.sleep(60)
        raise StopAsyncIteration

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        await asyncio.sleep(0)
        raise asyncio.TimeoutError()

    async def send(self, payload):
        if self.closed:
            raise ConnectionError("send on closed WS")
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason


class _FakeProvider:
    provider_name = "test"
    connection_type = "websocket"
    model = "test-model"
    voice = "test"
    supports_manual_vad = False
    handshake_direction = "server_first"

    def __init__(self, ws_seq):
        self._ws_seq = list(ws_seq)
        self.opens = 0

    async def open_upstream(self):
        self.opens += 1
        return self._ws_seq.pop(0)

    async def inject_event(self, event):
        pass

    @classmethod
    def extract_audio_out(cls, event):
        return None

    def is_recoverable_error(self, exc):
        return True

    def should_gate_event(self, event):
        return False

    def on_inbound_event(self, event):
        pass

    def gate_cleared(self):
        return True

    def build_keepalive_chunk(self):
        return None

    def should_close_after_event(self, event):
        return False

    def classify_close_reason(self, exc, code, reason):
        return None


# ---------- 1. single-lock stress -------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_reconnects_coalesce_to_one_setup():
    """N concurrent ``_try_reconnect`` calls must coalesce: exactly ONE
    upstream open + one setup frame ships on the new WS.

    Notes on concurrency: we inject a real yield via
    ``await asyncio.sleep(0)`` inside ``rebuild_session_update`` so the
    50 tasks actually overlap. Without that, the cooperative scheduler
    runs each task to completion (FakeWS has no real I/O) and the test
    becomes a serial-call stress test instead.
    """
    # Initial WS (consumed by start), then a single rebuilt WS.
    initial = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    rebuilt = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _FakeProvider([initial, rebuilt])

    rebuilds = 0

    async def on_audio(_b64):
        pass

    async def on_event(_ev):
        pass

    async def rebuild():
        nonlocal rebuilds
        rebuilds += 1
        # Real yield: ensures the rebuild path yields long enough for
        # the other 49 callers to enter _try_reconnect concurrently.
        await asyncio.sleep(0)
        return {"type": "session.update", "session": {"instructions": "x"}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-concurrent",
        rebuild_session_update=rebuild,
        max_reconnects=10,
    )

    await relay.start({"type": "session.update", "session": {"instructions": "i"}})

    err = ConnectionError("InvalidParameter: synthetic")

    # Fire 50 concurrent _try_reconnect calls.
    results = await asyncio.gather(*[
        relay._try_reconnect(err, reason=ReconnectReason.RECOVERABLE_ERROR)
        for _ in range(50)
    ])

    await relay.stop()

    # Coalescing: exactly one open happened, one rebuild called.
    assert provider.opens == 2, (
        f"50 concurrent reconnects must coalesce; expected 2 opens "
        f"(initial + 1 reconnect), got {provider.opens}"
    )
    assert rebuilds == 1, (
        f"rebuild_session_update should be invoked exactly once; got {rebuilds}"
    )
    # All callers observe success (the coalesced waiters see handshake_complete).
    assert all(results), f"all reconnect callers should observe success; got {results}"
    # Exactly ONE setup frame on the new WS.
    setup_frames = [s for s in rebuilt.sent if "session.update" in s or "instructions" in s]
    assert len(setup_frames) == 1, (
        f"expected exactly one setup frame on rebuilt WS; got {len(setup_frames)}: {rebuilt.sent!r}"
    )


# ---------- 2. held outbound queue ------------------------------------------


@pytest.mark.asyncio
async def test_held_outbound_buffered_during_reconnect_and_flushed():
    """While ``_in_reconnect`` is True, outbound events go to
    ``_held_outbound``. After the new setupComplete, they flush in order.
    """
    initial = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    rebuilt = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _FakeProvider([initial, rebuilt])

    async def on_audio(_b64):
        pass

    async def on_event(_ev):
        pass

    async def rebuild():
        return {"type": "session.update", "session": {"instructions": "x"}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-hold",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    await relay.start({"type": "session.update", "session": {"instructions": "i"}})

    # Manually pause the reconnect midway: enter the reconnect window,
    # queue some frames, then complete the reconnect.
    # We use a sentinel rebuild to pause inside _try_reconnect.
    pause = asyncio.Event()
    resume = asyncio.Event()

    async def slow_rebuild():
        pause.set()
        await resume.wait()
        return {"type": "session.update", "session": {"instructions": "x"}}

    relay._rebuild_session_update = slow_rebuild

    # Kick off reconnect.
    reconnect_task = asyncio.create_task(
        relay._try_reconnect(
            ConnectionError("InvalidParameter"),
            reason=ReconnectReason.RECOVERABLE_ERROR,
        )
    )

    # Wait until we're inside the reconnect window (rebuild paused).
    await pause.wait()

    # Send three control events while paused — should be queued.
    await relay.send_event({"type": "response.create", "n": 1})
    await relay.send_event({"type": "response.create", "n": 2})
    await relay.send_event({"type": "response.create", "n": 3})

    assert len(relay._held_outbound) == 3, (
        f"three events should be buffered while _in_reconnect; got "
        f"{len(relay._held_outbound)}"
    )

    # Resume the reconnect — flush happens after handshake.
    resume.set()
    ok = await reconnect_task
    assert ok

    # Give the loop a tick to flush.
    for _ in range(5):
        await asyncio.sleep(0)
    await relay.stop()

    # Queue drained.
    assert relay._held_outbound == [], (
        f"queue must be empty after flush; got {relay._held_outbound}"
    )
    # Order preserved on the new WS.
    response_creates = [
        json.loads(s) for s in rebuilt.sent if '"response.create"' in s
    ]
    assert [r["n"] for r in response_creates] == [1, 2, 3], (
        f"flushed events must be in order; got {response_creates}"
    )


@pytest.mark.asyncio
async def test_held_outbound_drops_close_after_event_frames_on_flush():
    """Frames that would trigger ``should_close_after_event`` on the NEW
    upstream are skipped during flush — they referred to the dying
    connection's lifecycle.
    """
    initial = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    rebuilt = _FakeWS(frames=[json.dumps({"type": "session.created"})])

    class _CloseOnX(_FakeProvider):
        def should_close_after_event(self, event):
            # A frame with {"close_me": True} would tell us to close.
            return isinstance(event, dict) and event.get("close_me") is True

    provider = _CloseOnX([initial, rebuilt])

    async def on_audio(_b64):
        pass

    async def on_event(_ev):
        pass

    async def rebuild():
        return {"type": "session.update", "session": {}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-drop",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    await relay.start({"type": "session.update", "session": {}})

    pause = asyncio.Event()
    resume = asyncio.Event()

    async def slow_rebuild():
        pause.set()
        await resume.wait()
        return {"type": "session.update", "session": {}}

    relay._rebuild_session_update = slow_rebuild
    task = asyncio.create_task(
        relay._try_reconnect(
            ConnectionError("InvalidParameter"),
            reason=ReconnectReason.RECOVERABLE_ERROR,
        )
    )
    await pause.wait()

    await relay.send_event({"type": "response.create", "n": 1})
    await relay.send_event({"type": "control.bye", "close_me": True})  # skip
    await relay.send_event({"type": "response.create", "n": 2})

    resume.set()
    await task

    for _ in range(5):
        await asyncio.sleep(0)
    await relay.stop()

    # The close_me frame was filtered; the rest flushed in order.
    flushed_types = [
        json.loads(s).get("type") for s in rebuilt.sent
        if s.startswith("{")
    ]
    assert "control.bye" not in flushed_types, (
        f"close_after_event frames must be skipped on flush; got {flushed_types}"
    )
    assert flushed_types.count("response.create") == 2


@pytest.mark.asyncio
async def test_held_outbound_drops_oldest_at_cap():
    """The held queue is bounded — once ``HELD_OUTBOUND_CAP`` frames are
    queued, the oldest is dropped (best-effort: preserve the most
    recent intent rather than OOM).
    """
    initial = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    rebuilt = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _FakeProvider([initial, rebuilt])

    async def on_audio(_b64):
        pass

    async def on_event(_ev):
        pass

    async def rebuild():
        return {"type": "session.update", "session": {}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-cap",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    await relay.start({"type": "session.update", "session": {}})

    pause = asyncio.Event()
    resume = asyncio.Event()

    async def slow_rebuild():
        pause.set()
        await resume.wait()
        return {"type": "session.update", "session": {}}

    relay._rebuild_session_update = slow_rebuild
    task = asyncio.create_task(
        relay._try_reconnect(
            ConnectionError("InvalidParameter"),
            reason=ReconnectReason.RECOVERABLE_ERROR,
        )
    )
    await pause.wait()

    # Fill beyond cap.
    for i in range(HELD_OUTBOUND_CAP + 10):
        await relay.send_event({"type": "response.create", "n": i})

    assert len(relay._held_outbound) == HELD_OUTBOUND_CAP, (
        f"queue must be capped at {HELD_OUTBOUND_CAP}; got "
        f"{len(relay._held_outbound)}"
    )
    # The OLDEST entries got dropped; the LATEST cap entries survive.
    first_n = relay._held_outbound[0]["n"]
    last_n = relay._held_outbound[-1]["n"]
    assert first_n == 10
    assert last_n == HELD_OUTBOUND_CAP + 9

    resume.set()
    await task
    await relay.stop()


# ---------- 3. setup idempotency guard --------------------------------------


@pytest.mark.asyncio
async def test_double_setup_on_same_ws_is_refused():
    """If anything ever tries to ship two setup frames on the SAME
    ``_ws`` instance, the second call must raise — wire stays clean.
    """
    ws = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _FakeProvider([ws, ws])  # SAME ws on second call

    async def on_audio(_b64):
        pass

    async def on_event(_ev):
        pass

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-idem",
        rebuild_session_update=None,
        max_reconnects=2,
    )

    cfg = {"type": "session.update", "session": {}}
    await relay.start(cfg)
    # Now call _open_and_handshake again on the SAME _ws — should refuse.
    with pytest.raises(RuntimeError, match="setup_already_sent"):
        # _open_and_handshake reopens via provider.open_upstream which
        # returns the same ws fake here.
        await relay._open_and_handshake(cfg)

    await relay.stop()


# ---------- 4. ReconnectReason → policy table -------------------------------


def test_policy_table_invariants():
    """Sanity-check the policy table itself — pin the contract by reading
    it. If a future patch silently flips a flag (e.g. starts capping
    goAway), this catches it at unit-test time.
    """
    from orchestrator.voice_reconnect import POLICIES

    goaway = POLICIES[ReconnectReason.PROVIDER_GOAWAY]
    assert goaway.max_attempts == 0, "goAway must remain uncapped"
    assert goaway.reset_handle is False
    assert goaway.surface_to_user is True

    recov = POLICIES[ReconnectReason.RECOVERABLE_ERROR]
    assert recov.max_attempts == 2, "recoverable-error cap unchanged from HEAD"
    assert recov.reset_handle is False

    stale = POLICIES[ReconnectReason.STALE_HANDLE]
    assert stale.max_attempts == 1, "stale-handle is one-shot"
    assert stale.reset_handle is True
    assert stale.surface_to_user is False, "stale-handle is silent recovery"
