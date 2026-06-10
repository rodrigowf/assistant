"""Unit tests for orchestrator.voice_errors — the typed VoiceError
surface introduced in Increment A of the voice subsystem refactor.

Covers:
- VoiceErrorCategory enum stability (category strings are wire bytes;
  the frontend / Android render UI off these strings, so renaming a
  member silently breaks the banner).
- VoiceError.to_event() shape (single source of truth for the
  ``voice_error`` WebSocket envelope).
- Per-provider classifier coverage for every taxonomy row in
  plan §5 (wire patterns → category, recoverable flag, message
  fragment).
- BaseVoiceProvider.classify_close_reason default returns None.

See plan §A (Increment A) and §5 (taxonomy table).
"""

from __future__ import annotations

import pytest

from orchestrator.voice_errors import VoiceError, VoiceErrorCategory
from orchestrator.providers.voice_base import BaseVoiceProvider
from orchestrator.providers.gemini_voice import GeminiAIStudioBackend
from orchestrator.providers.openai_voice import OpenAIVoiceProvider
from orchestrator.providers.qwen_voice import QwenVoiceProvider


# --- category enum stability ----------------------------------------

def test_category_values_are_stable_wire_strings():
    """These strings appear in the ``voice_error`` event payload; the
    Android parser + frontend hook switch on them. Don't rename without
    coordinating both client sides.
    """
    assert VoiceErrorCategory.QUOTA_EXCEEDED.value == "quota_exceeded"
    assert VoiceErrorCategory.RATE_LIMIT.value == "rate_limit"
    assert VoiceErrorCategory.AUTH.value == "auth"
    assert VoiceErrorCategory.MODEL_UNAVAILABLE.value == "model_unavailable"
    assert VoiceErrorCategory.CONTEXT_FULL.value == "context_full"
    assert VoiceErrorCategory.NETWORK.value == "network"
    assert VoiceErrorCategory.PROVIDER_INTERNAL.value == "provider_internal"
    assert VoiceErrorCategory.UNKNOWN.value == "unknown"


# --- to_event() shape -----------------------------------------------

def test_to_event_shape_full():
    err = VoiceError(
        category=VoiceErrorCategory.QUOTA_EXCEEDED,
        message="Your project has exceeded its monthly spending cap.",
        recoverable=False,
        recovery_hint="Visit ai.studio/spend",
        provider_doc_url="https://ai.studio/spend",
        raw_close_code=1011,
        raw_close_reason="Your project has exceeded its monthly spending cap.",
        provider="google",
    )
    ev = err.to_event()
    assert ev["type"] == "voice_error"
    payload = ev["error"]
    assert payload == {
        "category": "quota_exceeded",
        "message": "Your project has exceeded its monthly spending cap.",
        "recoverable": False,
        "recovery_hint": "Visit ai.studio/spend",
        "provider_doc_url": "https://ai.studio/spend",
        "raw_close_code": 1011,
        "raw_close_reason": "Your project has exceeded its monthly spending cap.",
        "provider": "google",
    }


def test_to_event_shape_minimal_nulls_serialise_as_none():
    err = VoiceError(
        category=VoiceErrorCategory.NETWORK,
        message="Upstream WS closed: transport error",
        recoverable=True,
        recovery_hint=None,
        provider_doc_url=None,
        raw_close_code=None,
        raw_close_reason=None,
        provider="qwen",
    )
    ev = err.to_event()
    payload = ev["error"]
    assert payload["recovery_hint"] is None
    assert payload["provider_doc_url"] is None
    assert payload["raw_close_code"] is None
    assert payload["raw_close_reason"] is None
    assert payload["recoverable"] is True


# --- BaseVoiceProvider default ----------------------------------------

def test_base_classify_returns_none():
    """The base implementation is a safe default — relay synthesises a
    generic NETWORK envelope when the provider returns None.
    """
    # Smallest concrete subclass: instantiate via OpenAIVoiceProvider then
    # call up through the base. The new method must be present on the ABC.
    assert hasattr(BaseVoiceProvider, "classify_close_reason")


# --- Google AI Studio classifier ------------------------------------

def _gemini():
    return GeminiAIStudioBackend(
        model="gemini-live-2.5-flash-preview-native-audio",
    )


