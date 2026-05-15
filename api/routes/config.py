"""Global configuration endpoint — manages working directory, skills, and MCP defaults."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

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
        "voice_recording_enabled": False,  # save raw audio from voice sessions
    }


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
    harness_model: dict[str, str] | None = None  # per-provider harness model ("" = CLI default)
    default_voice_provider: str | None = None  # default provider for voice sessions
    default_voice_model: str | None = None     # default model for voice sessions
    default_voice_name: str | None = None      # default voice/speaker for voice sessions
    default_voice_transcription_language: str | None = None  # "" = auto-detect
    voice_recording_enabled: bool | None = None  # save raw audio from voice sessions


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
    from manager.qwen_models import list_qwen_models
    return {"models": [m.to_dict() for m in list_qwen_models()]}


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

    if body.voice_recording_enabled is not None:
        config["voice_recording_enabled"] = body.voice_recording_enabled

    _save_config(config)
    return config
