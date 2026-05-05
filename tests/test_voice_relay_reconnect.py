"""Tests for VoiceRelay's transparent reconnect on recoverable upstream
failures.

Background: DashScope (Qwen-Omni realtime) periodically closes the
WebSocket with WebSocket close code 1007 + a misleading
``InvalidParameter: The provided URL does not appear to be valid`` 400.
Empirically this fires mid-session with no offending frame on our side
— the validator is reused for unrelated internal pipeline failures.
Reopening with a fresh ``session.update`` consistently brings the
session back, so the relay does that automatically.

These tests stub the upstream WS so we can drive the drain loop into
the recoverable error path and assert it reconnects vs. propagates to
the frontend.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.voice_relay import VoiceRelay


class _FakeWS:
    """In-memory WS double — feed inbound frames via push(), the relay's
    ``async for`` loop yields them and then raises the configured error.
    """

    def __init__(self, frames: list[str], close_error: BaseException | None = None):
        self._frames: deque[str] = deque(frames)
        self._close_error = close_error
        self.sent: list[str] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.popleft()
        if self._close_error is not None:
            err = self._close_error
            # One-shot — don't keep raising.
            self._close_error = None
            raise err
        raise StopAsyncIteration

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        await asyncio.sleep(0)  # Mimic awaitable.
        raise asyncio.TimeoutError()

    async def send(self, payload: str):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class _FakeProvider:
    """Minimal voice provider double for the relay."""

    provider_name = "qwen-test"
    connection_type = "websocket"
    model = "qwen-test-model"
    voice = "test-voice"

    def __init__(self, ws_seq: list[_FakeWS]):
        self._ws_seq = list(ws_seq)
        self.opens = 0
        self.injected: list[dict[str, Any]] = []

    async def open_upstream(self):
        self.opens += 1
        return self._ws_seq.pop(0)

    async def inject_event(self, event: dict[str, Any]) -> None:
        self.injected.append(event)

    @staticmethod
    def extract_audio_out(event):
        return None

    # Avoid keepalive task spawn — that loop polls _last_audio_in_at and
    # would race the test's event loop close.
    # (No build_keepalive_chunk attribute => relay skips the keepalive.)


@pytest.mark.asyncio
async def test_reconnect_on_invalid_parameter_close():
    """A 1007 close with the InvalidParameter signature should trigger
    a transparent reopen — the frontend never sees an error."""
    initial = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError(
            "received 1007 (invalid frame payload data) <400> "
            "InternalError.Algo.InvalidParameter: The provided URL does not "
            "appear to be valid."
        ),
    )
    reconnected = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _FakeProvider([initial, reconnected])

    audio_calls: list[str] = []
    frontend_events: list[dict[str, Any]] = []
    rebuilt = []

    async def on_audio(b64):
        audio_calls.append(b64)

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        rebuilt.append(True)
        return {"type": "session.update", "session": {"instructions": "rebuilt"}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-reconnect",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    initial_config = {"type": "session.update", "session": {"instructions": "initial"}}
    await relay.start(initial_config)

    # Let the drain task hit the close error and run the reconnect path.
    # The rebuilt WS has no close_error so the new drain just idles on
    # StopAsyncIteration after the session.created.  Give the loop a
    # couple of cycles to settle.
    for _ in range(5):
        await asyncio.sleep(0)

    await relay.stop()

    assert provider.opens == 2, "provider should have been reopened once"
    assert len(rebuilt) == 1, "rebuild_session_update should be called once"
    assert relay._reconnect_count == 1
    # Frontend should not see any voice_relay_failed error.
    error_events = [e for e in frontend_events if e.get("type") == "error"]
    assert error_events == [], f"unexpected error frames: {error_events}"
    # The new session.update was sent on the reconnected WS.
    assert any('"rebuilt"' in s for s in reconnected.sent), \
        f"reconnected WS should receive the rebuilt session.update; got {reconnected.sent}"


@pytest.mark.asyncio
async def test_no_reconnect_for_unknown_error():
    """A close that doesn't match the recoverable signature should NOT
    trigger reconnect — the frontend gets the error frame as before."""
    initial = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError("some unrelated network error"),
    )
    provider = _FakeProvider([initial])

    frontend_events: list[dict[str, Any]] = []
    rebuilt = []

    async def on_audio(b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        rebuilt.append(True)
        return {"type": "session.update"}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-no-reconnect",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    await relay.start({"type": "session.update", "session": {"instructions": "initial"}})

    for _ in range(5):
        await asyncio.sleep(0)

    await relay.stop()

    assert provider.opens == 1, "should not reopen for unrelated errors"
    assert rebuilt == [], "rebuild should not be called for unrelated errors"
    # The frontend should see a voice_relay_failed error.
    error_events = [e for e in frontend_events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "voice_relay_failed"


@pytest.mark.asyncio
async def test_reconnect_respects_max_reconnects():
    """After exhausting max_reconnects, further failures should propagate
    to the frontend instead of looping forever."""
    sigil = ConnectionError(
        "received 1007 (invalid frame payload data) "
        "InvalidParameter: The provided URL does not appear to be valid."
    )
    # Three WSes, all fail with the recoverable error.  max_reconnects=1
    # means: initial open + 1 reconnect = 2 total opens; the second
    # failure must propagate.
    ws_a = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=sigil,
    )
    ws_b = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=sigil,
    )
    ws_c = _FakeWS(frames=[])  # Should not be opened.
    provider = _FakeProvider([ws_a, ws_b, ws_c])

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        return {"type": "session.update", "session": {"instructions": "x"}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-max",
        rebuild_session_update=rebuild,
        max_reconnects=1,
    )

    await relay.start({"type": "session.update", "session": {"instructions": "initial"}})

    for _ in range(20):
        await asyncio.sleep(0)

    await relay.stop()

    assert provider.opens == 2, "exactly one reconnect"
    error_events = [e for e in frontend_events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "voice_relay_failed"


@pytest.mark.asyncio
async def test_no_reconnect_without_rebuild_callback():
    """Without a rebuild callback, the relay falls back to the legacy
    behaviour: surface the error to the frontend immediately."""
    ws = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError(
            "InvalidParameter: The provided URL does not appear to be valid"
        ),
    )
    provider = _FakeProvider([ws])

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-no-cb",
        rebuild_session_update=None,
    )
    await relay.start({"type": "session.update", "session": {"instructions": "initial"}})
    for _ in range(5):
        await asyncio.sleep(0)
    await relay.stop()

    assert provider.opens == 1
    error_events = [e for e in frontend_events if e.get("type") == "error"]
    assert len(error_events) == 1
