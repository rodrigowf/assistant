"""Parity contract: goAway reconnect preserves four tuned behaviors.

The 2026-06-04 voice-lifecycle-refactor stabilised the goAway path. The
Increment C refactor (parameterised reconnect + single lock + held
outbound queue) MUST NOT regress these four properties, each of which
was empirically painful to discover and fix:

1. ``goAway`` triggers a fresh upstream open with the latest
   ``sessionResumption`` handle preserved (provider state stays alive
   across the seam — long calls hinge on this).
2. The drain task chains into a NEW task on the new ``_ws`` — exactly
   ONE setup frame is sent to the new WS (the 2026-06-04 01:11 incident
   showed two setup frames causing the second one to be rejected).
3. ``manual_vad.reset()`` is called on a successful reconnect so
   Silero's recurrent state doesn't carry frozen pre-cutover hidden
   state into the new session.
4. ``_manual_vad_speech_started_at`` is cleared so the post-reconnect
   safety-commit watchdog doesn't see an N-seconds-ago timestamp and
   fire an immediate commit on the fresh upstream.

A regression in any of these is a UX bug at best and a stuck-mic at
worst. Per plan §0.3 these are pinned BEFORE the refactor and must keep
passing AFTER it.

See plan §C ("Parameterised reconnect with single lock + held outbound
queue") and §11 (test plan).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.voice_relay import VoiceRelay


class _GoAwayFakeWS:
    """In-memory WS double. Yields frames then either closes cleanly
    (``raise StopAsyncIteration``) or raises the configured error.
    """

    def __init__(
        self,
        frames: list[str],
        clean_close: bool = False,
        close_error: BaseException | None = None,
    ):
        self._frames: deque[str] = deque(frames)
        self._clean_close = clean_close
        self._close_error = close_error
        self.sent: list[str] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.popleft()
        if self._close_error is not None:
            err = self._close_error
            self._close_error = None
            raise err
        if self._clean_close:
            raise StopAsyncIteration
        raise StopAsyncIteration

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        await asyncio.sleep(0)
        raise asyncio.TimeoutError()

    async def send(self, payload: str):
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason


class _GoAwayFakeProvider:
    """Provider double that requests reconnect on a ``goAway`` frame —
    mirrors Gemini Live's ``should_close_after_event`` contract. Tracks
    every session_config it receives so tests can count setup frames.
    """

    provider_name = "google"
    connection_type = "websocket"
    model = "gemini-live-test"
    voice = "Puck"
    supports_manual_vad = True
    audio_in_sample_rate = 16000
    handshake_direction = "client_first"

    def __init__(self, ws_seq: list[_GoAwayFakeWS]):
        self._ws_seq = list(ws_seq)
        self.opens = 0
        self.injected: list[dict[str, Any]] = []
        # Updated when the relay observes ``sessionResumptionUpdate``;
        # production Gemini provider does the same in ``on_inbound_event``.
        self._resumption_handle: str | None = None

    async def open_upstream(self):
        self.opens += 1
        return self._ws_seq.pop(0)

    async def inject_event(self, event: dict[str, Any]) -> None:
        self.injected.append(event)
        # Mimic production: capture the latest handle.
        upd = event.get("sessionResumptionUpdate")
        if isinstance(upd, dict):
            h = upd.get("newHandle")
            if isinstance(h, str):
                self._resumption_handle = h

    @classmethod
    def extract_audio_out(cls, event):
        return None

    def is_recoverable_error(self, exc: BaseException) -> bool:
        # goAway flows through ``_pending_reconnect`` path, NOT through
        # this gate. Return False to confirm goAway is the only trigger.
        return False

    def should_gate_event(self, event):
        return False

    def on_inbound_event(self, event):
        upd = event.get("sessionResumptionUpdate")
        if isinstance(upd, dict):
            h = upd.get("newHandle")
            if isinstance(h, str):
                self._resumption_handle = h

    def gate_cleared(self):
        return True

    def build_keepalive_chunk(self):
        return None

    def should_close_after_event(self, event):
        return isinstance(event, dict) and "goAway" in event

    def classify_close_reason(self, exc, code, reason):
        return None


@pytest.mark.asyncio
async def test_goaway_preserves_latest_resumption_handle_and_single_setup(monkeypatch):
    """A ``goAway`` frame must:

    - close the current WS cleanly (1000),
    - open a NEW upstream WS,
    - send exactly ONE setup frame on the new WS (no duplicate),
    - call ``manual_vad.reset()`` on the local VAD,
    - clear ``_manual_vad_speech_started_at``.
    """
    initial = _GoAwayFakeWS(
        frames=[
            # client_first handshake completes on setupComplete.
            json.dumps({"setupComplete": {}}),
            # Provider sends a resumption handle update mid-session.
            json.dumps({"sessionResumptionUpdate": {"newHandle": "HANDLE-FRESH"}}),
            # ...then a goAway with a short warning.
            json.dumps({"goAway": {"timeLeft": "30s"}}),
        ],
        clean_close=True,  # our 1000 close after goAway exits cleanly
    )
    reopened = _GoAwayFakeWS(frames=[json.dumps({"setupComplete": {}})])
    provider = _GoAwayFakeProvider([initial, reopened])

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(_b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    rebuilt: list[dict[str, Any]] = []

    async def rebuild():
        cfg = {
            "setup": {
                "sessionResumption": (
                    {"handle": provider._resumption_handle}
                    if provider._resumption_handle else {}
                ),
                "model": "gemini-live-test",
            }
        }
        rebuilt.append(cfg)
        return cfg

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-goaway",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )

    # Install a fake manual VAD by patching the constructor so the
    # ``start()`` path picks it up instead of trying to load Silero.
    fake_vad = MagicMock()
    fake_vad.reset = MagicMock()
    with patch(
        "orchestrator.voice_relay.voice_vad.VoiceVAD",
        return_value=fake_vad,
    ):
        await relay.start({"setup": {"model": "gemini-live-test"}})
    relay._manual_vad_speech_started_at = 1234.5  # simulate active speech

    for _ in range(80):
        await asyncio.sleep(0)

    # Snapshot state BEFORE stop(), in case stop() perturbs vad/timestamps.
    reset_called_pre_stop = fake_vad.reset.called
    speech_started_at_pre_stop = relay._manual_vad_speech_started_at

    await relay.stop()

    # 1. NEW upstream opened.
    assert provider.opens == 2, f"expected 2 opens, got {provider.opens}"

    # 2. rebuild captured the LATEST handle (not stale, not absent).
    assert len(rebuilt) == 1
    assert rebuilt[0]["setup"]["sessionResumption"]["handle"] == "HANDLE-FRESH", (
        "rebuild_session_update must read the latest handle the provider "
        "captured before goAway"
    )

    # 3. Exactly ONE setup frame on the NEW WS (Bug 5 guard).
    setup_frames_on_new = [
        s for s in reopened.sent
        if "setup" in s and "model" in s
    ]
    assert len(setup_frames_on_new) == 1, (
        f"expected exactly one setup frame on the reopened WS; got "
        f"{len(setup_frames_on_new)}: {reopened.sent!r}"
    )

    # 4. manual_vad.reset() called.
    assert reset_called_pre_stop, (
        "manual_vad.reset() must be called on successful reconnect so "
        "Silero's recurrent state doesn't carry across the seam"
    )

    # 5. Safety-commit watchdog timestamp cleared.
    assert speech_started_at_pre_stop is None, (
        "_manual_vad_speech_started_at must be cleared post-reconnect to "
        "prevent the safety-commit watchdog firing immediately"
    )

    # 6. Frontend got the reconnect_warning + reconnecting status frames.
    statuses = [
        e for e in frontend_events
        if e.get("type") == "voice_status"
    ]
    status_kinds = [e.get("status") for e in statuses]
    assert "reconnect_warning" in status_kinds
    assert "reconnecting" in status_kinds


@pytest.mark.asyncio
async def test_goaway_is_uncapped_by_max_reconnects():
    """goAway reconnects are protocol-driven and MUST NOT count against
    ``max_reconnects``. Multiple goAways in sequence should each succeed
    even when ``max_reconnects=1`` (the recoverable-error path's cap).
    """
    seq = []
    for i in range(3):
        seq.append(
            _GoAwayFakeWS(
                frames=[
                    json.dumps({"setupComplete": {}}),
                    json.dumps({"goAway": {"timeLeft": "30s"}}),
                ],
                clean_close=True,
            )
        )
    # Final WS just idles after setup.
    seq.append(_GoAwayFakeWS(frames=[json.dumps({"setupComplete": {}})]))

    provider = _GoAwayFakeProvider(seq)

    async def on_audio(_b64):
        pass

    async def on_event(_ev):
        pass

    async def rebuild():
        return {"setup": {"model": "gemini-live-test"}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-uncapped",
        rebuild_session_update=rebuild,
        max_reconnects=1,  # would cap recoverable errors
    )

    await relay.start({"setup": {"model": "gemini-live-test"}})

    for _ in range(60):
        await asyncio.sleep(0)

    await relay.stop()

    # All four WSes consumed — three goAway reconnects despite max_reconnects=1.
    assert provider.opens == 4, (
        f"goAway must be uncapped; expected 4 opens, got {provider.opens}"
    )
    # The error-path counter is NOT bumped by goAway reconnects.
    assert relay._reconnect_count == 0
