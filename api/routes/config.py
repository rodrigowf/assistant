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
        "default_model": "claude-sonnet-4-5-20250929",  # default model for orchestrator
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


class ConfigUpdate(BaseModel):
    working_directory: str | None = None  # entry id to set as active
    working_directory_history: list[WorkingDirectoryEntry] | None = None  # full replacement
    enabled_mcps: list[str] | None = None
    chrome_extension: bool | None = None
    default_model: str | None = None  # default model for new orchestrator sessions
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

    if body.default_model is not None:
        # Accept any non-empty model ID. The orchestrator infers provider from
        # the ID prefix; unknown/invalid IDs surface as upstream API errors at
        # send time rather than being gated here.
        if not body.default_model.strip():
            raise HTTPException(status_code=400, detail="default_model cannot be empty")
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
        config["default_voice_provider"] = provider_id
        config["default_voice_model"] = model_entry["id"]
        config["default_voice_name"] = voice_id
        config["default_voice_transcription_language"] = lang_id

    if body.voice_recording_enabled is not None:
        config["voice_recording_enabled"] = body.voice_recording_enabled

    _save_config(config)
    return config
