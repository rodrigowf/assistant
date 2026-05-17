"""Live model discovery for each provider.

Each provider exposes a list-models endpoint. We fetch them on demand,
cache for a short window, and fall back to the static registry on error
or when an API key is missing.

The endpoints return *raw* model IDs only — no capability metadata. We
classify each ID against capability patterns below, then merge with the
static `AVAILABLE_MODELS` / voice registry so anything we already know
about keeps its richer label and flags.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx

from orchestrator.config import (
    AVAILABLE_MODELS,
    ModelInfo,
    Provider,
)
from orchestrator.providers.voice_registry import (
    VOICE_MODELS,
    VoiceModelEntry,
    _OPENAI_VOICES,
    _QWEN_PLUS_VOICES,
    _QWEN_FLASH_VOICES,
    _QWEN_ASR_LANGUAGES,
    _OPENAI_TRANSCRIPTION_LANGUAGES,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600  # 10 minutes
HTTP_TIMEOUT_SECONDS = 8.0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    expires_at: float
    value: list[str]


_cache: dict[str, _CacheEntry] = {}
_cache_lock = asyncio.Lock()


def _cache_get(key: str) -> list[str] | None:
    entry = _cache.get(key)
    if entry is None or entry.expires_at < time.monotonic():
        return None
    return entry.value


def _cache_put(key: str, value: list[str]) -> None:
    _cache[key] = _CacheEntry(
        expires_at=time.monotonic() + CACHE_TTL_SECONDS,
        value=value,
    )


# ---------------------------------------------------------------------------
# Capability classification
# ---------------------------------------------------------------------------

# Anthropic: every modern Claude supports vision + tools.
_ANTHROPIC_TEXT_RE = re.compile(r"^claude-")

# OpenAI text-class models we want to expose in the orchestrator dropdown.
# We deliberately exclude image, embedding, tts, and dall-e families.
_OPENAI_TEXT_RE = re.compile(
    r"^(gpt-4|gpt-5|gpt-4o|chatgpt|o1|o3|o4)",
)
_OPENAI_AUDIO_INPUT_RE = re.compile(r"audio-preview|audio$|gpt-4o-audio")
_OPENAI_VISION_RE = re.compile(r"gpt-4o|gpt-4-turbo|gpt-4\.|gpt-5|chatgpt|o1|o3|o4")
_OPENAI_REALTIME_RE = re.compile(r"realtime")

_OPENAI_EXCLUDE_RE = re.compile(
    r"embedding|tts|whisper|dall-e|image|moderation|search|transcribe|computer-use|babbage|davinci|ada|curie",
)


def _humanize_openai(model_id: str) -> str:
    """Best-effort display name for an OpenAI model ID."""
    s = model_id.replace("gpt-", "GPT-").replace("chatgpt-", "ChatGPT ")
    s = s.replace("-", " ")
    return s


def _humanize_anthropic(model_id: str) -> str:
    """Best-effort display name for a Claude model ID like 'claude-opus-4-5-20251201'."""
    parts = model_id.split("-")
    # ['claude', 'opus', '4', '5', '20251201'] → 'Claude Opus 4.5'
    if len(parts) >= 4 and parts[0] == "claude":
        family = parts[1].capitalize()
        major = parts[2]
        # Skip a date suffix at the end
        version_parts = []
        for p in parts[3:]:
            if p.isdigit() and len(p) == 8:
                break
            version_parts.append(p)
        version = ".".join([major, *version_parts]) if version_parts else major
        return f"Claude {family} {version}"
    return model_id


def _classify_openai(model_id: str) -> ModelInfo:
    return ModelInfo(
        provider=Provider.OPENAI,
        model_id=model_id,
        display_name=_humanize_openai(model_id),
        supports_audio=bool(_OPENAI_AUDIO_INPUT_RE.search(model_id)),
        supports_vision=bool(_OPENAI_VISION_RE.search(model_id)),
        supports_tools=True,
        max_tokens=16384,
    )


def _classify_anthropic(model_id: str) -> ModelInfo:
    return ModelInfo(
        provider=Provider.ANTHROPIC,
        model_id=model_id,
        display_name=_humanize_anthropic(model_id),
        supports_audio=False,
        supports_vision=True,
        supports_tools=True,
        max_tokens=8192,
    )


# ---------------------------------------------------------------------------
# Fetchers — text/orchestrator models
# ---------------------------------------------------------------------------

async def _fetch_anthropic_models() -> list[str]:
    """GET https://api.anthropic.com/v1/models — returns model IDs."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []
    cached = _cache_get("anthropic")
    if cached is not None:
        return cached
    async with _cache_lock:
        cached = _cache_get("anthropic")
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    params={"limit": 1000},
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                ids = [m["id"] for m in data if _ANTHROPIC_TEXT_RE.match(m.get("id", ""))]
        except Exception as e:
            logger.warning("Anthropic model list fetch failed: %s", e)
            return []
        _cache_put("anthropic", ids)
        return ids


