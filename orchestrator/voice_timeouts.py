"""Centralised voice-pipeline timeouts.

Increment F (plan §F) collects every numeric constant the voice
pipeline reads at runtime into one frozen dataclass. Two motivations:

1. **Discoverability**: at HEAD the constants are scattered across
   ``voice_relay.py`` (4 module-level ``_X_S`` floats),
   ``session.py`` (``_GRACEFUL_SHUTDOWN_TIMEOUT_S``), and the route
   layer (``await_orchestrator_stop(timeout=5.0)``). The dataclass
   makes them one ``grep -n VoiceTimeouts`` away.

2. **Test-time overrides**: ``OrchestratorSession`` and
   ``VoiceRelay`` already accept per-instance config. With timeouts
   on a single object, a test fixture that wants
   ``graceful_shutdown_s=0.01`` can flip exactly that knob without
   monkey-patching module globals.

Defaults EQUAL the pre-Inc-F constants byte-for-byte. Per plan §0.1
this refactor must NOT change behavior — only the data structure
that holds the numbers. The parity test
``tests/test_voice_timeouts.py::test_defaults_equal_head_constants``
pins that contract.

Nothing here is wired to ``assistant_config.json`` YET — ``from_config``
is provided as the seam for a future user-facing config UI (§F notes
the next step), but the live wire-up stays defaults-only for now.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class VoiceTimeouts:
    """Frozen bundle of voice-pipeline timeouts.

    All values are seconds unless the name is suffixed ``_ms``. Each
    field's docstring cites the HEAD literal it replaces.
    """

    # ``api/routes/orchestrator.py`` — bounded wait inside
    # ``pool.await_orchestrator_stop(local_id, timeout=...)`` after a
    # voice tear-down request. 5s is long enough for an in-flight
    # graceful_shutdown to flush its frames and the relay to close.
    await_orchestrator_stop_s: float = 5.0

    # ``orchestrator/session.py:_GRACEFUL_SHUTDOWN_TIMEOUT_S`` —
    # bound on ``relay.send_shutdown_frames(...)`` during
    # ``end_voice``. Long enough for a clean activityEnd /
    # commit frame to flush; tight enough that a dead upstream
    # doesn't pin the teardown for seconds.
    graceful_shutdown_s: float = 0.5

    # ``orchestrator/voice_relay.py:_RECONNECT_HANDSHAKE_TIMEOUT_S``
    # — bound on the new-upstream ``open_and_handshake`` inside the
    # reconnect path. 15s is the post-2026-06-04 observation: a stuck
    # ``websockets.connect`` had hung for 40s+ on goAway #3 of a long
    # Gemini session; 15s lets a slow Anthropic/Gemini handshake
    # complete but flags genuinely-broken upstream.
    reconnect_handshake_s: float = 15.0

    # ``orchestrator/voice_relay.py:_MANUAL_VAD_SAFETY_COMMIT_S`` —
    # how long the manual-VAD path will keep buffering before forcing
    # a commit + response.create. DashScope documents a 60-second cap;
    # 50s stays comfortably below.
    manual_vad_safety_commit_s: float = 50.0

    # ``orchestrator/voice_relay.py:_KEEPALIVE_INTERVAL_S`` — cadence
    # for the silent-PCM keepalive that prevents Qwen-Omni's ASR
    # pipeline from timing out after ~3-5 min of audio silence. 30s
    # keeps comfortably under the ceiling while bandwidth stays
    # negligible.
    keepalive_s: float = 30.0

    # ``orchestrator/voice_relay.py:_VAD_STATE_HEARTBEAT_S`` —
    # cadence at which the relay re-broadcasts ``voice_vad_state``
    # while the VAD remains in ``listening``. Drives the UI's
    # duration clock between Silero transitions.
    vad_state_heartbeat_s: float = 1.0

    @classmethod
    def default(cls) -> VoiceTimeouts:
        """Return a frozen instance with HEAD-default values."""
        return cls()

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> VoiceTimeouts:
        """Construct from a config dict (e.g. ``assistant_config.json``).

        Reads from ``cfg["voice_timeouts"]`` (an optional sub-dict).
        Unknown keys are silently dropped — additive future-compat.
        Missing keys keep the default. The shape is forward-compatible
        with a user-facing config UI (§F notes), but Inc F doesn't
        wire that pipeline up — callers pass ``VoiceTimeouts.default()``
        for now.
        """
        sub = cfg.get("voice_timeouts", {}) or {}
        known = {f for f in cls.default().__dict__}
        filtered = {k: v for k, v in sub.items() if k in known}
        return replace(cls.default(), **filtered)
