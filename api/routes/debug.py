"""Remote console log collector — receives logs POSTed from the browser."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/debug", tags=["debug"])

LOG_FILE = Path(__file__).resolve().parent.parent.parent / "remote_console.log"


@router.post("/log", status_code=204)
async def collect_log(request: Request):
    try:
        body = await request.json()
        level = body.get("level", "log")
        msg = body.get("msg", "")
        ts = body.get("ts", datetime.utcnow().isoformat())
        line = f"[{ts}] [{level.upper()}] {msg}\n"
        with LOG_FILE.open("a") as f:
            f.write(line)
    except Exception:
        pass  # Never crash the client


@router.get("/log", response_class=PlainTextResponse)
async def read_log():
    if not LOG_FILE.exists():
        return "No logs yet.\n"
    return LOG_FILE.read_text()
