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
    """Minimal voice provider double for the relay.

    Provides only the relay-facing surface — does NOT subclass
    :class:`BaseVoiceProvider` so the tests can hand-roll edge cases
    without satisfying every abstract method.  The relay only calls
    the names we expose here, plus the hook methods that the post-refactor
    relay drives.
    """

    provider_name = "qwen-test"
    connection_type = "websocket"
    model = "qwen-test-model"
    voice = "test-voice"

    # Treat the same DashScope boilerplate as recoverable — these tests
    # exercise that path specifically.
    _RECONNECTABLE_ERR_SUBSTRINGS = (
        "InvalidParameter",
        "The provided URL does not appear to be valid",
        "response_idle_timeout",
    )

    def __init__(self, ws_seq: list[_FakeWS]):
        self._ws_seq = list(ws_seq)
        self.opens = 0
        self.injected: list[dict[str, Any]] = []

    async def open_upstream(self):
        self.opens += 1
        return self._ws_seq.pop(0)

    async def inject_event(self, event: dict[str, Any]) -> None:
        self.injected.append(event)

    @classmethod
    def extract_audio_out(cls, event):
        return None

    # --- relay hooks (matches BaseVoiceProvider surface) -----------------

    def is_recoverable_error(self, exc: BaseException) -> bool:
        text = str(exc)
        return any(s in text for s in self._RECONNECTABLE_ERR_SUBSTRINGS)

    def should_gate_event(self, event):
        return False

    def on_inbound_event(self, event):
        pass

    def gate_cleared(self):
        return True

    def build_keepalive_chunk(self):
        # Returning None skips the keepalive task — that loop polls
        # _last_audio_in_at and would race the test's event loop close.
        return None


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


# === Increment A — typed VoiceError surfacing ============================
#
# The relay emits a ``voice_error`` event (additive) ahead of the legacy
# ``voice_relay_failed`` ``error`` event. When the classifier flags
# ``recoverable=False`` (quota / auth / model_unavailable / context_full),
# the relay short-circuits any further reconnect attempts.

class _FakeProviderWithClassifier(_FakeProvider):
    """Hand-rolled fake provider that also implements
    ``classify_close_reason`` so we can test the new emission path
    without spinning up a real Google/Qwen/OpenAI provider.
    """

    def __init__(self, ws_seq, classifier_response=None):
        super().__init__(ws_seq)
        self._classifier_response = classifier_response

    def classify_close_reason(self, exc, code, reason):
        return self._classifier_response


@pytest.mark.asyncio
async def test_voice_error_event_emitted_alongside_legacy_error():
    """When a non-recoverable error closes the WS, the relay must emit
    BOTH the new ``voice_error`` event AND the legacy
    ``voice_relay_failed`` ``error`` event (back-compat with clients
    that haven't been updated yet).
    """
    from orchestrator.voice_errors import VoiceError, VoiceErrorCategory

    classified = VoiceError(
        category=VoiceErrorCategory.QUOTA_EXCEEDED,
        message="Your project has exceeded its monthly spending cap.",
        recoverable=False,
        recovery_hint="Top up at ai.studio/spend",
        provider_doc_url="https://ai.studio/spend",
        raw_close_code=1011,
        raw_close_reason="exceeded its monthly spending cap",
        provider="google",
    )

    ws = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError(
            "received 1011 (internal error) Your project has exceeded its "
            "monthly spending cap"
        ),
    )
    provider = _FakeProviderWithClassifier([ws], classifier_response=classified)

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-quota",
        rebuild_session_update=None,
    )
    await relay.start({"type": "session.update", "session": {"instructions": "initial"}})

    for _ in range(10):
        await asyncio.sleep(0)
    await relay.stop()

    voice_errors = [e for e in frontend_events if e.get("type") == "voice_error"]
    legacy_errors = [
        e for e in frontend_events
        if e.get("type") == "error"
        and (e.get("error") or {}).get("code") == "voice_relay_failed"
    ]
    assert len(voice_errors) == 1, (
        f"expected one voice_error event; got {voice_errors!r} "
        f"(full event list: {frontend_events!r})"
    )
    payload = voice_errors[0]["error"]
    assert payload["category"] == "quota_exceeded"
    assert payload["recoverable"] is False
    assert payload["provider"] == "google"
    assert "spending cap" in payload["message"]
    # Legacy event still emitted — back-compat.
    assert len(legacy_errors) == 1
    # voice_error must come BEFORE the legacy error so clients that
    # listen to both see the typed envelope first and can decide whether
    # to suppress the generic banner.
    voice_error_idx = frontend_events.index(voice_errors[0])
    legacy_idx = frontend_events.index(legacy_errors[0])
    assert voice_error_idx < legacy_idx


