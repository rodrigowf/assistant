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


class VoiceEntry(TypedDict):
    id: str             # exact value the provider's session.update accepts
    label: str          # human label for the dropdown
    description: str    # optional one-line hint (gender, accent, etc.)


class TranscriptionLanguageEntry(TypedDict):
    id: str             # value the provider's transcription expects
                        # (e.g. "en", "pt"); empty string means auto-detect
    label: str          # human label for the dropdown
    description: str    # optional hint


class VoiceModelEntry(TypedDict):
    id: str
    label: str
    voice: str                # default speaker for this model
    voices: list[VoiceEntry]  # all selectable speakers for this model
    transcription_languages: list[TranscriptionLanguageEntry]
    default_transcription_language: str  # "" means auto-detect
    default: bool             # one entry per provider should be marked default


# ----------------------------------------------------------------------------
# Provider registry
# ----------------------------------------------------------------------------

VOICE_PROVIDERS: dict[str, type[BaseVoiceProvider]] = {
    "openai": OpenAIVoiceProvider,
    "qwen": QwenVoiceProvider,
    # "google": GoogleVoiceProvider,   # added in Phase 2
}


# OpenAI Realtime voices — full list verified at
# developers.openai.com/api/docs/guides/realtime-conversations (May 2026).
# `cedar` and `marin` are Realtime-API exclusives; OpenAI recommends them
# for best quality. All 10 voices are available on both gpt-realtime
# and gpt-realtime-mini.
_OPENAI_VOICES: list[VoiceEntry] = [
    {"id": "cedar",   "label": "Cedar",   "description": "Realtime-exclusive, recommended"},
    {"id": "marin",   "label": "Marin",   "description": "Realtime-exclusive, recommended"},
    {"id": "alloy",   "label": "Alloy",   "description": "Neutral, balanced"},
    {"id": "ash",     "label": "Ash",     "description": "Expressive"},
    {"id": "ballad",  "label": "Ballad",  "description": "Mellow"},
    {"id": "coral",   "label": "Coral",   "description": "Warm female"},
    {"id": "echo",    "label": "Echo",    "description": "Masculine, legacy"},
    {"id": "sage",    "label": "Sage",    "description": "Calm"},
    {"id": "shimmer", "label": "Shimmer", "description": "Feminine, legacy"},
    {"id": "verse",   "label": "Verse",   "description": "Expressive storyteller"},
]

