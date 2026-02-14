"""ConnectionManager — tracks active WebSocket sessions."""

from __future__ import annotations

from starlette.websockets import WebSocket

from manager.session import SessionManager


class ConnectionManager:
    """Track active WebSocket connections and their SessionManager instances."""

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


class OrchestratorConnectionManager:
    """Track the single active orchestrator session.

    Only ONE orchestrator session can be active at a time.
    """

    def __init__(self) -> None:
        self._active: tuple[WebSocket, object] | None = None
        self._session_id: str | None = None

    def connect(self, session_id: str, ws: WebSocket, session: object) -> bool:
        """Register the orchestrator session. Returns False if one is already active."""
        if self._active is not None:
            return False
        self._active = (ws, session)
        self._session_id = session_id
        return True

    async def disconnect(self) -> None:
        """Disconnect the active orchestrator session."""
        if self._active is not None:
            _, session = self._active
            try:
                if hasattr(session, "stop"):
                    await session.stop()
            except Exception:
                pass
            self._active = None
            self._session_id = None

    def get_active(self) -> tuple[str, WebSocket, object] | None:
        """Get the active orchestrator (session_id, ws, session) or None."""
        if self._active is None or self._session_id is None:
            return None
        ws, session = self._active
        return (self._session_id, ws, session)

    @property
    def is_active(self) -> bool:
        return self._active is not None

    @property
    def session_id(self) -> str | None:
        return self._session_id
