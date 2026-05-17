"""Voice and audio endpoints for the orchestrator.

Provides:
- POST /api/orchestrator/voice/session — ephemeral provider token (legacy: OpenAI only)
- GET  /api/orchestrator/voice/models  — registry of voice providers and their models
- POST /api/orchestrator/audio         — upload audio file for multimodal processing
- GET  /api/orchestrator/models        — list available text/audio models
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from orchestrator.config import get_available_models, get_audio_capable_models
from orchestrator.providers.discovery import (
    list_orchestrator_models,
    list_voice_models_live,
)
from orchestrator.providers.voice_registry import (
    DEFAULT_VOICE_MODEL,
    DEFAULT_VOICE_PROVIDER,
    instantiate_provider,
    list_voice_models,
    resolve_voice_target,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice"])

# Audio upload constraints
MAX_AUDIO_SIZE_MB = 25  # OpenAI's limit is 25MB
ALLOWED_AUDIO_FORMATS = {"wav", "mp3", "webm", "ogg", "m4a", "flac"}


@router.post("/api/orchestrator/voice/session")
async def create_voice_session(
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    transcription_language: str | None = None,
    endpoint: str | None = None,
) -> dict:
    """Return ephemeral connection metadata for a voice provider.

    For backward compatibility (OpenAI WebRTC, default), the response shape
    matches the legacy contract used by the existing frontend / Android
    clients::

        {
          "client_secret": {"value": "ek_...", "expires_at": 123456789},
          "id": "...",
          "model": "gpt-realtime",
          "voice": "cedar",
          "connection_info": { ... }   # provider-agnostic metadata for new clients
        }

    Newer clients should read ``connection_info`` (which includes
    ``connection_type``, ``endpoint``, ``audio_in_format``, etc.) instead of
    the OpenAI-specific top-level fields.
    """
    try:
        provider_id, model_entry, voice_name, language = resolve_voice_target(
            provider, model, voice, transcription_language,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        provider_obj = instantiate_provider(
            provider_id, model_entry["id"], voice_name, language,
            endpoint=endpoint,
        )
        info = await provider_obj.get_connection_info()
    except RuntimeError as e:
        # Missing API key or config
        raise HTTPException(status_code=503, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("Provider session creation failed: %s %s",
                     e.response.status_code, e.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"{provider_id} API error: {e.response.status_code}",
        )
    except Exception as e:
        logger.exception("Failed to create voice session for %s/%s",
                         provider_id, model_entry["id"])
        raise HTTPException(status_code=502, detail=str(e))

    response: dict = {"connection_info": info}
    if provider_id == "openai":
        # Legacy fields for existing OpenAI WebRTC clients.
        response.update({
            "client_secret": {
                "value": info["ephemeral_token"],
                "expires_at": info["expires_at"],
            },
            "model": info["model"],
            "voice": info["voice"],
        })
    return response


@router.get("/api/orchestrator/voice/models")
async def list_voice_provider_models() -> dict:
    """Return live voice provider models, merged with the static registry.

    Used by the settings UI to populate provider/model dropdowns. Falls
    back to the static registry on API/network errors or when keys are
    missing.
    """
    try:
        providers = await list_voice_models_live()
    except Exception:
        logger.exception("Live voice model discovery failed; falling back to static")
        providers = list_voice_models()
    return {
        "providers": providers,
        "default_provider": DEFAULT_VOICE_PROVIDER,
        "default_model": DEFAULT_VOICE_MODEL,
    }


@router.post("/api/orchestrator/audio")
async def upload_audio(
    request: Request,
    audio: UploadFile = File(...),
    text: str | None = Form(None),
) -> dict:
    """Upload an audio file for multimodal processing.

    The audio is sent to a multimodal model (GPT-4o) that handles both
    transcription and understanding in a single pass.

    Args:
        audio: The audio file (wav, mp3, webm, ogg, m4a, flac)
        text: Optional accompanying text prompt

    Returns:
        {"status": "queued", "audio_format": str, "size_bytes": int}

    Note:
        This endpoint queues the audio for processing. The actual response
        is streamed via the WebSocket connection. The caller should listen
        on the orchestrator WebSocket for the response events.
    """
    # Validate file format
    filename = audio.filename or "audio"
    ext = Path(filename).suffix.lstrip(".").lower()

    # Try to get format from content type if extension is missing
    if not ext and audio.content_type:
        content_type = audio.content_type.lower()
        if "wav" in content_type or "wave" in content_type:
            ext = "wav"
        elif "mp3" in content_type or "mpeg" in content_type:
            ext = "mp3"
        elif "webm" in content_type:
            ext = "webm"
        elif "ogg" in content_type:
            ext = "ogg"
        elif "m4a" in content_type or "mp4" in content_type:
            ext = "m4a"
        elif "flac" in content_type:
            ext = "flac"

    if ext not in ALLOWED_AUDIO_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format: {ext}. Allowed: {', '.join(ALLOWED_AUDIO_FORMATS)}",
        )

    # Read and validate size
    content = await audio.read()
    size_bytes = len(content)
    max_size_bytes = MAX_AUDIO_SIZE_MB * 1024 * 1024

    if size_bytes > max_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file too large: {size_bytes / (1024*1024):.1f}MB (max {MAX_AUDIO_SIZE_MB}MB)",
        )

    if size_bytes == 0:
        raise HTTPException(status_code=400, detail="Empty audio file")

    # Encode to base64 for WebSocket transmission
    audio_base64 = base64.b64encode(content).decode("utf-8")

    # Get the pool to send to the orchestrator session
    pool = request.app.state.pool

    if not pool.has_orchestrator():
        raise HTTPException(
            status_code=400,
            detail="No active orchestrator session. Start a session via WebSocket first.",
        )

    # Queue the audio for processing by broadcasting a message
    # The WebSocket handler will pick this up
    await pool.broadcast_orchestrator({
        "type": "audio_upload",
        "audio": audio_base64,
        "format": ext,
        "text": text,
        "size_bytes": size_bytes,
    })

    return {
        "status": "queued",
        "audio_format": ext,
        "size_bytes": size_bytes,
    }


@router.get("/api/orchestrator/models")
async def list_models() -> dict:
    """List orchestrator models — fetched live from each provider.

    Falls back to the static registry on API/network errors or when keys
    are missing.
    """
    try:
        models = await list_orchestrator_models()
    except Exception:
        logger.exception("Live model discovery failed; falling back to static")
        models = get_available_models()

    audio_models = [m for m in models if m.supports_audio]
    return {
        "models": [m.to_dict() for m in models],
        "audio_capable_models": [m.model_id for m in audio_models],
        "default_model": "claude-sonnet-4-5-20250929",
    }


@router.get("/api/orchestrator/models/audio")
async def list_audio_models() -> dict:
    """List models that support audio input (live, with static fallback)."""
    try:
        all_models = await list_orchestrator_models()
        models = [m for m in all_models if m.supports_audio]
    except Exception:
        logger.exception("Live audio model discovery failed; falling back to static")
        models = get_audio_capable_models()
    return {
        "models": [m.to_dict() for m in models],
    }