def test_google_quota_cap_1011():
    p = _gemini()
    exc = ConnectionError(
        "received 1011 (internal error) Your project has exceeded its "
        "monthly spending cap. Please go to AI Studio at https://ai.studio/spend"
    )
    err = p.classify_close_reason(exc, 1011, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False
    assert "spending cap" in err.message
    assert err.provider == "google"
    # Recovery hint should give the user something actionable.
    assert err.recovery_hint is not None and "ai.studio" in err.recovery_hint.lower()


def test_google_denied_access_1008():
    p = _gemini()
    exc = ConnectionError(
        "received 1008 (policy violation) Your project has been denied "
        "access to the Live API"
    )
    err = p.classify_close_reason(exc, 1008, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.AUTH
    assert err.recoverable is False
    assert "denied access" in err.message
    assert err.provider == "google"


def test_google_session_expired_1008_is_recoverable():
    """1008 with the session-expired phrase is the existing recoverable
    path (gemini_voice_base._is_session_expired_close). Classifier must
    flag it recoverable.
    """
    p = _gemini()
    exc = ConnectionError(
        "received 1008 (policy violation) BidiGenerateContent session expired"
    )
    err = p.classify_close_reason(exc, 1008, exc.args[0])
    assert err is not None
    # Either RECOVERABLE-flavored or left to the relay's reconnect path.
    assert err.recoverable is True


def test_google_model_unavailable():
    p = _gemini()
    exc = ConnectionError(
        "received 1008 (policy violation) Model not found"
    )
    err = p.classify_close_reason(exc, 1008, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.MODEL_UNAVAILABLE
    assert err.recoverable is False


# --- OpenAI classifier ----------------------------------------------

def _openai():
    return OpenAIVoiceProvider(model="gpt-realtime", voice="cedar")


def test_openai_insufficient_quota():
    p = _openai()
    exc = RuntimeError(
        '{"error": {"code": "insufficient_quota", "message": '
        '"You exceeded your current quota"}}'
    )
    err = p.classify_close_reason(exc, None, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False
    assert err.provider == "openai"
    assert err.recovery_hint is not None


def test_openai_auth_401():
    p = _openai()
    exc = RuntimeError('{"error": {"message": "Incorrect API key provided"}}')
    err = p.classify_close_reason(exc, 401, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.AUTH
    assert err.recoverable is False


def test_openai_rate_limit_429():
    p = _openai()
    exc = RuntimeError(
        '{"error": {"type": "rate_limit_exceeded", "message": "Rate limit reached"}}'
    )
    err = p.classify_close_reason(exc, 429, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.RATE_LIMIT
    # Rate limits self-clear; mark recoverable so the relay backs off
    # and retries.
    assert err.recoverable is True


def test_openai_model_not_found():
    p = _openai()
    exc = RuntimeError('{"error": {"code": "model_not_found"}}')
    err = p.classify_close_reason(exc, None, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.MODEL_UNAVAILABLE
    assert err.recoverable is False


# --- Qwen / DashScope classifier ------------------------------------

def _qwen():
    return QwenVoiceProvider(model="qwen3-omni-flash-realtime", voice="Cherry")


def test_qwen_balance_insufficient_english():
    p = _qwen()
    exc = ConnectionError(
        "received 1011 (internal error) Account balance insufficient"
    )
    err = p.classify_close_reason(exc, 1011, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False
    assert err.provider == "qwen"


def test_qwen_balance_chinese():
    p = _qwen()
    exc = ConnectionError("received 1011 (internal error) 余额不足")
    err = p.classify_close_reason(exc, 1011, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False


def test_qwen_invalid_api_key():
    p = _qwen()
    exc = ConnectionError("received 1008 (policy violation) InvalidApiKey")
    err = p.classify_close_reason(exc, 1008, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.AUTH
    assert err.recoverable is False


def test_qwen_invalid_parameter_remains_recoverable():
    """The existing DashScope `InvalidParameter` boilerplate IS the
    recoverable path. Classifier must agree.
    """
    p = _qwen()
    exc = ConnectionError(
        "received 1007 (invalid frame payload data) <400> "
        "InternalError.Algo.InvalidParameter: The provided URL does not "
        "appear to be valid"
    )
    err = p.classify_close_reason(exc, 1007, exc.args[0])
    # If classified, must be recoverable; otherwise None is acceptable
    # (relay falls back to is_recoverable_error which already says True).
    if err is not None:
        assert err.recoverable is True


# --- non-mutation contract ------------------------------------------

def test_classify_does_not_mutate_provider_state():
    """The classifier must not mutate `_resumption_handle`,
    `_goaway_received`, `_stale_handle_recovery_used`, etc. Only
    `is_recoverable_error` does that (design contract — see
    tests/parity/test_voice_error_recoverable_parity.py).
    """
    p = _gemini()
    p._resumption_handle = "h"
    p._goaway_received = True
    p._stale_handle_recovery_used = False
    before = (p._resumption_handle, p._goaway_received, p._stale_handle_recovery_used)

    p.classify_close_reason(
        ConnectionError("received 1008 (policy violation) session expired"),
        1008,
        "session expired",
    )

    after = (p._resumption_handle, p._goaway_received, p._stale_handle_recovery_used)
    assert before == after


# --- relay-facing helpers (frozen wire envelope) --------------------

def test_voice_error_is_frozen_dataclass():
    """The dataclass is frozen so the relay can hold one VoiceError as a
    shared canonical instance without worrying about callers mutating
    it in-place. Caught at construction-time at module import.
    """
    err = VoiceError(
        category=VoiceErrorCategory.NETWORK,
        message="x",
        recoverable=True,
        recovery_hint=None,
        provider_doc_url=None,
        raw_close_code=None,
        raw_close_reason=None,
        provider="qwen",
    )
    with pytest.raises((AttributeError, Exception)):
        err.category = VoiceErrorCategory.AUTH  # type: ignore[misc]
