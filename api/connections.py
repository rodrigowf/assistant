"""ConnectionManager — tracks active WebSocket sessions."""

from __future__ import annotations

import orjson
from starlette.websockets import WebSocket, WebSocketState

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
    """Track the single active orchestrator session with multiple WebSocket subscribers.

    Only ONE orchestrator session can be active at a time, but multiple
    WebSocket connections (e.g. two browser windows) can subscribe to it
    simultaneously and all receive the same events.
    """

    def __init__(self) -> None:
        self._session: object | None = None
        self._session_id: str | None = None
        self._subscribers: set[WebSocket] = set()

    def connect(self, session_id: str, ws: WebSocket, session: object) -> bool:
        """Register the orchestrator session. Returns False if one is already active."""
        if self._session is not None:
            return False
        self._session = session
        self._session_id = session_id
        self._subscribers = {ws}
        return True

    def subscribe(self, session_id: str, ws: WebSocket) -> bool:
        """Add a WebSocket subscriber to the active session.

        Returns True if the session matched and ws was added,
        False if there is no active session or the session_id doesn't match.
        """
        if self._session is None or self._session_id != session_id:
            return False
        self._subscribers.add(ws)
        return True

    def unsubscribe(self, ws: WebSocket) -> None:
        """Remove a WebSocket subscriber (called when a browser disconnects)."""
        self._subscribers.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        """Send a JSON payload to all subscribed WebSockets."""
        if not self._subscribers:
            return
        data = orjson.dumps(payload)
        dead: list[WebSocket] = []
        for ws in self._subscribers:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._subscribers.discard(ws)

    async def disconnect(self) -> None:
        """Stop the orchestrator session and clear all state."""
        if self._session is not None:
            try:
                if hasattr(self._session, "stop"):
                    await self._session.stop()
            except Exception:
                pass
            self._session = None
            self._session_id = None
            self._subscribers.clear()

    def get_session(self) -> object | None:
        """Return the active OrchestratorSession, or None."""
        return self._session

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def is_active(self) -> bool:
        return self._session is not None

    @property
    def session_id(self) -> str | None:
        return self._session_id