# Qwen3.5-Omni-Plus voices — full preset list from
# www.alibabacloud.com/help/en/model-studio/omni-voice-list (May 2026).
# 47 multilingual presets + 7 Chinese-dialect presets. Voice-cloning is
# also supported on Plus but uses a separate API path; only presets here.
_QWEN_PLUS_VOICES: list[VoiceEntry] = [
    {"id": "Tina",        "label": "Tina",        "description": "Female, warm (default)"},
    {"id": "Cindy",       "label": "Cindy",       "description": "Female, Taiwanese-accented young woman"},
    {"id": "Liora Mira",  "label": "Liora Mira",  "description": "Female, gentle"},
    {"id": "Sunnybobi",   "label": "Sunnybobi",   "description": "Female, cheerful"},
    {"id": "Raymond",     "label": "Raymond",     "description": "Male, clear-voiced"},
    {"id": "Ethan",       "label": "Ethan",       "description": "Male, standard Mandarin (northern)"},
    {"id": "Theo Calm",   "label": "Theo Calm",   "description": "Male, healing tone"},
    {"id": "Serena",      "label": "Serena",      "description": "Female, gentle young woman"},
    {"id": "Harvey",      "label": "Harvey",      "description": "Male, deep and mellow"},
    {"id": "Maia",        "label": "Maia",        "description": "Female, intellectual + gentle"},
    {"id": "Evan",        "label": "Evan",        "description": "Male, youthful"},
    {"id": "Qiao",        "label": "Qiao",        "description": "Female, Taiwanese-accented, cute"},
    {"id": "Momo",        "label": "Momo",        "description": "Female, playful"},
    {"id": "Wil",         "label": "Wil",         "description": "Male, HK/Taiwan accent"},
    {"id": "Angel",       "label": "Angel",       "description": "Female, slightly Taiwanese-accented"},
    {"id": "Li Cassian",  "label": "Li Cassian",  "description": "Male, restrained"},
    {"id": "Mia",         "label": "Mia",         "description": "Female, lifestyle/aesthetic"},
    {"id": "Joyner",      "label": "Joyner",      "description": "Male, exaggerated/funny"},
    {"id": "Gold",        "label": "Gold",        "description": "Male, West-Coast rapper style"},
    {"id": "Katerina",    "label": "Katerina",    "description": "Female, mature/commanding"},
    {"id": "Ryan",        "label": "Ryan",        "description": "Male, high-energy dramatic"},
    {"id": "Jennifer",    "label": "Jennifer",    "description": "Female, premium American"},
    {"id": "Aiden",       "label": "Aiden",       "description": "Male, American young man"},
    {"id": "Mione",       "label": "Mione",       "description": "Female, mature British"},
    {"id": "Sohee",       "label": "Sohee",       "description": "Female, warm Korean"},
    {"id": "Lenn",        "label": "Lenn",        "description": "Male, German youth"},
    {"id": "Ono Anna",    "label": "Ono Anna",    "description": "Female, playful childhood-friend"},
    {"id": "Sonrisa",     "label": "Sonrisa",     "description": "Female, warm Latin-American"},
    {"id": "Bodega",      "label": "Bodega",      "description": "Male, warm Spanish"},
    {"id": "Emilien",     "label": "Emilien",     "description": "Male, romantic French"},
    {"id": "Andre",       "label": "Andre",       "description": "Male, steady magnetic"},
    {"id": "Radio Gol",   "label": "Radio Gol",   "description": "Male, sports commentator"},
    {"id": "Alek",        "label": "Alek",        "description": "Male, Russian-inspired warmth"},
    {"id": "Rizky",       "label": "Rizky",       "description": "Male, young Indonesian"},
    {"id": "Roya",        "label": "Roya",        "description": "Female, sporty free-spirited"},
    {"id": "Arda",        "label": "Arda",        "description": "Clean, crisp tone"},
    {"id": "Hana",        "label": "Hana",        "description": "Female, mature Vietnamese"},
    {"id": "Dolce",       "label": "Dolce",       "description": "Male, laid-back Italian"},
    {"id": "Jakub",       "label": "Jakub",       "description": "Male, charismatic Polish"},
    {"id": "Griet",       "label": "Griet",       "description": "Female, mature Dutch"},
    {"id": "Eliška",      "label": "Eliška",      "description": "Female, Central European"},
    {"id": "Marina",      "label": "Marina",      "description": "Female, multicultural"},
    {"id": "Siiri",       "label": "Siiri",       "description": "Female, reserved Finnish"},
    {"id": "Ingrid",      "label": "Ingrid",      "description": "Female, rural Norwegian"},
    {"id": "Sigga",       "label": "Sigga",       "description": "Female, Icelandic youth"},
    {"id": "Bea",         "label": "Bea",         "description": "Female, sweet Filipino"},
    {"id": "Chloe",       "label": "Chloe",       "description": "Female, Malaysian office worker"},
    # Chinese-dialect presets:
    {"id": "Sunny",       "label": "Sunny",       "description": "Female, Sichuan dialect"},
    {"id": "Dylan",       "label": "Dylan",       "description": "Male, Beijing dialect"},
    {"id": "Eric",        "label": "Eric",        "description": "Male, Sichuan dialect"},
    {"id": "Peter",       "label": "Peter",       "description": "Male, Tianjin dialect"},
    {"id": "Joseph Chen", "label": "Joseph Chen", "description": "Male, Hokkien dialect"},
    {"id": "Marcus",      "label": "Marcus",      "description": "Male, Shaanxi dialect"},
    {"id": "Li",          "label": "Li",          "description": "Male, Nanjing dialect"},
    {"id": "Rocky",       "label": "Rocky",       "description": "Male, Cantonese, witty"},
]

