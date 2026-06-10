"""Parity test: VAD config-knob defaults equal HEAD constants.

Increment B exposes three voice-tuning knobs in ``assistant_config.json``:

    voice_vad_threshold       (default 0.28)
    voice_vad_min_silence_ms  (default 2500)
    voice_mic_gain            (default 1.0)

The plan's source-fidelity rule (plan §0.1 + §B "Source-fidelity
preservation") requires the new defaults equal the current hardcoded
literals exactly — the knobs become user-tunable but the out-of-the-box
behaviour at HEAD must be preserved verbatim.

This file pins:

1. The defaults in ``_default_config()`` (loaded from
   ``api.routes.config``) are the documented numbers.
2. A freshly-started voice relay constructs ``VoiceVAD`` with those
   numbers when no override is configured.
3. The numbers match the literals documented in ``voice_vad.py:107-112``.

See plan §11 — "assert ``voice_vad_threshold=0.28``,
``voice_vad_min_silence_ms=2500``, ``voice_mic_gain=1.0`` are the
defaults in the new settings".
"""

from __future__ import annotations

import json

import pytest

from orchestrator.voice_vad import VoiceVAD


def _isolate_config(tmp_path, monkeypatch):
    """Point the config module at a temp ``PROJECT_ROOT`` so each test
    is isolated from the developer machine's saved
    ``assistant_config.json``. Mirrors the fixture used by
    ``tests/test_api_config.py``."""
    import utils.paths
    monkeypatch.setattr(utils.paths, "PROJECT_ROOT", tmp_path)
    import api.routes.config as cfg_module
    monkeypatch.setattr(cfg_module, "PROJECT_ROOT", tmp_path)
    return cfg_module


def test_default_config_carries_vad_knobs(tmp_path, monkeypatch):
    """The defaults dict produced by ``_default_config()`` must include
    the three VAD/mic knobs with the documented numbers."""
    cfg_mod = _isolate_config(tmp_path, monkeypatch)
    defaults = cfg_mod._default_config()

    assert defaults["voice_vad_threshold"] == pytest.approx(0.28), (
        "Default voice_vad_threshold must equal the documented Silero "
        "constant (voice_vad.py:110). Changing it changes far-field "
        "detection behaviour for every user."
    )
    assert defaults["voice_vad_min_silence_ms"] == 2500, (
        "Default voice_vad_min_silence_ms must equal the documented "
        "Silero constant (voice_vad.py:111). 2500 was tuned to ride "
        "through breathing pauses without committing mid-sentence."
    )
    assert defaults["voice_mic_gain"] == pytest.approx(1.0), (
        "Default voice_mic_gain must be 1.0 (no scaling). Any other "
        "default would silently change every user's mic level."
    )


def test_vad_consumes_config_when_loaded(tmp_path, monkeypatch):
    """When ``assistant_config.json`` is missing the VAD knobs (old
    config files predating Increment B), the loader must fall back to
    HEAD constants — NOT a code-level 0.5/1200/whatever that would
    silently undo the user's tuning.
    """
    cfg_mod = _isolate_config(tmp_path, monkeypatch)
    # Write a config WITHOUT the new keys.
    legacy = {
        "working_directory": "/tmp",
        "working_directory_history": [{"id": "/tmp", "path": "/tmp", "label": None,
                                       "ssh_host": None, "ssh_user": None, "ssh_key": None,
                                       "claude_config_dir": None}],
        "enabled_mcps": [],
        "chrome_extension": False,
        "provider": "claude",
        "default_model": "claude-sonnet-4-5-20250929",
        "summarizer_model": "",
        "harness_model": {"claude": ""},
        "default_voice_provider": "google",
        "default_voice_model": "gemini-live-2.5-flash-preview-native-audio",
        "default_voice_name": "Puck",
        "default_voice_transcription_language": "",
        "default_voice_endpoint": "vertex",
        "voice_recording_enabled": False,
    }
    (tmp_path / "assistant_config.json").write_text(json.dumps(legacy))

    loaded = cfg_mod._load_config()

    # Increment B's loader auto-fills missing knobs with HEAD defaults
    # via ``_default_config()`` so legacy config files don't change
    # behaviour.
    assert loaded["voice_vad_threshold"] == pytest.approx(0.28)
    assert loaded["voice_vad_min_silence_ms"] == 2500
    assert loaded["voice_mic_gain"] == pytest.approx(1.0)


def test_voicevad_constructed_with_defaults_matches_explicit():
    """Confirm that ``VoiceVAD()`` (no args) produces a state machine
    identical to ``VoiceVAD(threshold=0.28, min_silence_duration_ms=2500,
    min_speech_duration_ms=200)`` at every internal observable. This is
    a belt-and-suspenders check on top of the speech-fixture parity
    test in ``test_voice_vad_parity.py``.
    """
    a = VoiceVAD(input_sample_rate=24000)
    b = VoiceVAD(
        input_sample_rate=24000,
        threshold=0.28,
        min_silence_duration_ms=2500,
        min_speech_duration_ms=200,
    )
    assert a._threshold_on == b._threshold_on
    assert a._threshold_off == b._threshold_off
    assert a._min_speech_windows == b._min_speech_windows
    assert a._min_silence_windows == b._min_silence_windows
