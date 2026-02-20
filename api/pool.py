"""SessionPool — shared pool of Claude Code sessions with event broadcast."""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
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

    Sessions are keyed by a stable **local ID** (UUID) that never changes.
    The Claude Code SDK session ID is stored as an attribute on the
    ``SessionManager`` and used only for resume operations and JSONL lookups.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionManager] = {}
        self._subscribers: dict[str, set[WebSocket]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._watchers: set[WebSocket] = set()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create(
        self,
        config: ManagerConfig,
        local_id: str | None = None,
        resume_sdk_id: str | None = None,
        fork: bool = False,
    ) -> str:
        """Create, start, and register a SessionManager. Returns the stable local_id.

        The session is announced to watchers immediately — the local_id is
        stable and will never change.
        """
        lid = local_id or str(_uuid.uuid4())
        sm = SessionManager(
            session_id=resume_sdk_id,
            local_id=lid,
            fork=fork,
            config=config,
        )
        await sm.start()

        self._sessions[lid] = sm
        self._subscribers[lid] = set()
        self._locks[lid] = asyncio.Lock()

        # Announce immediately — local_id is stable from creation.
        # Include sdk_session_id so the frontend can load history for resumed sessions.
        await self._notify_watchers({
            "type": "agent_session_opened",
            "session_id": lid,
            "sdk_session_id": sm.sdk_session_id,
        })

        return lid

    async def close(self, session_id: str) -> None:
        """Stop a session, notify subscribers, and clean up."""
        sm = self._sessions.pop(session_id, None)
        if sm is None:
            return

        # Notify while subscribers/watchers are still registered
        await self._broadcast(session_id, {"type": "session_stopped"})
        await self._notify_watchers({"type": "agent_session_closed", "session_id": session_id})

        self._subscribers.pop(session_id, None)
        self._locks.pop(session_id, None)

        # sm.stop() uses anyio cancel scopes that must exit in the same task
        # they were entered — calling it from any other task raises RuntimeError
        # and propagates CancelledError through the ASGI stack, killing other
        # WebSocket handlers. The session is already removed from the pool so
        # no new work will reach it; the SDK subprocess exits when the client
        # is garbage-collected.

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
        for lid, sm in self._sessions.items():
            result.append({
                "session_id": lid,
                "sdk_session_id": sm.sdk_session_id,
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

        The session_id is always the stable local_id — it never changes.
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
                payload = serialize_event(event)
                await self._broadcast(session_id, payload)
                yield event

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
