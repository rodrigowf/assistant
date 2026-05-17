"""ConnectionManager — tracks active WebSocket sessions (legacy, for auth route)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.websockets import WebSocket

from manager.base_session import BaseSessionManager

if TYPE_CHECKING:
    # Imported only for type-checkers; runtime code uses the base class so
    # this module can load without the Claude SDK installed.
    pass


class ConnectionManager:
    """Track active WebSocket connections and their session-manager instances.

    Used by the auth route to look up sessions by WebSocket.  Typed against
    ``BaseSessionManager`` so the module stays provider-agnostic — Claude
    and Qwen sessions are stored uniformly.
    """

    def __init__(self) -> None:
        self._active: dict[str, tuple[WebSocket, BaseSessionManager]] = {}

    def connect(self, session_id: str, ws: WebSocket, sm: BaseSessionManager) -> None:
        self._active[session_id] = (ws, sm)

    async def disconnect(self, session_id: str) -> None:
        entry = self._active.pop(session_id, None)
        if entry is not None:
            _, sm = entry
            try:
                await sm.stop()
            except Exception:
                pass

    def get(self, session_id: str) -> tuple[WebSocket, BaseSessionManager] | None:
        return self._active.get(session_id)

    def is_active(self, session_id: str) -> bool:
        return session_id in self._active

    @property
    def active_count(self) -> int:
        return len(self._active)
