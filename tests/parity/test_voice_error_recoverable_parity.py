"""Parity contract: classifier and `is_recoverable_error` must agree.

This file enforces the design decision recorded in the Increment A
implementation session (2026-06-09):

  ``classify_close_reason`` is read-only and advisory. The legacy
  ``is_recoverable_error`` remains the source of truth for reconnect
  gating in :class:`VoiceRelay`. Wherever the new classifier emits a
  ``VoiceError`` row, its ``recoverable`` flag MUST match what the
  legacy gate would return for the same exception.

Without this contract the relay could short-circuit a reconnect the
classifier didn't expect, or keep retrying after the classifier flagged
the close non-recoverable. Both regressions silently degrade UX —
parity tests are the way to surface them at CI time.

See plan §11 (Increment A) and §0.3 (parity-test rule).
"""

from __future__ import annotations

import pytest

voice_errors = pytest.importorskip("orchestrator.voice_errors")

from orchestrator.providers.gemini_voice import GeminiAIStudioBackend
from orchestrator.providers.openai_voice import OpenAIVoiceProvider
from orchestrator.providers.qwen_voice import QwenVoiceProvider


# Fixtures: (label, exc-factory, ws_code, ws_reason).
# `exc-factory` so each test gets a fresh exception (no shared mutable
# state across tests) and so the legacy gate's state mutations
# (Gemini stale-handle one-shot) don't leak between fixtures.
_GEMINI_FIXTURES = [
    (
        "1011_billing_cap",
        lambda: ConnectionError(
            "received 1011 (internal error) Your project has exceeded its "
            "monthly spending cap"
        ),
        1011,
        "Your project has exceeded its monthly spending cap",
    ),
    (
        "1008_denied_access",
        lambda: ConnectionError(
            "received 1008 (policy violation) Your project has been denied "
            "access to the Live API"
        ),
        1008,
        "Your project has been denied access to the Live API",
    ),
    (
        "1008_model_unavailable",
        lambda: ConnectionError(
            "received 1008 (policy violation) Model not found"
        ),
        1008,
        "Model not found",
    ),
    (
        "1006_transport_only",
        lambda: ConnectionError(
            "received 1006 (no close frame received or sent)"
        ),
        1006,
        None,
    ),
]

_QWEN_FIXTURES = [
    (
        "1007_invalid_parameter_recoverable",
        lambda: ConnectionError(
            "received 1007 (invalid frame payload data) <400> "
            "InternalError.Algo.InvalidParameter: The provided URL does "
            "not appear to be valid"
        ),
        1007,
        "InvalidParameter: The provided URL does not appear to be valid",
    ),
    (
        "response_idle_timeout_recoverable",
        lambda: ConnectionError("response_idle_timeout"),
        None,
        "response_idle_timeout",
    ),
    (
        "1011_balance_insufficient",
        lambda: ConnectionError(
            "received 1011 (internal error) Account balance insufficient"
        ),
        1011,
        "Account balance insufficient",
    ),
    (
        "1008_invalid_api_key",
        lambda: ConnectionError(
            "received 1008 (policy violation) InvalidApiKey"
        ),
        1008,
        "InvalidApiKey",
    ),
]


# OpenAI is webrtc — `is_recoverable_error` is the inherited default
# (always False). Parity here just says: whenever classifier emits a
# row, recoverable matches False... EXCEPT we authorize RATE_LIMIT as
# recoverable=True in the plan (recoverable transient throttle, see
# taxonomy in plan §5). The live gate doesn't see those because the
# relay never closes an OpenAI connection. So we exempt OpenAI from
# strict-agreement and only assert the classifier's own consistency
# (recoverable=False for quota/auth/model_unavailable, True for
# rate_limit/network). That's a SEPARATE assertion already covered in
# `test_voice_error_classification_parity.py`; no fixture list here.


@pytest.mark.parametrize("label,exc_factory,ws_code,ws_reason", _GEMINI_FIXTURES)
def test_gemini_recoverable_agrees_with_classifier(
    label: str, exc_factory, ws_code: int | None, ws_reason: str | None
):
    """For each Gemini fixture: if the classifier emits a row, its
    `recoverable` flag must match what `is_recoverable_error` returns.

    The legacy gate's stateful one-shot behavior is preserved — we use
    fresh provider instances per fixture so state never leaks.
    """
    # Fresh classifier instance.
    p1 = GeminiAIStudioBackend(model="gemini-live-2.5-flash-preview-native-audio")
    # The classifier MUST NOT mutate provider state (design contract).
    snapshot_before = (
        p1._goaway_received,
        p1._resumption_handle,
        p1._stale_handle_recovery_used,
    )
    err = p1.classify_close_reason(exc_factory(), ws_code, ws_reason)
    snapshot_after = (
        p1._goaway_received,
        p1._resumption_handle,
        p1._stale_handle_recovery_used,
    )
    assert snapshot_before == snapshot_after, (
        f"{label}: classifier mutated provider state; this violates the "
        "read-only contract. Only `is_recoverable_error` may mutate."
    )

    # Fresh live-gate instance — separate, so the classifier call above
    # cannot have polluted it.
    p2 = GeminiAIStudioBackend(model="gemini-live-2.5-flash-preview-native-audio")
    live_recoverable = p2.is_recoverable_error(exc_factory())

    if err is not None:
        assert err.recoverable == live_recoverable, (
            f"{label}: classifier says recoverable={err.recoverable} but "
            f"live gate says {live_recoverable}; "
            "they MUST agree (plan §11 parity contract)."
        )


@pytest.mark.parametrize("label,exc_factory,ws_code,ws_reason", _QWEN_FIXTURES)
def test_qwen_recoverable_agrees_with_classifier(
    label: str, exc_factory, ws_code: int | None, ws_reason: str | None
):
    p1 = QwenVoiceProvider(model="qwen3-omni-flash-realtime", voice="Cherry")
    err = p1.classify_close_reason(exc_factory(), ws_code, ws_reason)
    p2 = QwenVoiceProvider(model="qwen3-omni-flash-realtime", voice="Cherry")
    live_recoverable = p2.is_recoverable_error(exc_factory())
    if err is not None:
        assert err.recoverable == live_recoverable, (
            f"{label}: classifier says recoverable={err.recoverable} but "
            f"live gate says {live_recoverable}."
        )


def test_classifier_is_pure_for_openai():
    """OpenAI provider's classifier must not mutate state either.

    OpenAI is webrtc — the relay never invokes `is_recoverable_error`
    for it, so there's no live gate to compare against. We just enforce
    the read-only contract.
    """
    p = OpenAIVoiceProvider(model="gpt-realtime", voice="cedar")
    queue_before = p._queue.qsize()
    transcript_before = p._current_transcript
    p.classify_close_reason(
        RuntimeError('{"error": {"code": "insufficient_quota"}}'),
        None,
        '{"error": {"code": "insufficient_quota"}}',
    )
    assert p._queue.qsize() == queue_before
    assert p._current_transcript == transcript_before