# Qwen3-Omni-Flash voices — 49 presets per DashScope omni-voice-list.
_QWEN_FLASH_VOICES: list[VoiceEntry] = [
    {"id": "Cherry",      "label": "Cherry",      "description": "Female, sunny (default)"},
    {"id": "Serena",      "label": "Serena",      "description": "Female, gentle"},
    {"id": "Ethan",       "label": "Ethan",       "description": "Male, standard Mandarin"},
    {"id": "Chelsie",     "label": "Chelsie",     "description": "Female, virtual-girlfriend"},
    {"id": "Momo",        "label": "Momo",        "description": "Female, playful"},
    {"id": "Vivian",      "label": "Vivian",      "description": "Female"},
    {"id": "Moon",        "label": "Moon",        "description": "Female"},
    {"id": "Maia",        "label": "Maia",        "description": "Female, intellectual"},
    {"id": "Kai",         "label": "Kai",         "description": "Male"},
    {"id": "Nofish",      "label": "Nofish",      "description": "Male"},
    {"id": "Bella",       "label": "Bella",       "description": "Female"},
    {"id": "Jennifer",    "label": "Jennifer",    "description": "Female, American"},
    {"id": "Ryan",        "label": "Ryan",        "description": "Male, dramatic"},
    {"id": "Katerina",    "label": "Katerina",    "description": "Female, mature"},
    {"id": "Aiden",       "label": "Aiden",       "description": "Male, American"},
    {"id": "Eldric Sage", "label": "Eldric Sage", "description": "Male"},
    {"id": "Mia",         "label": "Mia",         "description": "Female"},
    {"id": "Mochi",       "label": "Mochi",       "description": "Female"},
    {"id": "Bellona",     "label": "Bellona",     "description": "Female"},
    {"id": "Vincent",     "label": "Vincent",     "description": "Male"},
    {"id": "Bunny",       "label": "Bunny",       "description": "Female"},
    {"id": "Neil",        "label": "Neil",        "description": "Male"},
    {"id": "Elias",       "label": "Elias",       "description": "Male"},
    {"id": "Arthur",      "label": "Arthur",      "description": "Male"},
    {"id": "Nini",        "label": "Nini",        "description": "Female"},
    {"id": "Ebona",       "label": "Ebona",       "description": "Female"},
    {"id": "Seren",       "label": "Seren",       "description": "Female"},
    {"id": "Pip",         "label": "Pip",         "description": "Female"},
    {"id": "Stella",      "label": "Stella",      "description": "Female"},
    {"id": "Bodega",      "label": "Bodega",      "description": "Male, Spanish"},
    {"id": "Sonrisa",     "label": "Sonrisa",     "description": "Female, Latin-American"},
    {"id": "Alek",        "label": "Alek",        "description": "Male, Russian-inspired"},
    {"id": "Dolce",       "label": "Dolce",       "description": "Male, Italian"},
    {"id": "Sohee",       "label": "Sohee",       "description": "Female, Korean"},
    {"id": "Ono Anna",    "label": "Ono Anna",    "description": "Female"},
    {"id": "Lenn",        "label": "Lenn",        "description": "Male, German"},
    {"id": "Emilien",     "label": "Emilien",     "description": "Male, French"},
    {"id": "Andre",       "label": "Andre",       "description": "Male, magnetic"},
    {"id": "Radio Gol",   "label": "Radio Gol",   "description": "Male, sports commentator"},
    {"id": "Jada",        "label": "Jada",        "description": "Female"},
    # Chinese-dialect presets:
    {"id": "Dylan",       "label": "Dylan",       "description": "Male, Beijing dialect"},
    {"id": "Li",          "label": "Li",          "description": "Male, Nanjing dialect"},
    {"id": "Marcus",      "label": "Marcus",      "description": "Male, Shaanxi dialect"},
    {"id": "Roy",         "label": "Roy",         "description": "Male, dialect"},
    {"id": "Peter",       "label": "Peter",       "description": "Male, Tianjin dialect"},
    {"id": "Sunny",       "label": "Sunny",       "description": "Female, Sichuan dialect"},
    {"id": "Eric",        "label": "Eric",        "description": "Male, Sichuan dialect"},
    {"id": "Rocky",       "label": "Rocky",       "description": "Male, Cantonese"},
    {"id": "Kiki",        "label": "Kiki",        "description": "Female, dialect"},
]


# Qwen3-ASR-Flash language hints — curated subset of the full list at
# https://github.com/QwenLM/Qwen3-ASR (~30 languages supported).
# Empty string = auto-detect (no `language` field sent).
_QWEN_ASR_LANGUAGES: list[TranscriptionLanguageEntry] = [
    {"id": "",   "label": "Auto-detect", "description": "Let the ASR pick (best for multilingual)"},
    {"id": "en", "label": "English",     "description": ""},
    {"id": "pt", "label": "Portuguese",  "description": "Includes Brazilian"},
    {"id": "es", "label": "Spanish",     "description": ""},
    {"id": "zh", "label": "Chinese",     "description": "Mandarin"},
    {"id": "yue","label": "Cantonese",   "description": ""},
    {"id": "ja", "label": "Japanese",    "description": ""},
    {"id": "ko", "label": "Korean",      "description": ""},
    {"id": "fr", "label": "French",      "description": ""},
    {"id": "de", "label": "German",      "description": ""},
    {"id": "it", "label": "Italian",     "description": ""},
    {"id": "ru", "label": "Russian",     "description": ""},
    {"id": "ar", "label": "Arabic",      "description": ""},
    {"id": "hi", "label": "Hindi",       "description": ""},
    {"id": "id", "label": "Indonesian",  "description": ""},
    {"id": "ms", "label": "Malay",       "description": ""},
    {"id": "vi", "label": "Vietnamese",  "description": ""},
    {"id": "th", "label": "Thai",        "description": ""},
    {"id": "tr", "label": "Turkish",     "description": ""},
    {"id": "nl", "label": "Dutch",       "description": ""},
    {"id": "pl", "label": "Polish",      "description": ""},
]

