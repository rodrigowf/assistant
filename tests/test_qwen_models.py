"""Tests for ``manager/qwen_models.py`` — discovery of the Qwen harness model
catalog from ``~/.qwen/settings.json``.

The discovery layer is intentionally permissive: malformed entries are
dropped silently, and a missing file returns an empty list (the dropdown
falls back to "CLI default").  These tests pin that behavior so a future
schema change in Qwen's settings file doesn't crash the Configuration UI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manager.qwen.models import QwenModelInfo, list_qwen_models


@pytest.fixture
def qwen_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``list_qwen_models`` at a temp QWEN_HOME we control."""
    monkeypatch.setenv("QWEN_HOME", str(tmp_path))
    return tmp_path


def _write_settings(qwen_home: Path, payload: dict) -> Path:
    """Write a settings.json fixture and return its path."""
    path = qwen_home / "settings.json"
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# Happy path


def test_parses_real_world_settings_layout(qwen_home: Path) -> None:
    """The real ``~/.qwen/settings.json`` shape: one provider, several models
    with mixed contextWindowSize, modalities, and extra_body flags."""
    _write_settings(qwen_home, {
        "modelProviders": {
            "openai": [
                {
                    "id": "qwen3.6-plus",
                    "name": "[ModelStudio Standard] qwen3.6-plus",
                    "baseUrl": "https://dashscope.example.com/v1",
                    "envKey": "DASHSCOPE_API_KEY",
                    "generationConfig": {
                        "extra_body": {"enable_thinking": True},
                        "contextWindowSize": 1_000_000,
                    },
                },
                {
                    "id": "deepseek-v4-pro",
                    "name": "[ModelStudio Standard] deepseek-v4-pro",
                    "baseUrl": "https://dashscope.example.com/v1",
                    "generationConfig": {
                        "extra_body": {"enable_thinking": True},
                        "contextWindowSize": 1_000_000,
                        "modalities": {"image": True, "video": True},
                    },
                },
                {
                    "id": "minimal-model",
                    # No name, baseUrl, or generationConfig
                },
            ],
        },
    })

    models = list_qwen_models()
    assert len(models) == 3

    by_id = {m.id: m for m in models}

    # Full-featured row carries every badge
    plus = by_id["qwen3.6-plus"]
    assert plus.display_name == "[ModelStudio Standard] qwen3.6-plus"
    assert plus.provider == "openai"
    assert plus.base_url == "https://dashscope.example.com/v1"
    assert plus.context_window == 1_000_000
    assert plus.supports_thinking is True
    assert plus.supports_vision is False
    assert plus.supports_video is False

    # Vision + video modalities propagate
    deepseek = by_id["deepseek-v4-pro"]
    assert deepseek.supports_vision is True
    assert deepseek.supports_video is True
    assert deepseek.supports_thinking is True

    # Minimal row: display_name falls back to id, all badges off
    minimal = by_id["minimal-model"]
    assert minimal.display_name == "minimal-model"
    assert minimal.base_url is None
    assert minimal.context_window is None
    assert minimal.supports_thinking is False


def test_multiple_providers_are_flattened(qwen_home: Path) -> None:
    """``modelProviders`` may have multiple keys (e.g. ``openai``, ``ollama``).
    All of them should be flattened into the output list with the provider
    key recorded so the UI can group or label by source."""
    _write_settings(qwen_home, {
        "modelProviders": {
            "openai": [{"id": "qwen3.6-plus", "name": "Qwen 3.6 Plus"}],
            "ollama": [
                {"id": "llama3:8b", "name": "Llama 3 8B (local)"},
                {"id": "mistral:7b", "name": "Mistral 7B (local)"},
            ],
        },
    })

    models = list_qwen_models()
    assert len(models) == 3
    providers = {m.provider for m in models}
    assert providers == {"openai", "ollama"}


