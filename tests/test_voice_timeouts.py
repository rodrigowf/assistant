"""Increment F — VoiceTimeouts parity + override tests.

Plan §F is a "very low risk" refactor: collect the scattered numeric
constants into one frozen dataclass without changing their values.

The parity test pins that the defaults equal the pre-refactor HEAD
literals exactly. The override test confirms the construct-time knob
flows through ``replace`` cleanly.

If any number here is changed without a matching plan update, that's
a behavior change and CI catches it.
"""

from __future__ import annotations

import pytest

from orchestrator.voice_timeouts import VoiceTimeouts


def test_defaults_equal_head_constants():
    """Numerics MUST equal the pre-Inc-F literal constants exactly.

    Pre-Inc-F sources:
    - ``voice_relay.py:_KEEPALIVE_INTERVAL_S = 30.0``
    - ``voice_relay.py:_MANUAL_VAD_SAFETY_COMMIT_S = 50.0``
    - ``voice_relay.py:_VAD_STATE_HEARTBEAT_S = 1.0``
    - ``voice_relay.py:_RECONNECT_HANDSHAKE_TIMEOUT_S = 15.0``
    - ``session.py:_GRACEFUL_SHUTDOWN_TIMEOUT_S = 0.5``
    - ``api/routes/orchestrator.py``: ``await_orchestrator_stop(...,
      timeout=5.0)``
    """
    t = VoiceTimeouts.default()
    assert t.await_orchestrator_stop_s == pytest.approx(5.0)
    assert t.graceful_shutdown_s == pytest.approx(0.5)
    assert t.reconnect_handshake_s == pytest.approx(15.0)
    assert t.manual_vad_safety_commit_s == pytest.approx(50.0)
    assert t.keepalive_s == pytest.approx(30.0)
    assert t.vad_state_heartbeat_s == pytest.approx(1.0)


def test_from_config_empty_returns_defaults():
    """Empty / missing ``voice_timeouts`` dict yields defaults."""
    assert VoiceTimeouts.from_config({}) == VoiceTimeouts.default()
    assert VoiceTimeouts.from_config({"voice_timeouts": None}) == VoiceTimeouts.default()
    assert VoiceTimeouts.from_config({"voice_timeouts": {}}) == VoiceTimeouts.default()


def test_from_config_applies_partial_override():
    """Known keys override; unknown keys silently drop (forward-compat)."""
    t = VoiceTimeouts.from_config({
        "voice_timeouts": {
            "graceful_shutdown_s": 0.01,
            "nonexistent_field": 999,
        },
    })
    assert t.graceful_shutdown_s == pytest.approx(0.01)
    # Other defaults preserved.
    assert t.keepalive_s == pytest.approx(30.0)
    assert t.reconnect_handshake_s == pytest.approx(15.0)


def test_is_frozen():
    """Frozen dataclass — accidental mutation raises rather than
    silently shifting timeouts mid-session.
    """
    t = VoiceTimeouts.default()
    with pytest.raises(Exception):  # FrozenInstanceError subclass of Exception
        t.graceful_shutdown_s = 99.0  # type: ignore[misc]
