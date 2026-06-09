"""Parity tests for typed VoiceError classification (Increment A).

Pins each provider's `classify_close_reason` output to the canonical wire
phrases observed in live logs. The test fixtures here are the
ground-truth strings the legacy `is_recoverable_error` + close-summary
code path was written against — by asserting the new classifier
produces messages containing those same phrases, log searches and
diagnostic playbooks keep working post-refactor.

See plan §11 (Increment A) and §5 (VoiceError taxonomy).

This file is collected against HEAD (pre-refactor) via
``pytest.importorskip``: the classifier module does not yet exist, so
the whole file is skipped cleanly. After Increment A lands, every
assertion below must pass.
"""

from __future__ import annotations

import pytest

# Classifier module — does not exist at HEAD 9c25e07. The whole file
# skips until Increment A's implementation introduces it.
voice_errors = pytest.importorskip("orchestrator.voice_errors")
VoiceError = voice_errors.VoiceError
VoiceErrorCategory = voice_errors.VoiceErrorCategory

from orchestrator.providers.gemini_voice import GeminiAIStudioBackend
from orchestrator.providers.openai_voice import OpenAIVoiceProvider
from orchestrator.providers.qwen_voice import QwenVoiceProvider


def _gemini():
    # AI Studio backend; voice provider behavior under test does not
    # depend on which backend (the classifier is on the shared base).
    return GeminiAIStudioBackend(model="gemini-live-2.5-flash-preview-native-audio")


def _openai():
    return OpenAIVoiceProvider(model="gpt-realtime", voice="cedar")


def _qwen():
    return QwenVoiceProvider(model="qwen3-omni-flash-realtime", voice="Cherry")


# --- Google AI Studio ------------------------------------------------