def test_to_dict_round_trip(qwen_home: Path) -> None:
    """``QwenModelInfo.to_dict()`` is what crosses the API boundary — make sure
    every dataclass field is represented."""
    _write_settings(qwen_home, {
        "modelProviders": {
            "openai": [{
                "id": "test-id",
                "name": "Test Display",
                "baseUrl": "https://example.com/v1",
                "generationConfig": {
                    "contextWindowSize": 128_000,
                    "extra_body": {"enable_thinking": True},
                    "modalities": {"image": True},
                },
            }],
        },
    })

    [model] = list_qwen_models()
    d = model.to_dict()
    assert d == {
        "id": "test-id",
        "display_name": "Test Display",
        "provider": "openai",
        "base_url": "https://example.com/v1",
        "context_window": 128_000,
        "supports_vision": True,
        "supports_video": False,
        "supports_thinking": True,
    }


# ---------------------------------------------------------------------------
# Fallback / error tolerance


def test_missing_file_returns_empty_list(qwen_home: Path) -> None:
    """No settings.json yet (e.g. user hasn't run `qwen` once): empty list,
    no exception.  The UI shows "CLI default" + a hint."""
    assert list_qwen_models() == []


def test_unreadable_json_returns_empty_list(qwen_home: Path) -> None:
    """Corrupted settings file shouldn't blank the config page.  We log a
    warning and return an empty list."""
    (qwen_home / "settings.json").write_text("not valid json {{{")
    assert list_qwen_models() == []


def test_missing_model_providers_key(qwen_home: Path) -> None:
    """settings.json exists but doesn't define modelProviders."""
    _write_settings(qwen_home, {"env": {"DASHSCOPE_API_KEY": "sk-..."}})
    assert list_qwen_models() == []


def test_non_dict_model_providers_is_ignored(qwen_home: Path) -> None:
    """Schema drift: someone put a list where the object should be.
    Don't crash — return empty."""
    _write_settings(qwen_home, {"modelProviders": ["this", "is", "wrong"]})
    assert list_qwen_models() == []


def test_skips_malformed_entries_but_keeps_valid_ones(qwen_home: Path) -> None:
    """One bad row shouldn't poison the rest of the provider's catalog."""
    _write_settings(qwen_home, {
        "modelProviders": {
            "openai": [
                {"id": "good-1", "name": "Good 1"},
                {},  # missing id
                {"id": ""},  # empty id
                {"name": "no-id"},
                "not-even-a-dict",
                {"id": "good-2", "name": "Good 2"},
            ],
        },
    })

    ids = [m.id for m in list_qwen_models()]
    assert ids == ["good-1", "good-2"]


def test_non_list_provider_entry_is_skipped(qwen_home: Path) -> None:
    """If one provider's entries aren't a list, skip just that provider —
    keep entries from siblings that *are* well-formed."""
    _write_settings(qwen_home, {
        "modelProviders": {
            "openai": [{"id": "good", "name": "Good"}],
            "bogus": "should-be-a-list",
        },
    })

    [model] = list_qwen_models()
    assert model.id == "good"


def test_non_integer_context_window_is_dropped(qwen_home: Path) -> None:
    """``contextWindowSize`` is documented as an int.  If someone writes a
    string there, drop it rather than carrying through a wrong type."""
    _write_settings(qwen_home, {
        "modelProviders": {
            "openai": [{
                "id": "weird-ctx",
                "generationConfig": {"contextWindowSize": "1M"},
            }],
        },
    })

    [model] = list_qwen_models()
    assert model.context_window is None


# ---------------------------------------------------------------------------
# Dataclass behavior


def test_qwen_model_info_is_immutable() -> None:
    """frozen=True: prevents accidental in-place mutation between read and
    JSON-serialize."""
    model = QwenModelInfo(id="x", display_name="X", provider="openai")
    with pytest.raises(Exception):  # FrozenInstanceError
        model.id = "y"  # type: ignore[misc]
