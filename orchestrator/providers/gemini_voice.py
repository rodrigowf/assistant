"""Concrete Gemini Live voice backends.

This module exposes the two concrete classes implementing the Gemini
Live protocol — one per Google backend — plus a small ``select_backend``
helper used by the voice registry to pick between them at instantiation
time.

- :class:`GeminiAIStudioBackend` — talks to
  ``generativelanguage.googleapis.com`` using ``?key=$GEMINI_API_KEY``.
- :class:`VertexAIBackend` — talks to
  ``{location}-aiplatform.googleapis.com`` using a Bearer token from
  Application Default Credentials.

Both speak the same JSON protocol on the wire — the differences are
URL, auth, and the ``setup.model`` qualifier. All of that protocol-level
machinery lives in :mod:`gemini_voice_base`.

Selection precedence (used by ``select_backend``):
1. explicit ``endpoint`` argument (``"aistudio"`` or ``"vertex"``),
2. ``GEMINI_VOICE_BACKEND`` env var,
3. fallback :data:`DEFAULT_ENDPOINT` (Vertex — the stable one).
"""

from __future__ import annotations

import asyncio
import logging
import os

import websockets

from orchestrator.providers.gemini_voice_base import (
    GeminiVoiceProviderBase,
    GEMINI_LIVE_VOICES,
)

logger = logging.getLogger(__name__)

# Backend identifiers — these are the values stored in
# ``assistant_config.json:default_voice_endpoint`` and the
# ``endpoint=`` query param on voice routes.
ENDPOINT_AISTUDIO = "aistudio"
ENDPOINT_VERTEX = "vertex"
KNOWN_ENDPOINTS = (ENDPOINT_AISTUDIO, ENDPOINT_VERTEX)
DEFAULT_ENDPOINT = ENDPOINT_VERTEX

# --- AI Studio constants -----------------------------------------------------
AI_STUDIO_LIVE_WS = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
AI_STUDIO_DEFAULT_MODEL = "gemini-2.5-flash-native-audio-latest"

# --- Vertex AI constants -----------------------------------------------------
VERTEX_LIVE_WS_TEMPLATE = (
    "wss://{location}-aiplatform.googleapis.com/ws/"
    "google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"
)
VERTEX_DEFAULT_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_GCP_LOCATION = "us-central1"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class GeminiAIStudioBackend(GeminiVoiceProviderBase):
    """Gemini Live via AI Studio (``generativelanguage.googleapis.com``).

    Auth: ``?key=$GEMINI_API_KEY`` query param on the WS URL. No project
    ID needed — the key encodes the project. Note Google has been known
    to revoke Live access at this endpoint without warning (see module
    docstring of ``gemini_voice_base``); :class:`VertexAIBackend` is the
    recommended default.
    """

    DEFAULT_MODEL = AI_STUDIO_DEFAULT_MODEL

    @property
    def endpoint_id(self) -> str:
        return ENDPOINT_AISTUDIO

    def _qualify_model(self, model_id: str) -> str:
        return f"models/{model_id}"

    def _get_endpoint_url(self) -> str:
        return AI_STUDIO_LIVE_WS

    async def _open_upstream_ws(self) -> websockets.ClientConnection:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not configured — set it in context/.env or "
                "switch the voice endpoint to 'vertex'."
            )
        url = f"{AI_STUDIO_LIVE_WS}?key={api_key}"
        return await websockets.connect(
            url,
            open_timeout=15,
            max_size=2**24,
        )


class VertexAIBackend(GeminiVoiceProviderBase):
    """Gemini Live via Vertex AI (``{location}-aiplatform.googleapis.com``).

    Auth: OAuth Bearer token from Application Default Credentials,
    minted per session. Required configuration:

    - ``GCP_PROJECT_ID`` — numeric Cloud project ID hosting Vertex AI.
    - ``GCP_LOCATION`` — region (defaults to ``us-central1``).
    - ADC source: ``gcloud auth application-default login`` *or*
      ``GOOGLE_APPLICATION_CREDENTIALS`` pointing at a service-account
      JSON key.
    """

    DEFAULT_MODEL = VERTEX_DEFAULT_MODEL

    @property
    def endpoint_id(self) -> str:
        return ENDPOINT_VERTEX

    def _qualify_model(self, model_id: str) -> str:
        project_id = _require_gcp_project_id()
        location = os.environ.get("GCP_LOCATION", DEFAULT_GCP_LOCATION)
        return (
            f"projects/{project_id}/locations/{location}"
            f"/publishers/google/models/{model_id}"
        )

    def _get_endpoint_url(self) -> str:
        location = os.environ.get("GCP_LOCATION", DEFAULT_GCP_LOCATION)
        return VERTEX_LIVE_WS_TEMPLATE.format(location=location)

    async def _open_upstream_ws(self) -> websockets.ClientConnection:
        _require_gcp_project_id()  # fail fast if missing
        token = await get_adc_access_token()
        return await websockets.connect(
            self._get_endpoint_url(),
            additional_headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            open_timeout=15,
            max_size=2**24,
        )


# Backwards-compat alias. ``voice_registry`` imports this name as the
# "Google" provider class; the registry's ``instantiate_provider`` calls
# :func:`select_backend` to pick the concrete subclass at construction
# time, so the alias is mostly cosmetic — but keeping it avoids a chain
# of import renames in callers that already do
# ``from gemini_voice import GeminiLiveVoiceProvider``.
GeminiLiveVoiceProvider = VertexAIBackend


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_BACKEND_CLASSES: dict[str, type[GeminiVoiceProviderBase]] = {
    ENDPOINT_AISTUDIO: GeminiAIStudioBackend,
    ENDPOINT_VERTEX: VertexAIBackend,
}


def resolve_endpoint_id(endpoint: str | None) -> str:
    """Pick the active backend id from (explicit arg, env, default)."""
    if endpoint and endpoint in _BACKEND_CLASSES:
        return endpoint
    env_value = (os.environ.get("GEMINI_VOICE_BACKEND") or "").strip()
    if env_value and env_value in _BACKEND_CLASSES:
        return env_value
    return DEFAULT_ENDPOINT


def select_backend(endpoint: str | None) -> type[GeminiVoiceProviderBase]:
    """Return the concrete backend class for ``endpoint`` (or default)."""
    return _BACKEND_CLASSES[resolve_endpoint_id(endpoint)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_gcp_project_id() -> str:
    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        raise RuntimeError(
            "GCP_PROJECT_ID not configured — set it in context/.env to the "
            "numeric Cloud project ID hosting Vertex AI (e.g. 493034518147)."
        )
    return project_id


async def get_adc_access_token() -> str:
    """Mint an OAuth access token from Application Default Credentials.

    Runs the synchronous ``google.auth`` flow in a thread so it doesn't
    block the event loop. ADC discovery order is standard:
    ``GOOGLE_APPLICATION_CREDENTIALS`` → ``gcloud auth
    application-default login`` file → GCE metadata server.
    """
    def _refresh() -> str:
        from google.auth import default
        from google.auth.transport.requests import Request

        creds, _project = default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(Request())
        return creds.token

    return await asyncio.to_thread(_refresh)


__all__ = [
    "GeminiAIStudioBackend",
    "VertexAIBackend",
    "GeminiLiveVoiceProvider",
    "ENDPOINT_AISTUDIO",
    "ENDPOINT_VERTEX",
    "KNOWN_ENDPOINTS",
    "DEFAULT_ENDPOINT",
    "DEFAULT_GCP_LOCATION",
    "GEMINI_LIVE_VOICES",
    "resolve_endpoint_id",
    "select_backend",
    "get_adc_access_token",
]