@pytest.mark.asyncio
async def test_non_recoverable_voice_error_short_circuits_reconnect():
    """When the classifier flags the close as ``recoverable=False``, the
    relay must NOT consume reconnect attempts even if
    ``rebuild_session_update`` is configured.

    Authorised change per plan §10.2 (2026-06-09 user decision).
    """
    from orchestrator.voice_errors import VoiceError, VoiceErrorCategory

    classified = VoiceError(
        category=VoiceErrorCategory.QUOTA_EXCEEDED,
        message="Quota cap reached.",
        recoverable=False,
        recovery_hint=None,
        provider_doc_url=None,
        raw_close_code=1011,
        raw_close_reason="quota cap",
        provider="google",
    )

    ws = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError("received 1011 (internal error) quota cap"),
    )
    provider = _FakeProviderWithClassifier([ws], classifier_response=classified)

    frontend_events: list[dict[str, Any]] = []
    rebuilt: list[bool] = []

    async def on_audio(b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        rebuilt.append(True)
        return {"type": "session.update", "session": {}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-shortcircuit",
        rebuild_session_update=rebuild,
        max_reconnects=5,
    )
    await relay.start({"type": "session.update", "session": {"instructions": "initial"}})

    for _ in range(20):
        await asyncio.sleep(0)
    await relay.stop()

    # Despite max_reconnects=5 + a rebuild callback, the
    # recoverable=False classifier must keep the relay from retrying.
    assert provider.opens == 1, (
        f"expected exactly one open (no reconnects); got {provider.opens}. "
        "Classifier said recoverable=False — relay must short-circuit."
    )
    assert rebuilt == [], (
        "rebuild_session_update must not be called when the classifier "
        "flagged the error as non-recoverable."
    )


@pytest.mark.asyncio
async def test_recoverable_voice_error_does_not_short_circuit():
    """When the classifier flags recoverable=True, the existing
    reconnect path remains intact — the new code is fully back-compat.
    """
    from orchestrator.voice_errors import VoiceError, VoiceErrorCategory

    classified = VoiceError(
        category=VoiceErrorCategory.NETWORK,
        message="Transient transport close.",
        recoverable=True,
        recovery_hint=None,
        provider_doc_url=None,
        raw_close_code=1006,
        raw_close_reason=None,
        provider="qwen",
    )

    initial = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError(
            "received 1007 (invalid frame payload data) "
            "InvalidParameter: The provided URL does not appear to be valid"
        ),
    )
    reconnected = _FakeWS(frames=[json.dumps({"type": "session.created"})])
    provider = _FakeProviderWithClassifier(
        [initial, reconnected],
        classifier_response=classified,
    )

    frontend_events: list[dict[str, Any]] = []

    async def on_audio(b64):
        pass

    async def on_event(ev):
        frontend_events.append(ev)

    async def rebuild():
        return {"type": "session.update", "session": {"instructions": "rebuilt"}}

    relay = VoiceRelay(
        provider,
        on_audio_out=on_audio,
        on_event_for_frontend=on_event,
        session_id="t-recoverable",
        rebuild_session_update=rebuild,
        max_reconnects=2,
    )
    await relay.start({"type": "session.update", "session": {"instructions": "initial"}})

    for _ in range(10):
        await asyncio.sleep(0)
    await relay.stop()

    assert provider.opens == 2, "recoverable classifier preserves reconnect path"


@pytest.mark.asyncio
async def test_no_classifier_falls_back_to_generic_voice_error():
    """If the provider's classifier returns None, the relay synthesises a
    generic NETWORK ``voice_error`` envelope (recoverable=True). The
    relay's reconnect gate is then driven by ``is_recoverable_error``
    exactly as before.
    """
    ws = _FakeWS(
        frames=[json.dumps({"type": "session.created"})],
        close_error=ConnectionError("some unrelated network error"),
    )
    # The default _FakeProvider has no classify_close_reason method.
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
        session_id="t-fallback",
        rebuild_session_update=None,
    )
    await relay.start({"type": "session.update", "session": {"instructions": "initial"}})

    for _ in range(5):
        await asyncio.sleep(0)
    await relay.stop()

    voice_errors = [e for e in frontend_events if e.get("type") == "voice_error"]
    assert len(voice_errors) == 1
    payload = voice_errors[0]["error"]
    # No classifier output → relay uses the generic NETWORK fallback.
    assert payload["category"] == "network"
    assert payload["recoverable"] is True
    assert payload["provider"] == "qwen-test"
