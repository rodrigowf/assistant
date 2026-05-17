#!/usr/bin/env python3
"""Probe both Gemini Live backends to see which one this user has access to.

Used by the verification step of each installer (and by the install
agent when guiding the user through key setup) to pick a sensible
``default_voice_endpoint`` and to surface actionable hints when neither
backend works.

Output (always JSON on stdout, regardless of success):

    {
      "vertex":   {"status": "ok|skip|fail", "reason": "...", "model": "..."},
      "aistudio": {"status": "ok|skip|fail", "reason": "...", "model": "..."},
      "recommended_default": "vertex" | "aistudio" | null
    }

``status`` semantics:
- ``ok``    — opened the WS and received ``setupComplete``.
- ``skip``  — backend can't even be attempted (missing env var / ADC /
              SDK).  The ``reason`` tells the user how to fix it.
- ``fail``  — attempted the WS but it closed (typically Google's 1008
              policy denial or a missing model).

Exit code is always 0 — installers parse the JSON to decide warnings.
Designed to be importable too: ``probe()`` returns the same dict
without printing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

# Models to attempt per backend (first one that succeeds wins).
# Two AI Studio names to try because Google ships preview tags
# frequently — at least one usually works when access is enabled.
AISTUDIO_MODELS = [
    "gemini-2.5-flash-native-audio-latest",
    "gemini-3.1-flash-live-preview",
]
VERTEX_MODELS = [
    "gemini-live-2.5-flash-native-audio",
]
TIMEOUT_S = 8.0


async def _try_aistudio() -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "skip",
            "reason": (
                "GEMINI_API_KEY not set in context/.env. "
                "Get one at https://aistudio.google.com/apikey and add it."
            ),
            "model": None,
        }
    try:
        import websockets  # noqa: F401
    except Exception as e:
        return {"status": "skip", "reason": f"websockets not importable: {e}", "model": None}

    url = (
        f"wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={api_key}"
    )
    last_err = ""
    for model in AISTUDIO_MODELS:
        try:
            import websockets
            async with websockets.connect(url, open_timeout=10) as ws:
                await ws.send(json.dumps({
                    "setup": {
                        "model": f"models/{model}",
                        "generationConfig": {"responseModalities": ["AUDIO"]},
                    },
                }))
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT_S)
                except asyncio.TimeoutError:
                    last_err = f"{model}: no reply in {TIMEOUT_S}s"
                    continue
                if b"setupComplete" in msg:
                    return {"status": "ok", "reason": "", "model": model}
                last_err = f"{model}: unexpected first frame {str(msg)[:120]}"
        except Exception as e:  # noqa: BLE001 — Google returns many error shapes
            reason = repr(e)[:200]
            last_err = f"{model}: {reason}"
            # 1008 = policy denial.  Google's common message:
            # "Your project has been denied access. Please contact support."
            if "denied access" in reason.lower():
                # Don't bother retrying other models — denial is project-wide.
                return {
                    "status": "fail",
                    "reason": (
                        "Google denied access to the AI Studio Live endpoint "
                        "for this project. Either enable billing on the Cloud "
                        "project tied to this key, or switch to the Vertex AI "
                        "backend (see GCP_PROJECT_ID in context/.env)."
                    ),
                    "model": model,
                }
    return {"status": "fail", "reason": last_err or "all models failed", "model": None}


async def _try_vertex() -> dict[str, Any]:
    project_id = os.environ.get("GCP_PROJECT_ID", "").strip()
    if not project_id:
        return {
            "status": "skip",
            "reason": (
                "GCP_PROJECT_ID not set in context/.env. "
                "Create a Cloud project at https://console.cloud.google.com/projectcreate, "
                "enable Vertex AI at https://console.cloud.google.com/apis/library/aiplatform.googleapis.com, "
                "then add the numeric project id here."
            ),
            "model": None,
        }
    try:
        import websockets  # noqa: F401
        from google.auth import default
        from google.auth.transport.requests import Request
    except Exception as e:
        return {
            "status": "skip",
            "reason": (
                f"google-auth / websockets not importable ({e}). "
                "These ship in requirements.txt — re-run pip install -r requirements.txt."
            ),
            "model": None,
        }

    # Mint ADC token.
    try:
        creds, _project = default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(Request())
        token = creds.token
    except Exception as e:  # noqa: BLE001
        return {
            "status": "skip",
            "reason": (
                f"Application Default Credentials not available ({e}). "
                "Run `gcloud auth application-default login` once, then "
                f"`gcloud auth application-default set-quota-project {project_id}`."
            ),
            "model": None,
        }

    location = os.environ.get("GCP_LOCATION", "us-central1")
    url = (
        f"wss://{location}-aiplatform.googleapis.com/ws/"
        "google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    last_err = ""
    for model in VERTEX_MODELS:
        model_uri = (
            f"projects/{project_id}/locations/{location}"
            f"/publishers/google/models/{model}"
        )
        try:
            import websockets
            async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
                await ws.send(json.dumps({
                    "setup": {
                        "model": model_uri,
                        "generationConfig": {"responseModalities": ["AUDIO"]},
                    },
                }))
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT_S)
                except asyncio.TimeoutError:
                    last_err = f"{model}: no reply in {TIMEOUT_S}s"
                    continue
                if b"setupComplete" in msg:
                    return {"status": "ok", "reason": "", "model": model}
                last_err = f"{model}: unexpected first frame {str(msg)[:120]}"
        except Exception as e:  # noqa: BLE001
            reason = repr(e)[:200]
            last_err = f"{model}: {reason}"
            if "not found" in reason.lower() and "publisher model" in reason.lower():
                last_err = (
                    f"{model}: Vertex doesn't expose this model in {location}. "
                    "Try a different GCP_LOCATION, or enable Vertex AI API."
                )
    return {"status": "fail", "reason": last_err or "all models failed", "model": None}


async def probe() -> dict[str, Any]:
    """Run both probes concurrently and return the combined verdict."""
    vertex, aistudio = await asyncio.gather(_try_vertex(), _try_aistudio())
    out: dict[str, Any] = {"vertex": vertex, "aistudio": aistudio}
    # Prefer Vertex when both work (it's the more stable endpoint).
    if vertex["status"] == "ok":
        out["recommended_default"] = "vertex"
    elif aistudio["status"] == "ok":
        out["recommended_default"] = "aistudio"
    else:
        out["recommended_default"] = None
    return out


def _load_context_env() -> None:
    """Best-effort .env loader so this script works outside the venv too.

    Doesn't try to be a full dotenv parser — just splits ``KEY=VALUE``
    lines and skips comments. Avoids adding python-dotenv as a hard
    dependency for the probe.
    """
    path = os.path.join(os.path.dirname(__file__), os.pardir, "context", ".env")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def main() -> int:
    _load_context_env()
    result = asyncio.run(probe())
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
