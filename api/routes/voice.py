"""Voice and audio endpoints for the orchestrator.

Provides:
- POST /api/orchestrator/voice/session — ephemeral OpenAI tokens for WebRTC
- POST /api/orchestrator/audio — upload audio file for multimodal processing
- GET /api/orchestrator/models — list available models
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from orchestrator.config import get_available_models, get_audio_capable_models

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice"])

# OpenAI Realtime API constants
OPENAI_REALTIME_SESSIONS_URL = "https://api.openai.com/v1/realtime/sessions"
VOICE_MODEL = "gpt-realtime"
VOICE_NAME = "cedar"

# Audio upload constraints
MAX_AUDIO_SIZE_MB = 25  # OpenAI's limit is 25MB
ALLOWED_AUDIO_FORMATS = {"wav", "mp3", "webm", "ogg", "m4a", "flac"}


@router.post("/api/orchestrator/voice/session")
async def create_voice_session() -> dict:
    """Exchange the server-side OPENAI_API_KEY for a short-lived ephemeral token.

    The ephemeral token (~60s TTL) is returned to the frontend, which uses it
    to establish a WebRTC connection directly with the OpenAI Realtime API.
    The actual OPENAI_API_KEY never reaches the browser.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")

    payload = {
        "model": VOICE_MODEL,
        "voice": VOICE_NAME,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                OPENAI_REALTIME_SESSIONS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error("OpenAI session creation failed: %s %s", e.response.status_code, e.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI API error: {e.response.status_code}",
        )
    except Exception as e:
        logger.exception("Failed to create voice session")
        raise HTTPException(status_code=502, detail=str(e))


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
    """List all available models for the orchestrator.

    Returns models grouped by provider with capability flags.
    """
    models = get_available_models()
    audio_models = get_audio_capable_models()

    return {
        "models": [m.to_dict() for m in models],
        "audio_capable_models": [m.model_id for m in audio_models],
        "default_model": "claude-sonnet-4-5-20250929",
    }


@router.get("/api/orchestrator/models/audio")
async def list_audio_models() -> dict:
    """List models that support audio input."""
    models = get_audio_capable_models()
    return {
        "models": [m.to_dict() for m in models],
    }
