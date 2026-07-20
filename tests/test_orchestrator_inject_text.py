"""Silent shared-text inject (``build_silent_text_inject`` + provider
``format_text_input``).

When a file link / text is shared into a session mid-voice-call, it must be
added to the live conversation as a user turn WITHOUT a ``response.create`` —
the model folds it into context instead of being interrupted to speak. These
tests pin:

1. ``format_text_input`` produces a ``conversation.item.create`` message item
   (OpenAI/Qwen shape) with NO ``response.create`` — contrast
   ``format_tool_result`` which does include one.
2. The Gemini override uses ``clientContent`` with ``turnComplete: False``.
3. ``OrchestratorSession.build_silent_text_inject`` persists a user JSONL turn
   tagged ``shared_inject`` and returns the provider commands (empty when no
   provider is attached, but the turn is still written).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator.session import OrchestratorSession


# ---------- 1. OpenAI/Qwen default shape ------------------------------------

def test_format_text_input_openai_shape_has_no_response_create() -> None:
    from orchestrator.providers.openai_voice import OpenAIVoiceProvider

    prov = OpenAIVoiceProvider.__new__(OpenAIVoiceProvider)
    cmds = prov.format_text_input("hello context")

    assert len(cmds) == 1, cmds
    item = cmds[0]
    assert item["type"] == "conversation.item.create"
    assert item["item"]["type"] == "message"
    assert item["item"]["role"] == "user"
    assert item["item"]["content"] == [{"type": "input_text", "text": "hello context"}]
    # Silent — no response.create anywhere.
    assert all(c.get("type") != "response.create" for c in cmds)


def test_format_tool_result_still_triggers_response_create() -> None:
    """Guard: the silent-inject change must NOT have altered tool-result flow."""
    from orchestrator.providers.openai_voice import OpenAIVoiceProvider

    prov = OpenAIVoiceProvider.__new__(OpenAIVoiceProvider)
    cmds = prov.format_tool_result("call-1", "result")
    assert any(c.get("type") == "response.create" for c in cmds)


# ---------- 2. Gemini override ----------------------------------------------

def test_format_text_input_gemini_shape_turn_incomplete() -> None:
    from orchestrator.providers.gemini_voice import GeminiAIStudioBackend

    prov = GeminiAIStudioBackend.__new__(GeminiAIStudioBackend)
    cmds = prov.format_text_input("gemini context")

    assert len(cmds) == 1, cmds
    cc = cmds[0]["clientContent"]
    assert cc["turnComplete"] is False
    assert cc["turns"] == [{"role": "user", "parts": [{"text": "gemini context"}]}]


# ---------- 3. Session persist + command passthrough ------------------------

def _session() -> OrchestratorSession:
    config = MagicMock()
    config.summarizer_model = None
    context = {"pool": MagicMock(), "store": MagicMock()}
    return OrchestratorSession(config=config, context=context, local_id="t-inject")


def test_build_silent_text_inject_persists_turn_and_returns_provider_commands() -> None:
    s = _session()
    s._writer = MagicMock()
    provider = MagicMock()
    provider.format_text_input.return_value = [{"type": "conversation.item.create"}]
    s._voice_provider = provider

    cmds = s.build_silent_text_inject("shared link + metadata")

    # Persisted exactly one user turn tagged shared_inject.
    assert s._writer.append.call_count == 1
    entry = s._writer.append.call_args[0][0]
    assert entry["type"] == "user"
    assert entry["message"] == {"role": "user", "content": "shared link + metadata"}
    assert entry["source"] == "shared_inject"
    # Provider commands passed through.
    provider.format_text_input.assert_called_once_with("shared link + metadata")
    assert cmds == [{"type": "conversation.item.create"}]


def test_build_silent_text_inject_no_provider_still_persists() -> None:
    s = _session()
    s._writer = MagicMock()
    s._voice_provider = None

    cmds = s.build_silent_text_inject("orphan text")

    assert cmds == []
    assert s._writer.append.call_count == 1  # turn still written


def test_build_silent_text_inject_empty_text_is_noop() -> None:
    s = _session()
    s._writer = MagicMock()
    s._voice_provider = MagicMock()

    cmds = s.build_silent_text_inject("")

    assert cmds == []
    s._writer.append.assert_not_called()
