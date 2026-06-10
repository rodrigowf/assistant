"""Parity contract: recoverable-error reconnect honours ``max_reconnects``.

This pins the OTHER reconnect class (the one driven by
``is_recoverable_error``): transient transport closes like DashScope's
1007 "InvalidParameter" boilerplate. The Increment C refactor reshapes
this path (parameterised reason, single lock, held queue) but the cap
must still bound retries — otherwise a permanently broken upstream
becomes a runaway loop.

See plan §C and §11.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import pytest

from orchestrator.voice_relay import VoiceRelay


class _RecovFakeWS:
    def __init__(self, frames, close_error=None):
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
            self._close_error = None
            raise err
        raise StopAsyncIteration

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        await asyncio.sleep(0)
        raise asyncio.TimeoutError()

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True


class _RecovFakeProvider:
    provider_name = "qwen"
    connection_type = "websocket"
    model = "qwen-realtime"
    voice = "Cherry"
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
        return "InvalidParameter" in str(exc)

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


@pytest.mark.asyncio
async def test_recoverable_error_capped_at_max_reconnects():
    """``max_reconnects=2`` must bound consecutive recoverable-error
    reconnects at 2 (i.e., 3 total opens). Critical: the refactor
    parameterises the reason but must keep this cap intact for
    non-goAway flows.
    """
    sigil = ConnectionError(
        "received 1007 InvalidParameter: The provided URL does not appear to be valid"
    )
    ws_a = _RecovFakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=sigil,
    )
    ws_b = _RecovFakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=sigil,
    )
    ws_c = _RecovFakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=sigil,
    )
    ws_d = _RecovFakeWS(frames=[])  # must not open
    provider = _RecovFakeProvider([ws_a, ws_b, ws_c, ws_d])

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(_b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        return {"type": "session.update", "session": {"instructions": "x"}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-cap",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )
    await relay.start({"type": "session.update", "session": {"instructions": "i"}})

    for _ in range(40):
        await asyncio.sleep(0)
    await relay.stop()

    # Initial open + 2 reconnects = 3 opens. ws_d never touched.
    assert provider.opens == 3, (
        f"recoverable-error reconnects must be capped at max_reconnects=2 "
        f"(3 opens total); got {provider.opens}"
    )
    # After the cap is hit, the legacy error frame surfaces.
    error_events = [
        e for e in frontend_events
        if e.get("type") == "error" and (e.get("error") or {}).get("code") == "voice_relay_failed"
    ]
    assert len(error_events) == 1
