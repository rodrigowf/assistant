"""SessionPool — shared pool of Claude Code sessions with event broadcast."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import orjson
from starlette.websockets import WebSocket, WebSocketState

from api.serializers import serialize_event
from manager.config import ManagerConfig
from manager.session import SessionManager
from manager.types import Event, TurnComplete

logger = logging.getLogger(__name__)


class SessionPool:
    """Shared pool of Claude Code sessions with per-session locking and broadcast.

    Both the frontend WebSocket handler and orchestrator tools use this pool.
    Sessions are independent — they survive orchestrator disconnects and can
    have multiple WebSocket subscribers receiving events simultaneously.

    Note on session IDs: The Claude Code SDK returns a placeholder ID at
    connect time.  The *real* session ID (used for JSONL storage) only
    arrives in the first ``ResultMessage``.  The pool delays the
    ``agent_session_opened`` notification until after the first ``send()``,
    so the frontend only ever sees the real, stable ID.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionManager] = {}
        self._subscribers: dict[str, set[WebSocket]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._watchers: set[WebSocket] = set()
        # Sessions that haven't been announced to watchers yet
        self._pending_announce: set[str] = set()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create(
        self,
        config: ManagerConfig,
        session_id: str | None = None,
        fork: bool = False,
    ) -> str:
        """Create, start, and register a SessionManager. Returns session_id.

        The ``agent_session_opened`` notification is deferred until after the
        first ``send()`` completes, when we have the real session ID.
        """
        sm = SessionManager(session_id=session_id, fork=fork, config=config)
        new_id = await sm.start()

        self._sessions[new_id] = sm
        self._subscribers[new_id] = set()
        self._locks[new_id] = asyncio.Lock()
        self._pending_announce.add(new_id)

        return new_id

    async def close(self, session_id: str) -> None:
        """Stop a session, notify subscribers, and clean up."""
        sm = self._sessions.pop(session_id, None)
        if sm is None:
            return

        try:
            await sm.stop()
        except Exception:
            logger.warning("Error stopping pooled session %s", session_id, exc_info=True)

        # Notify subscribers that the session is closed
        await self._broadcast(session_id, {"type": "session_stopped"})

        # Notify watchers (only if this session was already announced)
        if session_id not in self._pending_announce:
            await self._notify_watchers({
                "type": "agent_session_closed",
                "session_id": session_id,
            })

        self._pending_announce.discard(session_id)
        self._subscribers.pop(session_id, None)
        self._locks.pop(session_id, None)

    async def interrupt(self, session_id: str) -> None:
        """Interrupt the current response for a session."""
        sm = self._sessions.get(session_id)
        if sm is not None:
            await sm.interrupt()

    # ------------------------------------------------------------------
    # Session access
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> SessionManager | None:
        """Get a session by ID, or None."""
        return self._sessions.get(session_id)

    def has(self, session_id: str) -> bool:
        """Check if a session exists in the pool."""
        return session_id in self._sessions

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions with status info."""
        result = []
        for sid, sm in self._sessions.items():
            result.append({
                "session_id": sid,
                "status": sm.status.value,
                "cost": sm.cost,
                "turns": sm.turns,
            })
        return result

    # ------------------------------------------------------------------
    # Subscribers (WebSockets that receive session events)
    # ------------------------------------------------------------------

    def subscribe(self, session_id: str, ws: WebSocket) -> None:
        """Add a WebSocket subscriber for session events."""
        subs = self._subscribers.get(session_id)
        if subs is not None:
            subs.add(ws)

    def unsubscribe(self, session_id: str, ws: WebSocket) -> None:
        """Remove a WebSocket subscriber."""
        subs = self._subscribers.get(session_id)
        if subs is not None:
            subs.discard(ws)

    def subscriber_count(self, session_id: str) -> int:
        """Number of active subscribers for a session."""
        subs = self._subscribers.get(session_id)
        return len(subs) if subs else 0

    # ------------------------------------------------------------------
    # Watchers (WebSockets that get new-session notifications)
    # ------------------------------------------------------------------

    def watch(self, ws: WebSocket) -> None:
        """Register a WebSocket to receive new-session notifications."""
        self._watchers.add(ws)

    def unwatch(self, ws: WebSocket) -> None:
        """Unregister a session watcher."""
        self._watchers.discard(ws)

    # ------------------------------------------------------------------
    # Sending messages (with lock + broadcast)
    # ------------------------------------------------------------------

    async def send(
        self,
        session_id: str,
        text: str,
        *,
        source_ws: WebSocket | None = None,
    ) -> AsyncIterator[Event]:
        """Drive sm.send() with per-session lock, broadcasting to all subscribers.

        Yields raw Event objects for the caller to collect/process.
        Broadcasts serialized events to all subscriber WebSockets.

        If source_ws is provided, a ``user_message`` event is broadcast to
        OTHER subscribers (so they see what was sent). The source WS already
        knows what it sent.
        """
        sm = self._sessions.get(session_id)
        if sm is None:
            raise ValueError(f"No session with ID {session_id}")

        lock = self._locks[session_id]

        async with lock:
            # Broadcast user_message to subscribers that didn't originate the send
            await self._broadcast(
                session_id,
                {"type": "user_message", "text": text},
                exclude=source_ws,
            )

            async for event in sm.send(text):
                # Check for session ID change (SDK assigns real ID on first query)
                if isinstance(event, TurnComplete) and event.session_id and event.session_id != session_id:
                    new_id = event.session_id
                    self._rekey(session_id, new_id)
                    session_id = new_id

                # Broadcast serialized event to all subscribers
                payload = serialize_event(event)
                await self._broadcast(session_id, payload)
                yield event

            # After the first send completes, announce the session with the real ID
            if session_id in self._pending_announce:
                self._pending_announce.discard(session_id)
                await self._notify_watchers({
                    "type": "agent_session_opened",
                    "session_id": session_id,
                })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rekey(self, old_id: str, new_id: str) -> None:
        """Re-key internal maps when the SDK assigns a new session ID."""
        logger.info("Pool re-keying session %s → %s", old_id, new_id)

        sm = self._sessions.pop(old_id, None)
        if sm is not None:
            self._sessions[new_id] = sm

        subs = self._subscribers.pop(old_id, None)
        if subs is not None:
            self._subscribers[new_id] = subs

        lock = self._locks.pop(old_id, None)
        if lock is not None:
            self._locks[new_id] = lock

        if old_id in self._pending_announce:
            self._pending_announce.discard(old_id)
            self._pending_announce.add(new_id)

    async def _broadcast(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        exclude: WebSocket | None = None,
    ) -> None:
        """Send a JSON payload to all subscribers of a session."""
        subs = self._subscribers.get(session_id)
        if not subs:
            return
        data = orjson.dumps(payload)
        dead: list[WebSocket] = []
        for ws in subs:
            if ws is exclude:
                continue
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            subs.discard(ws)

    async def _notify_watchers(self, payload: dict[str, Any]) -> None:
        """Send a notification to all watchers."""
        data = orjson.dumps(payload)
        dead: list[WebSocket] = []
        for ws in self._watchers:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._watchers.discard(ws)
