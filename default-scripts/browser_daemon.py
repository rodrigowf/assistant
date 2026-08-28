#!/usr/bin/env python3
"""Local browser-control daemon — terminates the Chrome extension's WebSocket.

Runs on the machine Chrome is on. That is the *only* thing it needs to be
co-located with: the extension holds one persistent WebSocket, so something
long-lived has to accept it, and the Claude Code sessions that drive the
browser run on that same machine.

Deliberately minimal — the browser routes plus uploads (the extension POSTs
screenshots to /api/uploads on the origin it derives from its WebSocket URL).
None of the main backend's session pool, indexers or search server.

    context/scripts/run.sh context/scripts/browser_daemon.py          # foreground
    context/scripts/run.sh context/scripts/browser_cmd.py look        # auto-starts it

Binds loopback only. `browser_cmd.py` starts it on demand, so you rarely run
this by hand; do so to watch the logs.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn
from fastapi import FastAPI

from api.routes import browser as browser_route
from api.routes import uploads as uploads_route

HOST = "127.0.0.1"
# Deliberately NOT 8765. That port means "the main assistant backend" on both
# machines, and reusing it here is exactly the ambiguity that made an earlier
# design route browser traffic through the Jetson and back.
PORT = int(os.environ.get("BROWSER_DAEMON_PORT", "8766"))

LOG_DIR = PROJECT_ROOT / "logs"
PID_FILE = LOG_DIR / "browser-daemon.pid"


def build_app() -> FastAPI:
    app = FastAPI(title="Archie Browser Daemon")
    app.state.browser_hub = browser_route.BrowserHub()
    app.include_router(browser_route.router)
    app.include_router(uploads_route.router)
    return app


def port_is_taken(host: str, port: int) -> bool:
    """Cheap pre-flight so a second start exits cleanly rather than crashing.

    Not race-free — uvicorn's own bind failure below is the real guard. This
    just makes the common case ("already running") a clean exit instead of a
    traceback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def main() -> int:
    if port_is_taken(HOST, PORT):
        print(f"browser daemon already listening on {HOST}:{PORT}", flush=True)
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    print(f"browser daemon starting on http://{HOST}:{PORT} (pid {os.getpid()})",
          flush=True)
    try:
        uvicorn.run(build_app(), host=HOST, port=PORT, log_level="warning")
    except OSError as e:
        # Lost the race with another starter — that's success, not failure.
        print(f"browser daemon: port {PORT} already bound ({e})", flush=True)
        return 0
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
