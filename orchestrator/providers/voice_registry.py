"""Registry of available voice providers and their models.

Single source of truth for the voice multi-provider system:

- :data:`VOICE_PROVIDERS` maps provider id → :class:`BaseVoiceProvider` class
- :data:`VOICE_MODELS` maps provider id → list of selectable model entries
- :data:`DEFAULT_VOICE_PROVIDER` / :data:`DEFAULT_VOICE_MODEL` are the
  fallbacks used when nothing is configured

Adding a future provider (e.g. a self-hosted realtime model) requires only:
1. Implement :class:`BaseVoiceProvider` in a new module
2. Register it in :data:`VOICE_PROVIDERS` and :data:`VOICE_MODELS`
3. Add a frontend adapter in ``frontend/src/voice/providers/<name>.ts``
"""

from __future__ import annotations

from typing import TypedDict

from orchestrator.providers.openai_voice import OpenAIVoiceProvider
from orchestrator.providers.qwen_voice import QwenVoiceProvider
from orchestrator.providers.voice_base import BaseVoiceProvider


class VoiceModelEntry(TypedDict):
    id: str
    label: str
    voice: str          # default speaker for this model
    default: bool       # one entry per provider should be marked default


# ----------------------------------------------------------------------------
# Provider registry
# ----------------------------------------------------------------------------

VOICE_PROVIDERS: dict[str, type[BaseVoiceProvider]] = {
    "openai": OpenAIVoiceProvider,
    "qwen": QwenVoiceProvider,
    # "google": GoogleVoiceProvider,   # added in Phase 2
}


VOICE_MODELS: dict[str, list[VoiceModelEntry]] = {
    "openai": [
        {"id": "gpt-realtime", "label": "GPT Realtime", "voice": "cedar", "default": True},
        {"id": "gpt-realtime-mini", "label": "GPT Realtime Mini", "voice": "cedar", "default": False},
    ],
    "qwen": [
        {"id": "qwen3.5-omni-plus-realtime", "label": "Qwen3.5-Omni Plus",
         "voice": "Tina", "default": True},
        {"id": "qwen3-omni-flash-realtime", "label": "Qwen3-Omni Flash",
         "voice": "Cherry", "default": False},
    ],
    # Filled in by Phase 2:
    # "google": [
    #     {"id": "gemini-3.1-flash-live-preview", "label": "Gemini 3.1 Flash Live",
    #      "voice": "Aoede", "default": True},
    # ],
}


DEFAULT_VOICE_PROVIDER = "openai"
DEFAULT_VOICE_MODEL = "gpt-realtime"


# ----------------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------------

def get_provider_class(provider: str) -> type[BaseVoiceProvider]:
    """Return the provider class, raising ValueError on unknown id."""
    cls = VOICE_PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown voice provider: {provider!r}. "
            f"Available: {sorted(VOICE_PROVIDERS)}"
        )
    return cls


def get_model_entry(provider: str, model: str) -> VoiceModelEntry:
    """Return the registered model entry, raising ValueError on unknown id."""
    entries = VOICE_MODELS.get(provider, [])
    for entry in entries:
        if entry["id"] == model:
            return entry
    raise ValueError(
        f"Unknown model {model!r} for provider {provider!r}. "
        f"Available: {[e['id'] for e in entries]}"
    )


def resolve_voice_target(
    provider: str | None,
    model: str | None,
) -> tuple[str, VoiceModelEntry]:
    """Resolve a (provider, model) request, falling back to registry defaults.

    Returns the (provider_id, model_entry) tuple. The model entry contains
    the canonical id, label, and default voice for instantiating the provider.
    """
    p = provider or DEFAULT_VOICE_PROVIDER
    if p not in VOICE_PROVIDERS:
        p = DEFAULT_VOICE_PROVIDER

    entries = VOICE_MODELS.get(p, [])
    if not entries:
        raise RuntimeError(f"No models registered for voice provider {p!r}")

    if model:
        for entry in entries:
            if entry["id"] == model:
                return p, entry

    # No match — pick the entry flagged default, else the first one.
    for entry in entries:
        if entry.get("default"):
            return p, entry
    return p, entries[0]


def list_voice_models() -> dict[str, list[VoiceModelEntry]]:
    """Return the full provider → models map (for the API endpoint)."""
    return {p: list(models) for p, models in VOICE_MODELS.items()}


def instantiate_provider(provider: str, model: str, voice: str | None = None) -> BaseVoiceProvider:
    """Construct a provider instance from a (provider, model) pair."""
    cls = get_provider_class(provider)
    entry = get_model_entry(provider, model)
    return cls(model=model, voice=voice or entry["voice"])
