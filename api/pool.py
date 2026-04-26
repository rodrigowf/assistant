"""SessionPool — shared pool of Claude Code sessions with event broadcast.

The pool manages both regular agent sessions (SessionManager) and the single
orchestrator session (OrchestratorSession). All session state lives here;
there is no separate OrchestratorConnectionManager.

Key design:
- Sessions are keyed by a stable **local_id** (UUID from the frontend) that
  never changes across reconnects or backend restarts.
- Regular sessions (SessionManager) support multiple concurrent WebSocket
  subscribers via subscribe/unsubscribe.
- The orchestrator session is stored separately but uses the same subscriber
  infrastructure. At most one orchestrator can be active at a time.
- Watchers receive notifications when agent sessions are opened or closed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid as _uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
from starlette.websockets import WebSocket, WebSocketState

from api.serializers import serialize_event
from manager.config import ManagerConfig
from manager.session import SessionManager
from manager.types import Event

logger = logging.getLogger(__name__)


class SessionPool:
    """Unified pool for agent and orchestrator sessions."""

    def __init__(self) -> None:
        # Regular agent sessions
        self._sessions: dict[str, SessionManager] = {}
        self._subscribers: dict[str, set[WebSocket]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

        # Per-remote-host create() serialization. When N concurrent callers
        # try to spawn sessions on the same SSH host, we serialize them here
        # so (a) the ControlMaster socket is established by the first call
        # before the rest rush in, and (b) a transient SSH failure doesn't
        # get amplified by a parallel retry storm.  Local (non-SSH) sessions
        # bypass this lock entirely — they have no shared resource to guard.
        self._host_create_locks: dict[str, asyncio.Lock] = {}

        # Single orchestrator session
        self._orchestrator: Any | None = None  # OrchestratorSession
        self._orchestrator_id: str | None = None
        self._orchestrator_subs: set[WebSocket] = set()

        # Watchers: receive agent_session_opened / agent_session_closed events
        self._watchers: set[WebSocket] = set()

    # ------------------------------------------------------------------
    # Agent session lifecycle
    # ------------------------------------------------------------------

    def find_by_sdk_id(self, sdk_session_id: str) -> str | None:
        """Return the local_id of a pool session with the given SDK session ID, or None."""
        for lid, sm in self._sessions.items():
            if sm.sdk_session_id == sdk_session_id:
                return lid
        return None

    async def create(
        self,
        config: ManagerConfig,
        local_id: str | None = None,
        resume_sdk_id: str | None = None,
        fork: bool = False,
        mcp_servers: dict[str, dict] | None = None,
    ) -> str:
        """Create, start, and register a SessionManager. Returns the stable local_id.

        If *resume_sdk_id* is given and a session with that SDK ID is already
        in the pool **and healthy**, return the existing local_id instead of
        creating a duplicate.

        Args:
            config: Manager configuration.
            local_id: Stable frontend tab UUID.
            resume_sdk_id: SDK session ID for resuming.
            fork: Whether to fork from an existing session.
            mcp_servers: Optional dict of MCP servers to load. If provided, overrides
                         the mcp_servers in config.
        """
        # Serialize concurrent create()s that target the same remote SSH host.
        # This prevents a stampede of simultaneous SSH handshakes (e.g. from a
        # browser reconnect storm) that historically triggered session churn
        # on the remote host.  Local sessions skip the lock since there is no
        # shared SSH resource to protect.
        if config.ssh_host:
            lock = self._host_create_locks.setdefault(config.ssh_host, asyncio.Lock())
            async with lock:
                return await self._do_create(
                    config, local_id, resume_sdk_id, fork, mcp_servers
                )
        return await self._do_create(
            config, local_id, resume_sdk_id, fork, mcp_servers
        )

    async def _do_create(
        self,
        config: ManagerConfig,
        local_id: str | None,
        resume_sdk_id: str | None,
        fork: bool,
        mcp_servers: dict[str, dict] | None,
    ) -> str:
        """Inner body of create(), executed under the per-host lock if SSH."""
        # Deduplicate: reuse an existing pool session with the same SDK ID.
        # Crucially this runs *inside* the host lock, so if two reconnects
        # both try to spawn the same session the first wins and the second
        # finds it already present.
        if resume_sdk_id and not fork:
            existing = self.find_by_sdk_id(resume_sdk_id)
            if existing:
                sm = self._sessions[existing]
                if sm.is_active:
                    return existing
                # Existing session is dead — clean it up and fall through
                # to create a fresh one.
                logger.info("Replacing dead session %s (status=%s)", existing, sm.status.value)
                self._sessions.pop(existing, None)
                self._subscribers.pop(existing, None)
                self._locks.pop(existing, None)

        lid = local_id or str(_uuid.uuid4())

        # If mcp_servers provided, create a new config with that override
        if mcp_servers is not None:
            from dataclasses import replace
            config = replace(config, mcp_servers=mcp_servers)

        sm = SessionManager(
            session_id=resume_sdk_id,
            local_id=lid,
            fork=fork,
            config=config,
        )
        try:
            await sm.start()
        except Exception as e:
            # ProcessError stores the CLI stderr in e.stderr, not in str(e).
            # Check both so we catch it regardless of SDK version.
            stderr = getattr(e, "stderr", None) or ""
            if resume_sdk_id and "No conversation found" in (str(e) + stderr):
                # The SDK state for this session ID no longer exists (e.g. after a
                # server restart).  Fall back to starting a fresh session so the
                # frontend can continue working instead of showing an error.
                # Back off briefly before the retry so a *transient* remote
                # failure — which also surfaces as "No conversation found" when
                # the SSH wrapper couldn't reach the remote claude — doesn't
                # get hammered.  One retry only; if that also fails, surface
                # the error to the caller instead of looping.
                logger.warning(
                    "Resume SDK ID %s not found in Claude state; starting fresh session",
                    resume_sdk_id,
                )
                await asyncio.sleep(0.5 + random.random() * 0.5)
                sm = SessionManager(
                    session_id=None,
                    local_id=lid,
                    fork=False,
                    config=config,
                )
                await sm.start()
            else:
                raise

        self._sessions[lid] = sm
        self._subscribers[lid] = set()
        self._locks[lid] = asyncio.Lock()

        await self._notify_watchers({
            "type": "agent_session_opened",
            "session_id": lid,
            "sdk_session_id": sm.sdk_session_id,
        })

        return lid

    async def close(self, session_id: str) -> None:
        """Remove a session, notify subscribers, and clean up.

        Awaits ``sm.stop()`` so the SDK transport, the local ssh client (for
        remote sessions), and the remote ``claude`` process all shut down
        deterministically.  Relying on Python GC is not enough: GC cannot run
        async cleanup, so the subprocess + SSH connection + remote children
        would otherwise leak across close/reopen cycles.
        """
        sm = self._sessions.pop(session_id, None)
        if sm is None:
            return

        # Notify while subscribers/watchers are still registered
        await self._broadcast_session(session_id, {"type": "session_stopped"})
        await self._notify_watchers({"type": "agent_session_closed", "session_id": session_id})

        self._subscribers.pop(session_id, None)
        self._locks.pop(session_id, None)

        try:
            # Bound the wait so a misbehaving SDK transport (e.g. a remote
            # ssh that won't close) can't hang the close request.  After the
            # timeout the SessionManager is dropped anyway; the worst case
            # is one orphaned ssh that the OS reaps when its parent exits.
            await asyncio.wait_for(sm.stop(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("SessionManager %s did not stop within 10s; abandoning", session_id)
        except Exception:
            logger.exception("Error stopping SessionManager %s during close", session_id)

    async def interrupt(self, session_id: str) -> None:
        """Interrupt the current response for a session."""
        sm = self._sessions.get(session_id)
        if sm is not None:
            await sm.interrupt()

    # ------------------------------------------------------------------
    # Agent session access
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> SessionManager | None:
        return self._sessions.get(session_id)

    def has(self, session_id: str) -> bool:
        return session_id in self._sessions

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": lid,
                "sdk_session_id": sm.sdk_session_id,
                "status": sm.status.value,
                "cost": sm.cost,
                "turns": sm.turns,
            }
            for lid, sm in self._sessions.items()
        ]

    # ------------------------------------------------------------------
    # Agent session subscribers
    # ------------------------------------------------------------------

    def subscribe(self, session_id: str, ws: WebSocket) -> None:
        subs = self._subscribers.get(session_id)
        if subs is not None:
            subs.add(ws)

    def unsubscribe(self, session_id: str, ws: WebSocket) -> None:
        subs = self._subscribers.get(session_id)
        if subs is not None:
            subs.discard(ws)

    def subscriber_count(self, session_id: str) -> int:
        subs = self._subscribers.get(session_id)
        return len(subs) if subs else 0

    # ------------------------------------------------------------------
    # Orchestrator session lifecycle
    # ------------------------------------------------------------------

    def has_orchestrator(self) -> bool:
        return self._orchestrator is not None

    @property
    def orchestrator_id(self) -> str | None:
        return self._orchestrator_id

    def get_orchestrator(self) -> Any | None:
        """Return the active OrchestratorSession, or None."""
        return self._orchestrator

    def set_orchestrator(self, session_id: str, session: Any) -> None:
        """Register a freshly-started OrchestratorSession."""
        self._orchestrator = session
        self._orchestrator_id = session_id
        self._orchestrator_subs = set()

    def subscribe_orchestrator(self, session_id: str, ws: WebSocket) -> bool:
        """Add a WebSocket subscriber to the active orchestrator.

        Returns True if subscribed, False if no active session or ID mismatch.
        """
        if self._orchestrator is None or self._orchestrator_id != session_id:
            return False
        self._orchestrator_subs.add(ws)
        return True

    def unsubscribe_orchestrator(self, ws: WebSocket) -> None:
        self._orchestrator_subs.discard(ws)

    async def broadcast_orchestrator(self, payload: dict) -> None:
        """Broadcast a payload to all orchestrator subscribers."""
        if not self._orchestrator_subs:
            return
        data = orjson.dumps(payload)
        dead: list[WebSocket] = []
        for ws in self._orchestrator_subs:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._orchestrator_subs.discard(ws)

    async def stop_orchestrator(self) -> None:
        """Stop and clear the active orchestrator session."""
        session = self._orchestrator
        self._orchestrator = None
        self._orchestrator_id = None
        self._orchestrator_subs.clear()
        if session is not None and hasattr(session, "stop"):
            try:
                await session.stop()
            except Exception:
                pass

    async def close_all(self) -> None:
        """Stop every active session in the pool. Used at app shutdown so
        SDK subprocesses (and the remote ssh+claude they spawn) don't leak
        across backend restarts."""
        for sid in list(self._sessions.keys()):
            try:
                await self.close(sid)
            except Exception:
                logger.exception("Error closing session %s during shutdown", sid)
        try:
            await self.stop_orchestrator()
        except Exception:
            logger.exception("Error stopping orchestrator during shutdown")

    @property
    def orchestrator_subscriber_count(self) -> int:
        return len(self._orchestrator_subs)

    # ------------------------------------------------------------------
    # Watchers (receive new-session notifications)
    # ------------------------------------------------------------------

    def watch(self, ws: WebSocket) -> None:
        self._watchers.add(ws)

    def unwatch(self, ws: WebSocket) -> None:
        self._watchers.discard(ws)

    # ------------------------------------------------------------------
    # Sending messages (agent sessions, with lock + broadcast)
    # ------------------------------------------------------------------

    async def send(
        self,
        session_id: str,
        text: str,
        *,
        source_ws: WebSocket | None = None,
    ) -> AsyncIterator[Event]:
        """Drive sm.send() with per-session lock, broadcasting to all subscribers."""
        sm = self._sessions.get(session_id)
        if sm is None:
            raise ValueError(f"No session with ID {session_id}")

        lock = self._locks[session_id]

        async with lock:
            await self._broadcast_session(
                session_id,
                {"type": "user_message", "text": text},
                exclude=source_ws,
            )
            async for event in sm.send(text):
                payload = serialize_event(event)
                await self._broadcast_session(session_id, payload)
                yield event

    async def compact(self, session_id: str) -> AsyncIterator[Event]:
        """Trigger compaction with per-session lock, broadcasting to all subscribers."""
        sm = self._sessions.get(session_id)
        if sm is None:
            raise ValueError(f"No session with ID {session_id}")

        lock = self._locks[session_id]

        async with lock:
            async for event in sm.compact():
                payload = serialize_event(event)
                await self._broadcast_session(session_id, payload)
                yield event

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _broadcast_session(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        exclude: WebSocket | None = None,
    ) -> None:
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
