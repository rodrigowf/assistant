"""Voice session endpoint — issues ephemeral OpenAI tokens for WebRTC setup."""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice"])

OPENAI_REALTIME_SESSIONS_URL = "https://api.openai.com/v1/realtime/sessions"
VOICE_MODEL = "gpt-realtime"
VOICE_NAME = "cedar"


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
