"""Tests for ``api/routes/session_config.py``.

Per-session config supplements the global ``assistant_config.json`` with
session-specific overrides.  This file pins the behavior of two recent
additions: the ``provider`` field (pinned per-session because switching
the CLI behind an existing JSONL would corrupt its shape) and the
``harness_model`` field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.routes.session_config import (
    _ALLOWED_KEYS,
    _DEFAULTS,
    load_session_config,
    save_session_config,
)


@pytest.fixture
def context_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``get_context_dir()`` at a temp dir so tests don't write to
    the real context/."""
    import utils.paths
    monkeypatch.setattr(utils.paths, "get_context_dir", lambda: tmp_path)
    # session_config.py captured the function at import time
    import api.routes.session_config as sc
    monkeypatch.setattr(sc, "get_context_dir", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Schema


def test_provider_and_harness_model_are_allowed_keys() -> None:
    """Regression: these were added in the per-session-provider work.
    If someone removes them, this test catches it before chat.py starts
    silently dropping the values."""
    assert "provider" in _ALLOWED_KEYS
    assert "harness_model" in _ALLOWED_KEYS


def test_defaults_have_provider_and_harness_model_as_none() -> None:
    """None == "inherit from global config"; tests downstream rely on
    that exact sentinel."""
    assert _DEFAULTS["provider"] is None
    assert _DEFAULTS["harness_model"] is None


# ---------------------------------------------------------------------------
# Load


def test_load_missing_file_returns_defaults(context_dir: Path) -> None:
    """No config written yet — every field defaults to None (inherit)."""
    cfg = load_session_config("missing-sid")
    assert cfg == dict(_DEFAULTS)
    # Spot-check the new keys
    assert cfg["provider"] is None
    assert cfg["harness_model"] is None


def test_load_preserves_persisted_provider(context_dir: Path) -> None:
    """A session config written by an earlier resume should round-trip."""
    (context_dir / "sid-abc.config.json").write_text(json.dumps({
        "provider": "qwen",
        "harness_model": "qwen3.6-plus",
    }))
    cfg = load_session_config("sid-abc")
    assert cfg["provider"] == "qwen"
    assert cfg["harness_model"] == "qwen3.6-plus"
    # The other fields remain at default
    assert cfg["working_directory"] is None
    assert cfg["enabled_mcps"] is None


def test_load_ignores_unknown_keys(context_dir: Path) -> None:
    """Forward-compat: a future field landing in the file shouldn't
    show up here as an unfiltered top-level key."""
    (context_dir / "sid.config.json").write_text(json.dumps({
        "provider": "qwen",
        "future_field": "ignored",
    }))
    cfg = load_session_config("sid")
    assert "future_field" not in cfg
    assert cfg["provider"] == "qwen"


def test_load_handles_corrupted_json(context_dir: Path) -> None:
    """A botched write should fall back to defaults, not 500 the chat route."""
    (context_dir / "sid.config.json").write_text("{ not valid json")
    cfg = load_session_config("sid")
    assert cfg == dict(_DEFAULTS)


# ---------------------------------------------------------------------------
# Save


def test_save_round_trips_provider_and_harness_model(context_dir: Path) -> None:
    """Basic write + reload."""
    save_session_config("sid", {"provider": "qwen", "harness_model": "deepseek-v4-pro"})
    cfg = load_session_config("sid")
    assert cfg["provider"] == "qwen"
    assert cfg["harness_model"] == "deepseek-v4-pro"


def test_save_is_a_shallow_merge(context_dir: Path) -> None:
    """Saving one key shouldn't blow away the others."""
    save_session_config("sid", {"provider": "qwen", "working_directory": "/tmp/x"})
    save_session_config("sid", {"harness_model": "qwen3.6-plus"})
    cfg = load_session_config("sid")
    assert cfg["provider"] == "qwen"
    assert cfg["working_directory"] == "/tmp/x"
    assert cfg["harness_model"] == "qwen3.6-plus"


def test_save_strips_unknown_keys(context_dir: Path) -> None:
    """save_session_config only writes whitelisted keys.  An accidental
    'provder' typo from a caller shouldn't land in the file."""
    save_session_config("sid", {"provider": "qwen", "provder": "claude"})
    raw = json.loads((context_dir / "sid.config.json").read_text())
    assert raw.get("provider") == "qwen"
    assert "provder" not in raw


def test_save_empty_string_harness_model_is_meaningful(context_dir: Path) -> None:
    """Empty string is the explicit "CLI default for this session" signal,
    distinct from None ("inherit global default").  Both must round-trip
    without one being coerced to the other."""
    save_session_config("sid", {"harness_model": ""})
    cfg = load_session_config("sid")
    assert cfg["harness_model"] == ""

    save_session_config("sid", {"harness_model": None})
    cfg = load_session_config("sid")
    assert cfg["harness_model"] is None


def test_save_then_clear_provider_resets_to_inherit(context_dir: Path) -> None:
    """Setting provider back to None should be how the UI's "Reset" button
    relinquishes the pin and falls back to the global default."""
    save_session_config("sid", {"provider": "qwen"})
    assert load_session_config("sid")["provider"] == "qwen"

    save_session_config("sid", {"provider": None})
    assert load_session_config("sid")["provider"] is None