async def _fetch_openai_models() -> list[str]:
    """GET https://api.openai.com/v1/models — returns text-capable model IDs."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return []
    cached = _cache_get("openai")
    if cached is not None:
        return cached
    async with _cache_lock:
        cached = _cache_get("openai")
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                ids = [
                    m["id"] for m in data
                    if _OPENAI_TEXT_RE.match(m.get("id", ""))
                    and not _OPENAI_EXCLUDE_RE.search(m.get("id", ""))
                    and not _OPENAI_REALTIME_RE.search(m.get("id", ""))
                ]
        except Exception as e:
            logger.warning("OpenAI model list fetch failed: %s", e)
            return []
        _cache_put("openai", ids)
        return ids


async def _fetch_openai_realtime_models() -> list[str]:
    """OpenAI realtime model IDs from the /v1/models response."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return []
    cached = _cache_get("openai_realtime")
    if cached is not None:
        return cached
    async with _cache_lock:
        cached = _cache_get("openai_realtime")
        if cached is not None:
            return cached
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                ids = [
                    m["id"] for m in data
                    if _OPENAI_REALTIME_RE.search(m.get("id", ""))
                    and not _OPENAI_EXCLUDE_RE.search(m.get("id", ""))
                ]
        except Exception as e:
            logger.warning("OpenAI realtime model list fetch failed: %s", e)
            return []
        _cache_put("openai_realtime", ids)
        return ids


async def _fetch_qwen_models() -> list[str]:
    """DashScope does not expose a public list-models endpoint usable with
    a bearer token (it requires the OpenAPI control-plane signature flow).
    Return an empty list so the caller falls back to the static registry.
    """
    return []


# ---------------------------------------------------------------------------
# Public API — text / orchestrator models
# ---------------------------------------------------------------------------

async def list_orchestrator_models() -> list[ModelInfo]:
    """Return live + static merged orchestrator models.

    Strategy: prefer live IDs from each provider; for any ID that already
    exists in the static `AVAILABLE_MODELS` registry, keep the curated
    metadata. Anything not classified (e.g. OPENAI_API_KEY missing) falls
    back to the static entries so the dropdown is never empty.
    """
    anthropic_ids, openai_ids = await asyncio.gather(
        _fetch_anthropic_models(),
        _fetch_openai_models(),
    )

    by_id: dict[str, ModelInfo] = {}

    for mid in anthropic_ids:
        by_id[mid] = AVAILABLE_MODELS.get(mid) or _classify_anthropic(mid)
    for mid in openai_ids:
        by_id[mid] = AVAILABLE_MODELS.get(mid) or _classify_openai(mid)

    # Fallback: include static models whose provider returned nothing.
    if not anthropic_ids:
        for mid, info in AVAILABLE_MODELS.items():
            if info.provider == Provider.ANTHROPIC:
                by_id.setdefault(mid, info)
    if not openai_ids:
        for mid, info in AVAILABLE_MODELS.items():
            if info.provider == Provider.OPENAI:
                by_id.setdefault(mid, info)

    # Sort: anthropic first, then openai; within a provider, newest-looking IDs
    # first (rough date-suffix heuristic, else alpha).
    def _sort_key(info: ModelInfo) -> tuple[int, str]:
        prov_order = 0 if info.provider == Provider.ANTHROPIC else 1
        # invert ID so newer date-suffixes (larger numbers) sort first
        return (prov_order, "".join(chr(255 - ord(c)) for c in info.model_id))

    return sorted(by_id.values(), key=_sort_key)


# ---------------------------------------------------------------------------
# Public API — voice models
# ---------------------------------------------------------------------------

def _static_voice_entry(provider: str, model_id: str) -> VoiceModelEntry | None:
    for entry in VOICE_MODELS.get(provider, []):
        if entry["id"] == model_id:
            return entry
    return None


def _make_openai_voice_entry(model_id: str, is_default: bool) -> VoiceModelEntry:
    """Build a voice model entry for an OpenAI realtime model not in the static list."""
    label = _humanize_openai(model_id)
    return {
        "id": model_id,
        "label": label,
        "voice": "cedar",
        "voices": list(_OPENAI_VOICES),
        "transcription_languages": list(_OPENAI_TRANSCRIPTION_LANGUAGES),
        "default_transcription_language": "",
        "default": is_default,
    }


def _make_qwen_voice_entry(model_id: str, is_default: bool) -> VoiceModelEntry:
    """Build a voice model entry for a Qwen omni-realtime model not in the static list."""
    is_flash = "flash" in model_id.lower() or "turbo" in model_id.lower()
    voices = list(_QWEN_FLASH_VOICES if is_flash else _QWEN_PLUS_VOICES)
    default_voice = "Cherry" if is_flash else "Tina"
    return {
        "id": model_id,
        "label": model_id,
        "voice": default_voice,
        "voices": voices,
        "transcription_languages": list(_QWEN_ASR_LANGUAGES),
        "default_transcription_language": "en",
        "default": is_default,
    }


async def list_voice_models_live() -> dict[str, list[VoiceModelEntry]]:
    """Return live + static merged voice models per provider."""
    realtime_ids = await _fetch_openai_realtime_models()

    result: dict[str, list[VoiceModelEntry]] = {}

    # OpenAI: merge live realtime IDs with static metadata.
    if realtime_ids:
        seen: set[str] = set()
        entries: list[VoiceModelEntry] = []
        for mid in realtime_ids:
            seen.add(mid)
            static = _static_voice_entry("openai", mid)
            if static is not None:
                entries.append(dict(static))  # copy
            else:
                entries.append(_make_openai_voice_entry(mid, is_default=False))
        # Keep any static entries the API didn't return (defensive — useful if
        # the API filters preview models for the key holder).
        for entry in VOICE_MODELS.get("openai", []):
            if entry["id"] not in seen:
                entries.append(dict(entry))
        # Ensure exactly one default
        if not any(e.get("default") for e in entries) and entries:
            entries[0]["default"] = True
        result["openai"] = entries
    else:
        result["openai"] = [dict(e) for e in VOICE_MODELS.get("openai", [])]

    # Qwen: no live endpoint — use static registry.
    result["qwen"] = [dict(e) for e in VOICE_MODELS.get("qwen", [])]

    return result
