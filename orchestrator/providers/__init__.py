"""Model providers for the orchestrator agent.

Providers implement the ModelProvider protocol and translate between
the orchestrator's event system and various model APIs.

Available providers:
- AnthropicProvider: Claude models via Anthropic API
- OpenAITextProvider: GPT-4 family with multimodal (audio) support
- OpenAIVoiceProvider: OpenAI Realtime API for WebRTC voice

Lazy-loading note
-----------------
Each provider's underlying SDK (``anthropic``, ``openai``) is optional —
a Qwen-only deployment can run with neither installed.  We resolve the
classes via PEP 562 ``__getattr__`` so ``from orchestrator.providers
import AnthropicProvider`` triggers the SDK import only on first
access.  ``is_provider_available(name)`` lets call sites check whether
a backend can be loaded without provoking an ``ImportError``.
"""

from __future__ import annotations

from importlib import import_module


# name → (module_path, attribute_name)
_LAZY: dict[str, tuple[str, str]] = {
    "AnthropicProvider": ("orchestrator.providers.anthropic", "AnthropicProvider"),
    "OpenAIModel": ("orchestrator.providers.openai_text", "OpenAIModel"),
    "OpenAITextProvider": ("orchestrator.providers.openai_text", "OpenAITextProvider"),
    "OpenAIVoiceProvider": ("orchestrator.providers.openai_voice", "OpenAIVoiceProvider"),
    "AudioContent": ("orchestrator.providers.openai_text", "AudioContent"),
    "create_audio_message": ("orchestrator.providers.openai_text", "create_audio_message"),
}

# Underlying SDK each provider needs. Used by is_provider_available().
_PROVIDER_SDKS: dict[str, str] = {
    "AnthropicProvider": "anthropic",
    "OpenAITextProvider": "openai",
    "OpenAIVoiceProvider": "openai",
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache so future accesses are O(1)
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


def is_provider_available(name: str) -> bool:
    """Return True if the named provider class can be imported in this venv.

    A False result usually means the underlying SDK (``anthropic`` for
    Claude, ``openai`` for GPT/Qwen/Gemini) isn't installed.  Callers that
    enumerate available models should consult this first so they don't
    surface providers the user can't actually use.
    """
    sdk = _PROVIDER_SDKS.get(name)
    if sdk is None:
        return False
    try:
        import_module(sdk)
        return True
    except ImportError:
        return False


__all__ = [
    "AnthropicProvider",
    "OpenAIModel",
    "OpenAITextProvider",
    "OpenAIVoiceProvider",
    "AudioContent",
    "create_audio_message",
    "is_provider_available",
]