# OpenAI Realtime input transcription has its own language dropdown
# (whisper-1 + gpt-4o-transcribe both honour `language` ISO codes), but
# we don't yet expose it in the UI for OpenAI sessions.  Empty list
# means the dropdown will be hidden when an OpenAI model is selected.
_OPENAI_TRANSCRIPTION_LANGUAGES: list[TranscriptionLanguageEntry] = []


VOICE_MODELS: dict[str, list[VoiceModelEntry]] = {
    "openai": [
        {"id": "gpt-realtime",      "label": "GPT Realtime",      "voice": "cedar",
         "voices": _OPENAI_VOICES,
         "transcription_languages": _OPENAI_TRANSCRIPTION_LANGUAGES,
         "default_transcription_language": "",
         "default": True},
        {"id": "gpt-realtime-mini", "label": "GPT Realtime Mini", "voice": "cedar",
         "voices": _OPENAI_VOICES,
         "transcription_languages": _OPENAI_TRANSCRIPTION_LANGUAGES,
         "default_transcription_language": "",
         "default": False},
    ],
    "qwen": [
        {"id": "qwen3.5-omni-plus-realtime", "label": "Qwen3.5-Omni Plus",
         "voice": "Tina", "voices": _QWEN_PLUS_VOICES,
         "transcription_languages": _QWEN_ASR_LANGUAGES,
         "default_transcription_language": "en",
         "default": True},
        {"id": "qwen3-omni-flash-realtime",  "label": "Qwen3-Omni Flash",
         "voice": "Cherry", "voices": _QWEN_FLASH_VOICES,
         "transcription_languages": _QWEN_ASR_LANGUAGES,
         "default_transcription_language": "en",
         "default": False},
    ],
    # Filled in by Phase 2:
    # "google": [
    #     {"id": "gemini-3.1-flash-live-preview", ...},
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
    voice: str | None = None,
    transcription_language: str | None = None,
) -> tuple[str, VoiceModelEntry, str, str]:
    """Resolve a (provider, model, voice, transcription_language) request.

    Returns ``(provider_id, model_entry, voice_id, language_id)``.

    - ``voice_id``: validated against the model's ``voices`` list; unknown
      voices fall back to the model's default voice.
    - ``language_id``: validated against the model's
      ``transcription_languages`` list. Empty string ``""`` means
      auto-detect (no ``language`` field sent to the provider).
      ``None`` (caller didn't specify) falls back to the model's
      ``default_transcription_language``. Unknown values also fall back.
    """
    p = provider or DEFAULT_VOICE_PROVIDER
    if p not in VOICE_PROVIDERS:
        p = DEFAULT_VOICE_PROVIDER

    entries = VOICE_MODELS.get(p, [])
    if not entries:
        raise RuntimeError(f"No models registered for voice provider {p!r}")

    selected: VoiceModelEntry | None = None
    if model:
        for entry in entries:
            if entry["id"] == model:
                selected = entry
                break
    if selected is None:
        for entry in entries:
            if entry.get("default"):
                selected = entry
                break
    if selected is None:
        selected = entries[0]

    # Resolve voice — accept any voice listed under the chosen model, else
    # fall back to the model's default.
    voices = selected.get("voices") or []
    voice_ids = {v["id"] for v in voices}
    chosen_voice = voice if voice and voice in voice_ids else selected["voice"]

    # Resolve transcription language. The valid set includes the
    # explicit "" entry (auto-detect) so we can distinguish "user picked
    # auto" from "user didn't choose anything".
    lang_options = selected.get("transcription_languages") or []
    valid_lang_ids = {entry["id"] for entry in lang_options}
    if transcription_language is None:
        chosen_language = selected.get("default_transcription_language", "")
    elif transcription_language in valid_lang_ids:
        chosen_language = transcription_language
    else:
        chosen_language = selected.get("default_transcription_language", "")

    return p, selected, chosen_voice, chosen_language


def list_voice_models() -> dict[str, list[VoiceModelEntry]]:
    """Return the full provider → models map (for the API endpoint)."""
    return {p: list(models) for p, models in VOICE_MODELS.items()}


def instantiate_provider(
    provider: str,
    model: str,
    voice: str | None = None,
    transcription_language: str | None = None,
) -> BaseVoiceProvider:
    """Construct a provider instance from a (provider, model) pair."""
    cls = get_provider_class(provider)
    entry = get_model_entry(provider, model)
    voices = entry.get("voices") or []
    voice_ids = {v["id"] for v in voices}
    final_voice = voice if voice and voice in voice_ids else entry["voice"]

    lang_ids = {e["id"] for e in (entry.get("transcription_languages") or [])}
    if transcription_language is None:
        final_lang = entry.get("default_transcription_language", "")
    elif transcription_language in lang_ids:
        final_lang = transcription_language
    else:
        final_lang = entry.get("default_transcription_language", "")

    return cls(model=model, voice=final_voice, transcription_language=final_lang)
