"""Global configuration endpoint — manages working directory, skills, and MCP defaults."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])

_CONFIG_FILE_NAME = "assistant_config.json"


def _get_config_path() -> Path:
    """Return path to the global config JSON."""
    return PROJECT_ROOT / _CONFIG_FILE_NAME


def _load_config() -> dict[str, Any]:
    path = _get_config_path()
    if not path.is_file():
        return _default_config()
    try:
        with open(path) as f:
            data = json.load(f)
        # Forward-compat: if default_voice_transcription_language was
        # never written, resolve it from the saved provider+model's
        # default rather than from the global registry default — those
        # differ between providers (Qwen → "en", OpenAI → "" auto).
        # We do this BEFORE the generic setdefault loop so the right
        # value lands.
        if "default_voice_transcription_language" not in data:
            try:
                from orchestrator.providers.voice_registry import resolve_voice_target
                _, _, _, lang = resolve_voice_target(
                    data.get("default_voice_provider"),
                    data.get("default_voice_model"),
                    data.get("default_voice_name"),
                    None,  # → use that model's default_transcription_language
                )
                data["default_voice_transcription_language"] = lang
            except Exception:
                pass
        # Ensure all expected keys exist (forward-compat)
        defaults = _default_config()
        for k, v in defaults.items():
            data.setdefault(k, v)
        # Migrate legacy string history to WorkingDirectoryEntry objects
        data["working_directory_history"] = _migrate_wd_history(data["working_directory_history"])
        # Migrate legacy string working_directory to an entry id
        data["working_directory"] = _migrate_wd_active(
            data["working_directory"], data["working_directory_history"]
        )
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to load assistant config: %s", e)
        return _default_config()


def _migrate_wd_history(history: list) -> list[dict]:
    """Convert any legacy plain-string entries to WorkingDirectoryEntry dicts."""
    result = []
    for item in history:
        if isinstance(item, str):
            result.append({"id": item, "path": item, "label": None, "ssh_host": None, "ssh_user": None, "ssh_key": None})
        elif isinstance(item, dict):
            item.setdefault("id", item.get("path", ""))
            item.setdefault("label", None)
            item.setdefault("ssh_host", None)
            item.setdefault("ssh_user", None)
            item.setdefault("ssh_key", None)
            item.setdefault("claude_config_dir", None)
            # Auto-derive claude_config_dir for SSH entries that don't have it set
            if item.get("ssh_host") and not item.get("claude_config_dir"):
                item["claude_config_dir"] = item["path"].rstrip("/") + "/.claude_config"
            result.append(item)
    return result


def _migrate_wd_active(active: str, history: list[dict]) -> str:
    """Ensure active working_directory is an entry id (not a raw path)."""
    ids = {e["id"] for e in history}
    if active in ids:
        return active
    # Maybe it's a legacy path — find a matching entry
    for entry in history:
        if entry["path"] == active and not entry.get("ssh_host"):
            return entry["id"]
    # Fallback: first entry
    return history[0]["id"] if history else active


def _save_config(data: dict[str, Any]) -> None:
    path = _get_config_path()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _default_config() -> dict[str, Any]:
    from orchestrator.providers.voice_registry import (
        DEFAULT_VOICE_MODEL,
        DEFAULT_VOICE_PROVIDER,
        resolve_voice_target,
    )

    default_path = str(PROJECT_ROOT)
    default_entry = {"id": default_path, "path": default_path, "label": None, "ssh_host": None, "ssh_user": None, "ssh_key": None, "claude_config_dir": None}
    # Pull the default voice + transcription language for the default model.
    _, _, default_voice, default_lang = resolve_voice_target(
        DEFAULT_VOICE_PROVIDER, DEFAULT_VOICE_MODEL, None, None,
    )
    return {
        "working_directory": default_path,
        "working_directory_history": [default_entry],
        "enabled_mcps": [],   # empty = all enabled (legacy behavior)
        "chrome_extension": False,  # launch sessions with --chrome flag
        "provider": "claude",  # session-harness id (registry-driven)
        "default_model": "claude-sonnet-4-5-20250929",  # default model for orchestrator
        # Model used to summarize older conversation history into the digest the
        # voice agent reads at session start / reconnect.  Picked separately
        # from ``default_model`` because the chosen voice/text model is often a
        # realtime / audio model that can't be trusted to compress a long
        # transcript into a structured summary.  Empty string falls back to a
        # code-level default in ``orchestrator/session.py``
        # (``DEFAULT_SUMMARIZER_MODEL``).
        "summarizer_model": "",
        # Default *harness* model per provider — what `claude --model <id>`
        # or `qwen --model <id>` runs with on a new session.  Empty string
        # means "let the CLI pick its own default" (which is what the CLI
        # does when we omit the flag entirely).  Keyed by provider so the
        # picker can remember a different choice for each; populated from
        # the harness registry so a new harness lands with an empty entry
        # automatically.
        "harness_model": {p: "" for p in _valid_provider_names()},
        "default_voice_provider": DEFAULT_VOICE_PROVIDER,
        "default_voice_model": DEFAULT_VOICE_MODEL,
        "default_voice_name": default_voice,
        "default_voice_transcription_language": default_lang,
        # For the ``google`` voice provider only: which backend to talk
        # to. ``"vertex"`` (default) uses the Vertex AI Live endpoint;
        # ``"aistudio"`` falls back to the older
        # ``generativelanguage.googleapis.com`` path. Other providers
        # ignore this field.
        "default_voice_endpoint": _default_voice_endpoint(),
        "voice_recording_enabled": False,  # save raw audio from voice sessions
        # Increment B (voice subsystem refactor): user-tunable VAD +
        # mic-gain knobs. Defaults equal the historical hardcoded
        # literals exactly — see ``voice_vad.py:107-112`` — so changing
        # the API surface doesn't change out-of-the-box behaviour.
        "voice_vad_threshold": 0.28,        # Silero on-threshold; off = on - 0.15
        "voice_vad_min_silence_ms": 2500,   # ms below off-threshold before speech_stopped
        "voice_mic_gain": 1.0,              # server-side mic-input gain (reserved)
    }


def _default_voice_endpoint() -> str:
    """Resolve the default Gemini-backend id (see ``gemini_voice``)."""
    from orchestrator.providers.gemini_voice import resolve_endpoint_id
    return resolve_endpoint_id(None)


def _find_active_entry(config: dict[str, Any]) -> dict | None:
    """Return the WorkingDirectoryEntry dict for the currently-active working_directory."""
    active_id = config.get("working_directory", "")
    for entry in config.get("working_directory_history", []):
        if entry["id"] == active_id:
            return entry
    return None


# -----------------------------------------------------------------------
# Pydantic models
# -----------------------------------------------------------------------

class WorkingDirectoryEntry(BaseModel):
    """A working directory target — local or remote via SSH."""
    id: str                  # Unique stable identifier (path for local, host:path for SSH)
    path: str                # Absolute path on the target machine
    label: str | None = None # Optional human-readable name
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_key: str | None = None          # Path to private key file (on the local machine)
    claude_config_dir: str | None = None  # Override CLAUDE_CONFIG_DIR on the remote machine


def _valid_provider_names() -> frozenset[str]:
    """Return the set of registered session-harness ids.

    Computed dynamically from :mod:`manager.registry` so a new harness
    is enabled by registering its spec — no edits here.
    """
    from manager.registry import ensure_all_registered, registered_provider_names
    ensure_all_registered()
    return frozenset(registered_provider_names())


def _orchestrator_provider_for_model(model_id: str) -> str | None:
    """Map a model id to the orchestrator provider that handles it.

    Returns one of ``"anthropic"``, ``"openai"``, or ``None`` if the
    provider couldn't be inferred.  Used to validate at config-save time
    that the underlying SDK is installed before we let the user pick a
    model they can't actually run.
    """
    from orchestrator.config import _infer_model_info, get_model_info
    info = get_model_info(model_id) or _infer_model_info(model_id)
    if info is None:
        return None
    return info.provider.value


def _check_orchestrator_sdk_available(provider_id: str) -> str | None:
    """Return an install hint if the SDK for *provider_id* isn't importable.

    Returns ``None`` when the SDK is available.  Hint is a human-readable
    string suitable for a 400 response body.
    """
    sdk = {"anthropic": "anthropic", "openai": "openai"}.get(provider_id)
    if sdk is None:
        return None
    try:
        __import__(sdk)
    except ImportError:
        return (
            f"The `{sdk}` package isn't installed in this venv, so models "
            f"backed by the {provider_id} provider can't be used.  "
            f"Install it with: pip install -r requirements-{sdk}.txt"
        )
    return None


class ConfigUpdate(BaseModel):
    working_directory: str | None = None  # entry id to set as active
    working_directory_history: list[WorkingDirectoryEntry] | None = None  # full replacement
    enabled_mcps: list[str] | None = None
    chrome_extension: bool | None = None
    provider: str | None = None  # session provider — "claude" | "qwen"
    default_model: str | None = None  # default model for new orchestrator sessions
    summarizer_model: str | None = None  # model used to summarize older history for the voice prompt
    harness_model: dict[str, str] | None = None  # per-provider harness model ("" = CLI default)
    default_voice_provider: str | None = None  # default provider for voice sessions
    default_voice_model: str | None = None     # default model for voice sessions
    default_voice_name: str | None = None      # default voice/speaker for voice sessions
    default_voice_transcription_language: str | None = None  # "" = auto-detect
    # Backend for the ``google`` provider only — "vertex" | "aistudio".
    # ``None`` falls back to the env / module default.
    default_voice_endpoint: str | None = None
    voice_recording_enabled: bool | None = None  # save raw audio from voice sessions
    # Increment B (voice subsystem refactor) — VAD + mic tuning knobs.
    # Validated ranges per plan §B (e.g. threshold 0.15-0.50).
    voice_vad_threshold: float | None = None
    voice_vad_min_silence_ms: int | None = None
    voice_mic_gain: float | None = None


# -----------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------

@router.get("")
async def get_config() -> dict[str, Any]:
    """Return the current global configuration."""
    return _load_config()


@router.get("/providers")
async def list_session_providers() -> dict[str, Any]:
    """Return the registered session-harness specs for the frontend picker.

    Each entry has ``{id, label, description}`` — the same shape the
    Config/Session pages used to hardcode.  Adding a fourth harness lands
    here automatically by registering its spec.
    """
    from manager.registry import ensure_all_registered, get_registry
    ensure_all_registered()
    specs = [
        {"id": s.name, "label": s.label, "description": s.description}
        for s in get_registry().all().values()
    ]
    return {"providers": specs}


@router.get("/harness/qwen/models")
async def list_harness_qwen_models() -> dict[str, Any]:
    """Return the Qwen Code model catalog discovered from ``~/.qwen/settings.json``.

    The catalog is the source of truth Qwen Code itself uses for ``--model``
    validation, so anything the user wires up there (Qwen, DeepSeek, GLM, a
    local Ollama endpoint, …) shows up here automatically.  An empty list
    means the settings file is missing or has no providers configured — the
    frontend can fall back to "let Qwen pick the default."
    """
    from manager.qwen.models import list_qwen_models
    return {"models": [m.to_dict() for m in list_qwen_models()]}


# In-memory cache for the Gemini Live models list. The upstream API
# response shape is stable across calls but Google ships new Live
# models frequently — refresh every 60s so a new model becomes
# available without a server restart, but page loads don't hammer
# models.list.
# Per-backend Gemini Live model-catalog cache. Keyed by endpoint id
# ("aistudio" / "vertex"); each entry stores the timestamp + list. Both
# upstreams are reasonably stable but expensive to hit on every page
# load, so we cache for 60s. ``None`` means "not yet fetched".
_GEMINI_LIVE_MODELS_CACHE: dict[str, dict[str, Any]] = {
    "aistudio": {"at": 0.0, "models": None},
    "vertex": {"at": 0.0, "models": None},
}
_GEMINI_LIVE_MODELS_CACHE_TTL_S = 60.0


def _humanize_gemini_model_name(model_id: str) -> str:
    """Turn ``gemini-2.5-flash-native-audio-latest`` into a human label.

    Best-effort — strips the ``gemini-`` prefix, splits on ``-``, and
    title-cases each token (with a few special cases). Falls back to
    the raw id if the heuristic doesn't apply.
    """
    if not model_id.startswith("gemini-"):
        return model_id
    stem = model_id[len("gemini-"):]
    # Pull off a trailing date stamp like "preview-09-2025" or
    # "latest" so we can format it as a parenthesized suffix.
    parts = stem.split("-")
    out_parts: list[str] = ["Gemini"]
    for token in parts:
        if token in {"latest"}:
            out_parts.append("(latest)")
        elif token in {"preview"}:
            out_parts.append("(preview)")
        elif token in {"live"}:
            out_parts.append("Live")
        elif token in {"native"}:
            out_parts.append("Native")
        elif token in {"audio"}:
            out_parts.append("Audio")
        elif token in {"flash"}:
            out_parts.append("Flash")
        elif token.isdigit() and len(token) == 4:
            # Year fragment — append as parenthesized suffix.
            out_parts.append(f"({token})")
        else:
            out_parts.append(token.capitalize())
    return " ".join(out_parts)


@router.get("/voice/google/models")
async def list_voice_google_models(endpoint: str | None = None) -> dict[str, Any]:
    """Return the Gemini Live model catalog for the selected backend.

    The ``endpoint`` query param picks which Google backend to query:

    - ``vertex`` (default): queries Vertex's
      ``publishers/google/models`` catalog. Auth via Application
      Default Credentials.
    - ``aistudio``: queries
      ``generativelanguage.googleapis.com/v1beta/models``. Auth via the
      ``GEMINI_API_KEY`` env var.

    Both responses are name-filtered for Live-capable models, cached in
    memory for 60s (per backend), and shaped into the
    ``VoiceModelEntry``-compatible JSON the Config page consumes. On any
    failure the route returns ``{models: []}`` and the frontend falls
    back to the static ``VOICE_MODELS["google"]`` entry.
    """
    from orchestrator.providers.gemini_voice import resolve_endpoint_id

    backend = resolve_endpoint_id(endpoint)
    cache = _GEMINI_LIVE_MODELS_CACHE[backend]

    now = time.monotonic()
    if (
        cache["models"] is not None
        and (now - cache["at"]) < _GEMINI_LIVE_MODELS_CACHE_TTL_S
    ):
        return {"models": cache["models"]}

    if backend == "vertex":
        out = await _fetch_vertex_gemini_models()
    else:
        out = await _fetch_aistudio_gemini_models()

    # Mark the first entry default — Config page assumes exactly one
    # default entry per provider.
    if out:
        out[0]["default"] = True

    cache["models"] = out
    cache["at"] = now
    return {"models": out}


async def _fetch_aistudio_gemini_models() -> list[dict[str, Any]]:
    """Fetch the AI Studio Live catalog. Returns [] on any failure."""
    from orchestrator.providers.gemini_voice import GEMINI_LIVE_VOICES

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return []

    # AI Studio's default page size is 50, which excludes the Live models
    # (they sit past the cutoff). Request a large page and walk
    # ``nextPageToken`` defensively so we don't silently drop them again.
    all_models: list[dict[str, Any]] = []
    page_token: str | None = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for _ in range(10):
                params: dict[str, str] = {"key": api_key, "pageSize": "1000"}
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params=params,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "AI Studio models.list returned %s; falling back to static registry",
                        resp.status_code,
                    )
                    return []
                data = resp.json()
                all_models.extend(data.get("models", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
    except Exception:  # noqa: BLE001
        logger.exception(
            "AI Studio models.list failed; falling back to static registry",
        )
        return []

    voices_list = [{"id": v, "label": v, "description": ""} for v in GEMINI_LIVE_VOICES]
    out: list[dict[str, Any]] = []
    for m in all_models:
        methods = m.get("supportedGenerationMethods", [])
        if "bidiGenerateContent" not in methods:
            continue
        name = m.get("name", "")  # "models/<id>"
        mid = name.split("/", 1)[1] if "/" in name else name
        if not mid:
            continue
        out.append(_voice_model_entry(mid, voices_list, m.get("description", "")))
    return out


async def _fetch_vertex_gemini_models() -> list[dict[str, Any]]:
    """Fetch the Vertex publisher-model catalog. Returns [] on any failure.

    Vertex doesn't expose ``supportedGenerationMethods`` on the
    publisher-models endpoint, so we name-match instead.
    """
    from orchestrator.providers.gemini_voice import (
        DEFAULT_GCP_LOCATION,
        GEMINI_LIVE_VOICES,
        get_adc_access_token,
    )

    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        return []
    location = os.environ.get("GCP_LOCATION", DEFAULT_GCP_LOCATION)

    try:
        token = await get_adc_access_token()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Vertex AI ADC token mint failed; returning empty model list",
        )
        return []

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"https://{location}-aiplatform.googleapis.com/v1beta1/publishers/google/models",
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-goog-user-project": project_id,
                },
                params={"view": "PUBLISHER_MODEL_VIEW_BASIC"},
            )
        if resp.status_code != 200:
            logger.warning(
                "Vertex publishers/google/models returned %s; falling back to static registry",
                resp.status_code,
            )
            return []
        data = resp.json()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Vertex publishers/google/models failed; falling back to static registry",
        )
        return []

    voices_list = [{"id": v, "label": v, "description": ""} for v in GEMINI_LIVE_VOICES]
    out: list[dict[str, Any]] = []
    for m in data.get("publisherModels", []):
        name = m.get("name", "")  # "publishers/google/models/<id>"
        mid = name.rsplit("/", 1)[-1] if name else ""
        lower = mid.lower()
        if "live" not in lower and "native-audio" not in lower:
            continue
        if not mid:
            continue
        out.append(_voice_model_entry(mid, voices_list, m.get("description", "")))
    return out


def _voice_model_entry(
    model_id: str,
    voices_list: list[dict[str, str]],
    description: str = "",
) -> dict[str, Any]:
    """Shape a Gemini model id into the dict the Config page expects."""
    return {
        "id": model_id,
        "label": _humanize_gemini_model_name(model_id),
        "voice": voices_list[0]["id"] if voices_list else "",
        "voices": voices_list,
        "transcription_languages": [],
        "default_transcription_language": "",
        "default": False,
        "description": description,
    }


@router.put("")
async def update_config(body: ConfigUpdate) -> dict[str, Any]:
    """Update one or more config fields. Returns the full updated config."""
    config = _load_config()

    if body.working_directory_history is not None:
        validated: list[dict] = []
        for entry in body.working_directory_history:
            e = entry.model_dump()
            if entry.ssh_host:
                # Remote entry — we trust the user; we cannot validate a remote path locally.
                # Ensure the id is set to host:path if not explicitly set.
                if not e["id"] or e["id"] == e["path"]:
                    e["id"] = f"{entry.ssh_host}:{entry.path}"
                # Auto-derive CLAUDE_CONFIG_DIR as <remote_path>/.claude_config if not set
                if not e.get("claude_config_dir"):
                    e["claude_config_dir"] = entry.path.rstrip("/") + "/.claude_config"
            else:
                # Local entry — validate the path exists.
                if not Path(entry.path).is_dir():
                    raise HTTPException(status_code=400, detail=f"Directory does not exist: {entry.path}")
                if not e["id"] or e["id"] == "":
                    e["id"] = entry.path
            validated.append(e)
        config["working_directory_history"] = validated[:20]
        # If current active id was removed, reset to first entry
        active_ids = {e["id"] for e in validated}
        if config["working_directory"] not in active_ids:
            config["working_directory"] = validated[0]["id"] if validated else ""

    if body.working_directory is not None:
        new_id = body.working_directory
        history: list[dict] = config.get("working_directory_history", [])
        ids = {e["id"] for e in history}
        if new_id not in ids:
            raise HTTPException(status_code=400, detail=f"Unknown working directory id: {new_id}")
        config["working_directory"] = new_id

    if body.enabled_mcps is not None:
        config["enabled_mcps"] = body.enabled_mcps

    if body.chrome_extension is not None:
        config["chrome_extension"] = body.chrome_extension

    if body.provider is not None:
        provider = body.provider.strip().lower()
        valid = _valid_provider_names()
        if provider not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider {body.provider!r}; expected one of {sorted(valid)}",
            )
        config["provider"] = provider

    if body.harness_model is not None:
        # Shallow merge: only the keys present in the request overwrite the
        # saved map, so the frontend can patch one provider's choice without
        # clobbering the other.  Unknown provider keys are rejected so a
        # typo doesn't silently land in the config and confuse the picker.
        valid = _valid_provider_names()
        current = dict(config.get("harness_model") or {})
        for prov, model_id in body.harness_model.items():
            if prov not in valid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown harness provider {prov!r}; expected one of "
                        f"{sorted(valid)}"
                    ),
                )
            if not isinstance(model_id, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"harness_model[{prov!r}] must be a string (got {type(model_id).__name__})",
                )
            current[prov] = model_id.strip()
        config["harness_model"] = current

    if body.default_model is not None:
        # Accept any non-empty model ID. The orchestrator infers provider from
        # the ID prefix; unknown/invalid IDs surface as upstream API errors at
        # send time rather than being gated here.
        if not body.default_model.strip():
            raise HTTPException(status_code=400, detail="default_model cannot be empty")
        # Reject models whose underlying SDK isn't installed — better to fail
        # fast at save than to crash on the first message of a new session.
        provider_id = _orchestrator_provider_for_model(body.default_model)
        if provider_id is not None:
            hint = _check_orchestrator_sdk_available(provider_id)
            if hint is not None:
                raise HTTPException(status_code=400, detail=hint)
        config["default_model"] = body.default_model

    if body.summarizer_model is not None:
        # Empty string is allowed — means "use the code-level default".
        summ = body.summarizer_model.strip()
        if summ:
            provider_id = _orchestrator_provider_for_model(summ)
            if provider_id is not None:
                hint = _check_orchestrator_sdk_available(provider_id)
                if hint is not None:
                    raise HTTPException(status_code=400, detail=hint)
        config["summarizer_model"] = summ

    if (
        body.default_voice_provider is not None
        or body.default_voice_model is not None
        or body.default_voice_name is not None
        or body.default_voice_transcription_language is not None
    ):
        from orchestrator.providers.voice_registry import resolve_voice_target

        # Cascade rule: changing a higher-level field snaps the dependents
        # to the new model/provider's defaults. Changing a lower-level
        # field alone preserves the higher-level fields exactly.
        #
        #   Provider changed → model + voice + language all snap to new
        #   provider/model defaults.
        #   Model changed (provider not) → voice + language snap to new
        #   model defaults.
        #   Voice changed (alone) → keep saved language.
        #   Language changed (alone) → keep saved voice.
        provider_changed = body.default_voice_provider is not None
        model_changed = body.default_voice_model is not None
        voice_changed = body.default_voice_name is not None
        lang_changed = body.default_voice_transcription_language is not None

        provider_req = body.default_voice_provider or config.get("default_voice_provider")

        if provider_changed:
            # Snap model/voice/lang to new provider's defaults unless
            # the request also specified them.
            model_req = body.default_voice_model
            voice_req = body.default_voice_name
            lang_req = body.default_voice_transcription_language
        elif model_changed:
            # Provider unchanged, model changed → snap voice/lang to new
            # model defaults unless explicitly given.
            model_req = body.default_voice_model
            voice_req = body.default_voice_name
            lang_req = body.default_voice_transcription_language
        else:
            # Provider + model unchanged. Reuse saved values for any
            # field the request didn't explicitly set, so changing one
            # doesn't reset the others.
            model_req = config.get("default_voice_model")
            voice_req = (
                body.default_voice_name if voice_changed
                else config.get("default_voice_name")
            )
            lang_req = (
                body.default_voice_transcription_language if lang_changed
                else config.get("default_voice_transcription_language")
            )
        try:
            provider_id, model_entry, voice_id, lang_id = resolve_voice_target(
                provider_req, model_req, voice_req, lang_req,
            )
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        # OpenAI voice needs the `openai` SDK installed.  Qwen voice uses
        # raw websockets so it works regardless.
        if provider_id == "openai":
            hint = _check_orchestrator_sdk_available("openai")
            if hint is not None:
                raise HTTPException(status_code=400, detail=hint)
        config["default_voice_provider"] = provider_id
        config["default_voice_model"] = model_entry["id"]
        config["default_voice_name"] = voice_id
        config["default_voice_transcription_language"] = lang_id

    if body.default_voice_endpoint is not None:
        from orchestrator.providers.gemini_voice import KNOWN_ENDPOINTS
        endpoint = body.default_voice_endpoint.strip()
        if endpoint not in KNOWN_ENDPOINTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown voice endpoint {endpoint!r}; "
                    f"expected one of {list(KNOWN_ENDPOINTS)}"
                ),
            )
        config["default_voice_endpoint"] = endpoint

    if body.voice_recording_enabled is not None:
        config["voice_recording_enabled"] = body.voice_recording_enabled

    # Increment B — VAD + mic tuning knobs. Ranges are deliberately
    # tight so a malformed slider drag can't permanently break the
    # user's VAD; defaults equal HEAD constants exactly (see
    # tests/parity/test_vad_defaults_parity.py).
    if body.voice_vad_threshold is not None:
        if not (0.15 <= body.voice_vad_threshold <= 0.50):
            raise HTTPException(
                status_code=400,
                detail=(
                    "voice_vad_threshold must be in [0.15, 0.50] "
                    f"(got {body.voice_vad_threshold})"
                ),
            )
        config["voice_vad_threshold"] = float(body.voice_vad_threshold)

    if body.voice_vad_min_silence_ms is not None:
        if not (800 <= body.voice_vad_min_silence_ms <= 5000):
            raise HTTPException(
                status_code=400,
                detail=(
                    "voice_vad_min_silence_ms must be in [800, 5000] "
                    f"(got {body.voice_vad_min_silence_ms})"
                ),
            )
        config["voice_vad_min_silence_ms"] = int(body.voice_vad_min_silence_ms)

    if body.voice_mic_gain is not None:
        if not (0.5 <= body.voice_mic_gain <= 2.0):
            raise HTTPException(
                status_code=400,
                detail=(
                    "voice_mic_gain must be in [0.5, 2.0] "
                    f"(got {body.voice_mic_gain})"
                ),
            )
        config["voice_mic_gain"] = float(body.voice_mic_gain)

    _save_config(config)
    return config
