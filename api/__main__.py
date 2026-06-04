"""Programmatic uvicorn launcher.

Replaces ``python -m uvicorn api.app:create_app --factory ...`` because
the CLI cannot express ``ws_ping_interval=None`` / ``ws_ping_timeout=None``.
The CLI parses those as floats (default 20.0); passing ``0`` is interpreted
by the underlying ``websockets`` library as "ping every 0 seconds" which
immediately times out.

Why we need to disable protocol-level WS pings: the A300M Android peripheral
running okhttp on Android 5.0 doesn't reply to server-initiated PING frames,
so uvicorn closes the WS with 1011 "keepalive ping timeout" after 40s
(20s interval + 20s timeout) — exactly mid-voice-call. The app-level
heartbeat we already run (``{"type":"ping"}`` JSON messages every 15s,
plus a 45s silence-timeout) covers liveness without needing the protocol
PING frames.
"""

from __future__ import annotations

import os
import sys

import uvicorn


def main() -> None:
    host = os.environ.get("UVICORN_HOST", "127.0.0.1")
    port = int(os.environ.get("UVICORN_PORT", "8765"))
    config = uvicorn.Config(
        "api.app:create_app",
        factory=True,
        host=host,
        port=port,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    sys.exit(main() or 0)
