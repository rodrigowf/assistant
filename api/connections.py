"""ConnectionManager — tracks active WebSocket sessions (legacy, for auth route)."""

from __future__ import annotations

from starlette.websockets import WebSocket

from manager.session import SessionManager


class ConnectionManager:
    """Track active WebSocket connections and their SessionManager instances.

    Used by the auth route to look up sessions by WebSocket.
    """

    def __init__(self) -> None:
        self._active: dict[str, tuple[WebSocket, SessionManager]] = {}

    def connect(self, session_id: str, ws: WebSocket, sm: SessionManager) -> None:
        self._active[session_id] = (ws, sm)

    async def disconnect(self, session_id: str) -> None:
        entry = self._active.pop(session_id, None)
        if entry is not None:
            _, sm = entry
            try:
                await sm.stop()
            except Exception:
                pass

    def get(self, session_id: str) -> tuple[WebSocket, SessionManager] | None:
        return self._active.get(session_id)

    def is_active(self, session_id: str) -> bool:
        return session_id in self._active

    @property
    def active_count(self) -> int:
        return len(self._active)