def test_google_quota_exceeded_classified():
    """The 2026-06-08 14:23 freeze: WS 1011 with the AI Studio billing
    cap message. Bug 1 of the log inventory.
    """
    exc = ConnectionError(
        "received 1011 (internal error) Your project has exceeded its "
        "monthly spending cap. Please go to AI Studio at https://ai.studio/spend"
    )
    err = _gemini().classify_close_reason(exc, 1011, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False
    assert "exceeded its monthly spending cap" in err.message
    assert err.provider == "google"


def test_google_auth_denied_classified():
    """1008 with the AI Studio project-denied close (structural §5)."""
    exc = ConnectionError(
        "received 1008 (policy violation) Your project has been denied "
        "access to the Live API"
    )
    err = _gemini().classify_close_reason(exc, 1008, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.AUTH
    assert err.recoverable is False
    assert "denied access" in err.message
    assert err.provider == "google"


def test_google_model_unavailable_classified():
    """Regional / unsupported model close."""
    exc = ConnectionError(
        "received 1008 (policy violation) Model not found or not available "
        "in your region"
    )
    err = _gemini().classify_close_reason(exc, 1008, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.MODEL_UNAVAILABLE
    assert err.recoverable is False
    assert err.provider == "google"


def test_google_session_expired_recoverable_unchanged():
    """The existing 1008 "session expired" stale-handle recovery
    classifies as recoverable. This is the load-bearing parity check —
    the classifier must agree with the existing
    :meth:`is_recoverable_error` on this established path.
    """
    p = _gemini()
    # Set up the stale-handle precondition so is_recoverable_error agrees.
    p._resumption_handle = "some-handle"
    p._stale_handle_recovery_used = False
    exc = ConnectionError(
        "received 1008 (policy violation) BidiGenerateContent session expired"
    )
    err = p.classify_close_reason(exc, 1008, exc.args[0])
    # Classifier is read-only; safe to call BEFORE is_recoverable_error.
    if err is not None:
        # If classified, recoverability must match the live gate.
        assert err.recoverable is True
    # Live gate is the source of truth (and the one that mutates state).
    p2 = _gemini()
    p2._resumption_handle = "some-handle"
    p2._stale_handle_recovery_used = False
    assert p2.is_recoverable_error(exc) is True


def test_google_1006_transient_network():
    """Raw transport close with no semantic content → NETWORK, recoverable."""
    exc = ConnectionError("received 1006 (no close frame received or sent)")
    err = _gemini().classify_close_reason(exc, 1006, exc.args[0])
    # 1006 may be classified as NETWORK or left unclassified (None) and
    # default-filled by the relay. Either way recoverability is True.
    if err is not None:
        assert err.category == VoiceErrorCategory.NETWORK
        assert err.recoverable is True


# --- OpenAI Realtime -------------------------------------------------

def test_openai_insufficient_quota_classified():
    """OpenAI's billing-exhausted shape — observed in voice/session
    HTTPStatusError bodies and (when the WS exists) in close reasons.
    Per structural §5.
    """
    exc = RuntimeError(
        '{"error": {"code": "insufficient_quota", "message": '
        '"You exceeded your current quota, please check your plan and billing details."}}'
    )
    err = _openai().classify_close_reason(exc, None, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False
    assert "exceeded your current quota" in err.message or "insufficient_quota" in err.message
    assert err.provider == "openai"


def test_openai_auth_classified():
    exc = RuntimeError('{"error": {"message": "Incorrect API key provided"}}')
    err = _openai().classify_close_reason(exc, 401, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.AUTH
    assert err.recoverable is False
    assert "Incorrect API key" in err.message
    assert err.provider == "openai"


def test_openai_rate_limit_classified():
    exc = RuntimeError(
        '{"error": {"type": "rate_limit_exceeded", "message": "Rate limit"}}'
    )
    err = _openai().classify_close_reason(exc, 429, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.RATE_LIMIT
    assert err.recoverable is True  # Auto-retry with backoff
    assert err.provider == "openai"


def test_openai_model_not_found_classified():
    exc = RuntimeError('{"error": {"code": "model_not_found"}}')
    err = _openai().classify_close_reason(exc, None, str(exc))
    assert err is not None
    assert err.category == VoiceErrorCategory.MODEL_UNAVAILABLE
    assert err.recoverable is False
    assert err.provider == "openai"


# --- Qwen / DashScope -------------------------------------------------

def test_qwen_balance_classified():
    """DashScope balance-depleted close. Per structural §5."""
    exc = ConnectionError(
        "received 1011 (internal error) Account balance insufficient"
    )
    err = _qwen().classify_close_reason(exc, 1011, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False
    assert err.provider == "qwen"


def test_qwen_balance_chinese_variant_classified():
    """DashScope sometimes localises — 余额不足 is the Chinese variant."""
    exc = ConnectionError("received 1011 (internal error) 余额不足")
    err = _qwen().classify_close_reason(exc, 1011, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.QUOTA_EXCEEDED
    assert err.recoverable is False
    assert err.provider == "qwen"


def test_qwen_invalid_api_key_classified():
    exc = ConnectionError("received 1008 (policy violation) InvalidApiKey")
    err = _qwen().classify_close_reason(exc, 1008, exc.args[0])
    assert err is not None
    assert err.category == VoiceErrorCategory.AUTH
    assert err.recoverable is False
    assert "InvalidApiKey" in err.message
    assert err.provider == "qwen"


def test_qwen_invalid_parameter_recoverable_unchanged():
    """The DashScope `InvalidParameter` boilerplate is the existing
    recoverable path. The classifier must agree with the live gate.
    """
    exc = ConnectionError(
        "received 1007 (invalid frame payload data) <400> "
        "InternalError.Algo.InvalidParameter: The provided URL does not "
        "appear to be valid"
    )
    p = _qwen()
    # Live gate (unchanged): recoverable.
    assert p.is_recoverable_error(exc) is True
    err = p.classify_close_reason(exc, 1007, exc.args[0])
    # If the classifier emits a row for this fixture, it MUST agree.
    if err is not None:
        assert err.recoverable is True


def test_qwen_response_idle_timeout_recoverable_unchanged():
    """Qwen's 5-min ASR idle-timeout close. Existing recoverable path."""
    exc = ConnectionError("response_idle_timeout")
    p = _qwen()
    assert p.is_recoverable_error(exc) is True
    err = p.classify_close_reason(exc, None, str(exc))
    if err is not None:
        assert err.recoverable is True


# --- Generic fallback -------------------------------------------------

def test_unclassifiable_close_returns_none():
    """The default ``BaseVoiceProvider.classify_close_reason`` returns
    None — the relay then synthesises a generic NETWORK envelope.
    """
    from orchestrator.providers.voice_base import BaseVoiceProvider
    # Smoke check: default on the ABC returns None. We call through one
    # concrete instance to make the assertion meaningful.
    exc = RuntimeError("some opaque transport error")
    err = _gemini().classify_close_reason(exc, None, None)
    # Unrecognised → None is acceptable (relay applies fallback).
    assert err is None or err.category in (
        VoiceErrorCategory.UNKNOWN,
        VoiceErrorCategory.NETWORK,
        VoiceErrorCategory.PROVIDER_INTERNAL,
    )
