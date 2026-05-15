"""Tests for the per-provider hook surface on ``VoiceRelay``.

After the deliverable-3 refactor, every Qwen-specific code path in the
relay lives behind a hook on :class:`BaseVoiceProvider`:

- ``is_recoverable_error`` — decides whether a drain failure triggers a
  transparent reconnect (Qwen's "InvalidParameter" boilerplate is
  recoverable; arbitrary network errors are not).
- ``should_gate_event`` / ``on_inbound_event`` / ``gate_cleared`` —
  defer outbound frames the provider currently can't accept (Qwen's
  ``response.create`` while a response is in flight).
- ``build_keepalive_chunk`` — opt-in silent-PCM keepalive (Qwen needs
  it; the default ``None`` skips the keepalive task entirely).
- ``format_audio_in`` / ``extract_audio_out`` — required-for-websocket
  audio shape; the relay calls them directly instead of duck-typing.

These tests drive a fake provider that toggles each hook so the relay
is verified provider-agnostic.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import pytest

from orchestrator.voice_relay import VoiceRelay


class _FakeWS:
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
            self._close_error = None
            raise err
        # Idle indefinitely after the queued frames are drained.
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        raise asyncio.TimeoutError()

    async def send(self, payload: str):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class _HookProvider:
    """Provider double whose hooks are programmable per test."""

    provider_name = "hook-test"
    connection_type = "websocket"
    model = "hook-test-model"
    voice = "x"

    def __init__(
        self,
        ws_seq: list[_FakeWS],
        *,
        recoverable: bool = False,
        gate_first_n: int = 0,
        keepalive_chunk: str | None = None,
    ):
        self._ws_seq = list(ws_seq)
        self.opens = 0
        self.injected: list[dict[str, Any]] = []
        self.on_inbound_calls: list[dict[str, Any]] = []
        self._recoverable = recoverable
        # Number of times should_gate_event will return True for
        # ``response.create`` before letting it through.
        self._gate_remaining = gate_first_n
        self._keepalive_chunk = keepalive_chunk
        self._response_active = False

    async def open_upstream(self):
        self.opens += 1
        return self._ws_seq.pop(0)

    async def inject_event(self, event):
        self.injected.append(event)

    def format_audio_in(self, pcm_b64):
        return {"type": "_audio_in", "data": pcm_b64}

    @classmethod
    def extract_audio_out(cls, event):
        if event.get("type") == "audio_out":
            return event.get("data")
        return None

    def is_recoverable_error(self, exc):
        return self._recoverable

    def should_gate_event(self, event):
        if event.get("type") == "response.create" and self._gate_remaining > 0:
            self._gate_remaining -= 1
            self._response_active = True
            return True
        return False

    def on_inbound_event(self, event):
        self.on_inbound_calls.append(event)
        if event.get("type") == "response.done":
            self._response_active = False

    def gate_cleared(self):
        return not self._response_active

    def build_keepalive_chunk(self):
        return self._keepalive_chunk


@pytest.mark.asyncio
async def test_keepalive_skipped_when_provider_returns_none():
    """If build_keepalive_chunk() returns None, no keepalive task is spawned."""
    ws = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _HookProvider([ws], keepalive_chunk=None)

    async def on_audio(_):
        pass

    async def on_event(_):
        pass

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-no-keepalive",
    )
    await relay.start({"type": "session.update", "session": {}})
    try:
        assert relay._keepalive_task is None, \
            "Relay must not spawn a keepalive task when the provider returns None"
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_keepalive_spawned_when_provider_opts_in():
    """A non-None keepalive chunk causes the relay to spawn the keepalive task."""
    ws = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _HookProvider([ws], keepalive_chunk="AAAA")

    async def on_audio(_):
        pass

    async def on_event(_):
        pass

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-keepalive",
    )
    await relay.start({"type": "session.update", "session": {}})
    try:
        assert relay._keepalive_task is not None, \
            "Relay must spawn the keepalive task when the provider returns a chunk"
        assert not relay._keepalive_task.done()
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_recoverable_hook_drives_reconnect_decision():
    """A provider returning True from is_recoverable_error triggers a reopen."""
    initial = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError("anything — provider decides"),
    )
    reopened = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _HookProvider([initial, reopened], recoverable=True)

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(_):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        return {"type": "session.update", "session": {}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-recoverable",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    await relay.start({"type": "session.update", "session": {}})
    for _ in range(5):
        await asyncio.sleep(0)
    await relay.stop()

    assert provider.opens == 2, "provider should have been reopened once"
    error_events = [e for e in frontend_events if e.get("type") == "error"]
    assert error_events == [], "frontend must not see an error after a successful reconnect"


@pytest.mark.asyncio
async def test_non_recoverable_hook_surfaces_error_to_frontend():
    """A provider returning False from is_recoverable_error skips reconnect."""
    ws = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError("InvalidParameter (provider says no)"),
    )
    provider = _HookProvider([ws], recoverable=False)

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(_):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        return {"type": "session.update", "session": {}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-fatal",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    await relay.start({"type": "session.update", "session": {}})
    for _ in range(5):
        await asyncio.sleep(0)
    await relay.stop()

    assert provider.opens == 1, "provider must not reopen when it classifies the error as fatal"
    error_events = [e for e in frontend_events if e.get("type") == "error"]
    assert len(error_events) == 1


@pytest.mark.asyncio
async def test_should_gate_event_defers_outbound_until_cleared():
    """Gated events park in the deferred queue and replay after gate_cleared()."""
    # Inbound: session.created, then a response.done that clears the gate.
    ws = _FakeWS(
        frames=[
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "response.done"}),
        ],
    )
    provider = _HookProvider([ws], gate_first_n=1)  # First response.create gets gated.

    async def on_audio(_):
        pass

    async def on_event(_):
        pass

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-gate",
    )

    await relay.start({"type": "session.update", "session": {}})
    # First send — gated.
    await relay.send_event({"type": "response.create"})
    # Note: we cannot assert "not in ws.sent" yet because the queued
    # response.done from the WS frames is not guaranteed to be drained
    # before the assertion runs.  Yield to the loop to let drain run.
    for _ in range(10):
        await asyncio.sleep(0)
    await relay.stop()

    response_create_frames = [s for s in ws.sent if "response.create" in s]
    assert len(response_create_frames) == 1, \
        f"After gate clears, the deferred response.create must replay exactly once. Sent: {ws.sent}"


@pytest.mark.asyncio
async def test_on_inbound_event_called_for_every_inbound():
    """The provider sees every inbound event in order (not just unrecognised ones)."""
    ws = _FakeWS(
        frames=[
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "response.created"}),
            json.dumps({"type": "audio_out", "data": "AAAA"}),
            json.dumps({"type": "response.done"}),
        ],
    )
    provider = _HookProvider([ws])

    audio_received: list[str] = []

    async def on_audio(b64):
        audio_received.append(b64)

    async def on_event(_):
        pass

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-on-inbound",
    )

    await relay.start({"type": "session.update", "session": {}})
    for _ in range(10):
        await asyncio.sleep(0)
    await relay.stop()

    seen_types = [e.get("type") for e in provider.on_inbound_calls]
    # session.created is drained pre-loop and also fed to inject_event +
    # the frontend, but the on_inbound_event hook runs INSIDE the drain
    # loop only — so it should see response.created, audio_out, response.done.
    assert "response.created" in seen_types
    assert "audio_out" in seen_types
    assert "response.done" in seen_types
    # Audio chunk was extracted and forwarded.
    assert audio_received == ["AAAA"]


@pytest.mark.asyncio
async def test_format_audio_in_called_for_each_chunk():
    """send_audio funnels through provider.format_audio_in (no duck-typing)."""
    ws = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _HookProvider([ws])

    async def on_audio(_):
        pass

    async def on_event(_):
        pass

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-audio-in",
    )

    await relay.start({"type": "session.update", "session": {}})
    await relay.send_audio("AAAA")
    await relay.send_audio("BBBB")
    await relay.stop()

    audio_frames = [json.loads(s) for s in ws.sent if "_audio_in" in s]
    assert len(audio_frames) == 2
    assert audio_frames[0] == {"type": "_audio_in", "data": "AAAA"}
    assert audio_frames[1] == {"type": "_audio_in", "data": "BBBB"}
