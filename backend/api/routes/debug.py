"""Remote console log collector — receives logs POSTed from the browser."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from utils.paths import PROJECT_ROOT

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/debug", tags=["debug"])

LOG_FILE = PROJECT_ROOT / "logs" / "remote_console.log"


@router.post("/log", status_code=204)
async def collect_log(request: Request):
    try:
        body = await request.json()
        level = body.get("level", "log")
        msg = body.get("msg", "")
        ts = body.get("ts", datetime.utcnow().isoformat())
        # Behind the nginx proxy the socket peer is 127.0.0.1; nginx passes the device in X-Real-IP.
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
        line = f"[{ts}] [{ip}] [{level.upper()}] {msg}\n"
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line)
    except Exception:
        pass  # Never crash the client


@router.get("/log", response_class=PlainTextResponse)
async def read_log():
    if not LOG_FILE.exists():
        return "No logs yet.\n"
    return LOG_FILE.read_text()
