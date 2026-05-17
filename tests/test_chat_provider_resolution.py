"""Tests for ``api.routes.chat._resolve_session_provider``.

The resolution function picks which provider + harness model a chat
session should run with, given per-session config, global config, and
optionally a JSONL on disk to sniff.  The precedence is:

  provider:  session_cfg > detected from JSONL > global config
  model:     session_cfg > global harness_model[provider]

And we want to persist a *detected* provider back to the session config
so subsequent resumes are deterministic (the JSONL might get truncated
or the global default might flip).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.routes.chat import _resolve_session_provider


@pytest.fixture
def context_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the JSONL lookup at temp dirs so we can plant fixtures."""
    context_dir = tmp_path / "context"
    chats_dir = context_dir / "chats"
    context_dir.mkdir()
    chats_dir.mkdir()
    import utils.paths
    monkeypatch.setattr(utils.paths, "get_context_dir", lambda: context_dir)
    monkeypatch.setattr(utils.paths, "get_chats_dir", lambda: chats_dir)
    return {"context": context_dir, "chats": chats_dir}


def _write_claude_jsonl(path: Path) -> None:
    """A minimal JSONL that Claude's adapter will recognize.

    detect_provider() reads the first line of the file and checks if it
    has the shape one of the adapters expects.  Claude's lines have
    ``type: "user" | "assistant"`` with a ``message`` object containing
    a ``content`` field (list of blocks).
    """
    path.write_text(json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        "session_id": path.stem,
    }) + "\n")


def _write_qwen_jsonl(path: Path) -> None:
    """A minimal JSONL that Qwen's adapter will recognize.

    Qwen's detector keys off ``message.parts`` (the user's role still says
    "user" but the shape is ``parts: [{text}, ...]`` rather than Claude's
    ``content: [{type: text, text}]``).  See manager/qwen_adapter.py.
    """
    path.write_text(json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "parts": [{"text": "hi"}],
        },
        "sessionId": path.stem,
    }) + "\n")


# ---------------------------------------------------------------------------
# Provider precedence


def test_session_provider_wins_over_everything(context_dirs) -> None:
    """If session_cfg has a pinned provider, it wins — even when global
    says otherwise and even if a JSONL on disk would sniff the other way."""
    _write_claude_jsonl(context_dirs["context"] / "sid.jsonl")
    provider, _, persist = _resolve_session_provider(
        resume_sdk_id="sid",
        session_cfg={"provider": "qwen"},
        assistant_cfg={"provider": "claude"},
    )
    assert provider == "qwen"
    # Already pinned per-session → no need to persist anything.
    assert persist is None


def test_detected_provider_wins_over_global(context_dirs) -> None:
    """No per-session pin yet, but the JSONL is sniffable → use the
    detected provider so we don't switch CLIs behind an existing file."""
    _write_qwen_jsonl(context_dirs["chats"] / "sid.jsonl")
    provider, _, persist = _resolve_session_provider(
        resume_sdk_id="sid",
        session_cfg={},
        assistant_cfg={"provider": "claude"},
    )
    assert provider == "qwen"
    # Detected → caller should persist so we don't re-sniff next time.
    assert persist == "qwen"


def test_global_fallback_when_no_session_and_no_jsonl(context_dirs) -> None:
    """Fresh session, no resume, no file on disk: global default wins."""
    provider, _, persist = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={},
        assistant_cfg={"provider": "qwen"},
    )
    assert provider == "qwen"
    # Global fallback doesn't get persisted — we only persist detected
    # providers, since the global default might change later.
    assert persist is None


def test_resume_with_missing_jsonl_uses_global(context_dirs) -> None:
    """resume_sdk_id is set but no file exists at either candidate path:
    falls through to the global default."""
    provider, _, persist = _resolve_session_provider(
        resume_sdk_id="ghost-session",
        session_cfg={},
        assistant_cfg={"provider": "claude"},
    )
    assert provider == "claude"
    assert persist is None


def test_provider_is_lowercased(context_dirs) -> None:
    """Whatever path resolved the provider, ManagerConfig expects lowercase."""
    provider, _, _ = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={"provider": "QWEN"},
        assistant_cfg={},
    )
    assert provider == "qwen"


def test_no_decision_returns_none(context_dirs) -> None:
    """Nothing in session, nothing in global, no resume: caller should
    leave ManagerConfig at whatever the .manager.json baseline is."""
    provider, model, persist = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={},
        assistant_cfg={},
    )
    assert provider is None
    assert model is None
    assert persist is None


# ---------------------------------------------------------------------------
# Harness model precedence


def test_session_model_wins_over_global(context_dirs) -> None:
    """Per-session pin beats the global map."""
    _, model, _ = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={"provider": "qwen", "harness_model": "deepseek-v4-pro"},
        assistant_cfg={"provider": "qwen", "harness_model": {"qwen": "qwen3.6-plus"}},
    )
    assert model == "deepseek-v4-pro"


def test_session_empty_string_means_cli_default(context_dirs) -> None:
    """Empty string is the explicit "CLI default for this session" pin.
    Translates to None at the API boundary so we omit --model entirely
    (instead of passing --model "", which would crash the CLI)."""
    _, model, _ = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={"provider": "qwen", "harness_model": ""},
        assistant_cfg={"provider": "qwen", "harness_model": {"qwen": "qwen3.6-plus"}},
    )
    assert model is None


def test_global_model_used_when_session_inherits(context_dirs) -> None:
    """session_cfg.harness_model == None → fall back to global[provider]."""
    _, model, _ = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={},
        assistant_cfg={"provider": "qwen", "harness_model": {"qwen": "qwen3.6-plus"}},
    )
    assert model == "qwen3.6-plus"


def test_global_model_for_different_provider_does_not_leak(context_dirs) -> None:
    """The global map is per-provider — Claude sessions shouldn't pick up
    a Qwen model id even if it's the only key present."""
    _, model, _ = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={},
        assistant_cfg={"provider": "claude", "harness_model": {"qwen": "qwen3.6-plus"}},
    )
    assert model is None


def test_global_empty_string_is_normalized_to_none(context_dirs) -> None:
    """Whitespace / empty-string entries in the global map should be treated
    as "no override," not passed through as a literal --model ''.  This is
    what the seed in _default_config() actually writes."""
    _, model, _ = _resolve_session_provider(
        resume_sdk_id=None,
        session_cfg={},
        assistant_cfg={"provider": "qwen", "harness_model": {"qwen": "   "}},
    )
    assert model is None
