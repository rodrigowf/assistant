"""SessionPool — shared pool of agent sessions with event broadcast.

The pool manages both regular agent sessions (provider-specific
BaseSessionManager implementations) and the single orchestrator session
(OrchestratorSession). All session state lives here; there is no
separate OrchestratorConnectionManager.

Key design:
- Sessions are keyed by a stable **local_id** (UUID from the frontend) that
  never changes across reconnects or backend restarts.
- Regular sessions support multiple concurrent WebSocket subscribers via
  subscribe/unsubscribe.
- The orchestrator session is stored separately but uses the same subscriber
  infrastructure. At most one orchestrator can be active at a time.
- Watchers receive notifications when agent sessions are opened or closed.
- The provider (Claude vs Qwen) is selected per session based on
  ``ManagerConfig.provider``; pool internals stay provider-agnostic.
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
from manager._proc import process_alive as _process_alive, looks_like
from manager.base_session import BaseSessionManager
from manager.config import ManagerConfig
from manager.types import Event


def _session_manager_for(config: ManagerConfig, **kwargs) -> BaseSessionManager:
    """Factory: build the right session manager for the configured provider.

    Resolution goes through :mod:`manager.registry` so the pool itself
    knows nothing about which harnesses exist — adding a fourth is a
    spec registration plus a new ``manager.<x>_adapter`` module.  The
    spec's ``session_class_loader`` is lazy, so importing the pool
    doesn't drag in claude-agent-sdk on a Qwen-only host (or vice-versa).
    """
    from manager.registry import ensure_all_registered, get_registry
    ensure_all_registered()
    registry = get_registry()
    provider = (config.provider or "").lower()
    if not provider:
        # No provider pinned on the config (legacy default).  Fall back to
        # the first registered harness so the pool stays deterministic.
        # Registration order in :mod:`manager.registry._ADAPTER_MODULES`
        # is the source of truth.
        names = registry.names()
        if not names:
            raise RuntimeError("No session harnesses registered")
        provider = names[0]
    spec = registry.require(provider)
    session_class = spec.session_class_loader()
    return session_class(config=config, **kwargs)


def _kill_tracked_pid(pid: int) -> bool:
    """Force-kill a tracked subprocess PID, dispatching via the harness
    registry.

    Used by the orphan reaper.  Every registered harness contributes a
    ``comm_prefix`` and a ``kill_helper_loader``; we look up the helper by
    the live ``/proc/<pid>/comm`` prefix.  Unknown comm prefixes are left
    alone — the reaper only acts on PIDs that still look like ones we
    spawned.

    Iteration order is registration order, so if two specs share a comm
    prefix (e.g. multiple Node-based harnesses) the first registered
    wins.  That's fine for the orphan-reaper case: any kill helper that
    survived the per-spec comm check is safe to call.
    """
    from manager.registry import ensure_all_registered, get_registry
    ensure_all_registered()
    for spec in get_registry().all().values():
        if looks_like(pid, spec.comm_prefix):
            return spec.kill_helper_loader()(pid)
    return False

logger = logging.getLogger(__name__)


class SessionPool:
    """Unified pool for agent and orchestrator sessions."""

    def __init__(self) -> None:
        # Regular agent sessions
        self._sessions: dict[str, BaseSessionManager] = {}
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

        # Orchestrator session currently being torn down. Parked here for
        # the duration of stop_orchestrator() so a concurrent voice_start
        # for the same local_id can await it via await_orchestrator_stop
        # instead of reconnecting into the husk. Cleared after stop() returns.
        self._stopping_orchestrator: Any | None = None
        self._stopping_orchestrator_id: str | None = None

        # Watchers: receive agent_session_opened / agent_session_closed events
        self._watchers: set[WebSocket] = set()

        # Belt-and-braces: PIDs of every bundled-claude subprocess we ever
        # spawned, mapped to a (session_id, first_seen_at) tuple.  The
        # reaper task scans this periodically and SIGKILLs any pid whose
        # owning session has been gone from the pool for more than the
        # grace period — covers the case where the per-session SIGKILL
        # path inside _lifecycle() was itself bypassed (lifecycle task
        # cancelled hard, SDK transport refactored so we couldn't grab
        # the pid, etc.).  See manager.claude.session.kill_claude_subprocess.
        self._tracked_pids: dict[int, tuple[str, float]] = {}
        # Sessions that have been removed from _sessions but whose pid we
        # still want to keep an eye on for ``orphan_grace_seconds``.
        self._closed_session_pids: dict[str, tuple[int, float]] = {}
        self._reaper_task: asyncio.Task[None] | None = None

        # Session-owned in-flight turn task.  Decouples turn lifetime from
        # the WebSocket that initiated it: a page reload (or any momentary
        # disconnect) merely unsubscribes from broadcasts; the task keeps
        # running and the new WS picks up the stream when it subscribes.
        # Keyed by session_id; absent / done means "no turn running".
        # See start_turn() / cancel_turn() for the public API.
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}

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

        sm = _session_manager_for(
            config,
            session_id=resume_sdk_id,
            local_id=lid,
            fork=fork,
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
                    "Resume SDK ID %s not found; starting fresh session",
                    resume_sdk_id,
                )
                await asyncio.sleep(0.5 + random.random() * 0.5)
                sm = _session_manager_for(
                    config,
                    session_id=None,
                    local_id=lid,
                    fork=False,
                )
                await sm.start()
            else:
                raise

        self._sessions[lid] = sm
        self._subscribers[lid] = set()
        self._locks[lid] = asyncio.Lock()
        # Track the session subprocess pid for the orphan reaper.
        #
        # Two paths converge here:
        #
        #   Claude: ``sm.subprocess_pid`` returns the bundled-claude PID
        #   that was captured at SDK connect time.  Stable for the life
        #   of the session, so a single insert here is enough.
        #
        #   Qwen: spawns a fresh subprocess per turn, so ``subprocess_pid``
        #   only returns a value while a turn is mid-flight (typically
        #   ``None`` at this point).  Instead, install spawn/exit
        #   callbacks the QwenSessionManager invokes around each turn —
        #   keeps tracking in sync with the actual subprocess lifetime.
        import time as _time
        pid = sm.subprocess_pid
        if pid is not None:
            self._tracked_pids[pid] = (lid, _time.monotonic())

        def _on_pid_spawn(spawned_pid: int, _lid: str = lid) -> None:
            self._tracked_pids[spawned_pid] = (_lid, _time.monotonic())

        def _on_pid_exit(exited_pid: int) -> None:
            self._tracked_pids.pop(exited_pid, None)

        sm.set_pid_callbacks(_on_pid_spawn, _on_pid_exit)

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

        # Cancel any in-flight session-owned turn before tearing down the SDK
        # client — otherwise the driver task continues iterating sm.send()
        # against a half-disconnected session and emits scary tracebacks.
        turn_task = self._turn_tasks.pop(session_id, None)
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except (asyncio.CancelledError, Exception):
                pass

        # Notify while subscribers/watchers are still registered
        await self._broadcast_session(session_id, {"type": "session_stopped"})
        await self._notify_watchers({"type": "agent_session_closed", "session_id": session_id})

        self._subscribers.pop(session_id, None)
        self._locks.pop(session_id, None)

        # Hand the pid(s) off to the closed-session shadow map so the
        # reaper has a grace window to verify the subprocess actually
        # exits.  If sm.stop() (which calls our SIGKILL fallback)
        # succeeds, the pid will be gone by the time the reaper looks
        # at it — no-op.  Otherwise the reaper escalates after
        # orphan_grace_seconds.
        #
        # Detach the spawn callback first so a Qwen session can't push
        # more PIDs after we've started tearing down.  The exit callback
        # stays attached briefly so any PID exit during stop() still
        # cleans up the tracking entry.
        sm.set_pid_callbacks(None, None)
        import time as _time
        now = _time.monotonic()
        pid = sm.subprocess_pid
        if pid is not None:
            self._closed_session_pids[session_id] = (pid, now)
            self._tracked_pids.pop(pid, None)
        # Sweep any other tracked PIDs that still belong to this session
        # (Qwen turns leave the spawn callback's entries behind even if
        # the per-turn cleanup ran — defensive cleanup, no signals sent).
        for tracked_pid, (owner_lid, _seen) in list(self._tracked_pids.items()):
            if owner_lid == session_id:
                self._closed_session_pids.setdefault(session_id, (tracked_pid, now))
                self._tracked_pids.pop(tracked_pid, None)

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

    async def resolve_session_permission(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        *,
        message: str | None = None,
        responder: str = "user",
    ) -> bool:
        """Resolve a pending permission request for a session.

        Returns True if this call won the race (and the SDK was unblocked),
        False if the request was already answered or doesn't exist.  When the
        orchestrator answers first the UI's later call is a no-op — the
        ``permission_resolved`` event the manager emits already tells the UI
        to close its modal.
        """
        sm = self._sessions.get(session_id)
        if sm is None:
            return False
        return sm.resolve_permission(
            request_id, decision, message=message, responder=responder,
        )

    # ------------------------------------------------------------------
    # Agent session access
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> BaseSessionManager | None:
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
                "provider": sm.provider_name,
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
        """Broadcast a payload to all orchestrator subscribers.

        Iterates over a snapshot because ``await ws.send_bytes`` yields and
        concurrent (un)subscribers would otherwise mutate the set mid-iteration
        ("Set changed size during iteration"), tearing down the voice relay.
        """
        if not self._orchestrator_subs:
            return
        data = orjson.dumps(payload)
        dead: list[WebSocket] = []
        for ws in tuple(self._orchestrator_subs):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._orchestrator_subs.discard(ws)

    async def stop_orchestrator(self) -> None:
        """Stop and clear the active orchestrator session.

        Moves the session into ``_stopping_orchestrator`` for the
        duration of ``session.stop()`` so a concurrent ``voice_start``
        for the same ``local_id`` can await the stop instead of racing
        a dying session (see :meth:`await_orchestrator_stop`). After
        ``session.stop()`` returns, the stopping slot is cleared.
        """
        session = self._orchestrator
        local_id = self._orchestrator_id
        self._orchestrator = None
        self._orchestrator_id = None
        self._orchestrator_subs.clear()
        if session is None or not hasattr(session, "stop"):
            return
        # Park the session in the stopping slot so a concurrent start
        # for the same local_id can wait it out cleanly.
        self._stopping_orchestrator = session
        self._stopping_orchestrator_id = local_id
        try:
            await session.stop()
        except Exception:
            logger.exception("orchestrator session.stop() raised")
        finally:
            # Release the stopping slot only if it's still us — defensive
            # against future code that might park multiple sessions.
            if self._stopping_orchestrator is session:
                self._stopping_orchestrator = None
                self._stopping_orchestrator_id = None

    async def await_orchestrator_stop(
        self,
        local_id: str,
        timeout: float = 5.0,
    ) -> bool:
        """Wait for an in-flight teardown of ``local_id`` to finish.

        Returns True if a teardown was in flight and completed (or there
        was nothing to wait for). Returns False if it timed out — the
        caller can choose to reject the new start with an error, since a
        slot that won't release is a bug worth surfacing rather than
        silently overwriting.

        Used by ``_handle_start`` so that a ``voice_start`` arriving
        during the ENDING window for the same ``local_id`` is held
        until the prior session is fully gone, then served a fresh
        session instead of reconnecting into the husk.
        """
        if self._stopping_orchestrator_id != local_id:
            return True
        session = self._stopping_orchestrator
        if session is None:
            return True
        ended_event = getattr(session, "_voice_ended", None)
        if ended_event is None:
            # Pre-refactor session (no state machine) — fall back to a
            # polling wait on the slot itself.
            import time as _time
            deadline = _time.monotonic() + timeout
            while self._stopping_orchestrator_id == local_id:
                if _time.monotonic() > deadline:
                    return False
                await asyncio.sleep(0.05)
            return True
        try:
            await asyncio.wait_for(ended_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        # Give stop_orchestrator() the moment to clear the slot.
        for _ in range(10):
            if self._stopping_orchestrator_id != local_id:
                return True
            await asyncio.sleep(0.02)
        return self._stopping_orchestrator_id != local_id

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

    # ------------------------------------------------------------------
    # Orphan reaper — last-line defense for leaked claude subprocesses
    # ------------------------------------------------------------------

    async def start_orphan_reaper(
        self,
        *,
        interval_seconds: float = 30.0,
        orphan_grace_seconds: float = 30.0,
    ) -> None:
        """Spawn the background task that nukes orphaned `claude` subprocesses.

        Runs every *interval_seconds* and force-kills any tracked pid whose
        owning session has been gone from the pool for *orphan_grace_seconds*.
        Idempotent — safe to call multiple times; the second call no-ops.

        The grace period gives the per-session SIGKILL path inside
        SessionManager._lifecycle a chance to do the cleanup itself.  The
        reaper only acts when that path failed (lifecycle task cancelled,
        SDK refactored, etc.) — in steady state it observes that every
        previously-tracked pid is already gone and just trims bookkeeping.
        """
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(
            self._reaper_loop(interval_seconds, orphan_grace_seconds),
            name="pool-orphan-reaper",
        )

    async def stop_orphan_reaper(self) -> None:
        if self._reaper_task is None:
            return
        self._reaper_task.cancel()
        try:
            await self._reaper_task
        except (asyncio.CancelledError, Exception):
            pass
        self._reaper_task = None

    async def _reaper_loop(
        self, interval_seconds: float, orphan_grace_seconds: float
    ) -> None:
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._reap_orphans_once, orphan_grace_seconds
                    )
                except Exception:
                    logger.exception("orphan reaper iteration failed")
        except asyncio.CancelledError:
            raise

    def _reap_orphans_once(self, orphan_grace_seconds: float) -> None:
        """One pass of the orphan reaper.  Synchronous so it can run in a
        thread executor — the underlying kill helpers do sleep() polls.

        Provider-agnostic: ``_kill_tracked_pid`` dispatches by /proc comm
        prefix, so the same reaper works for both Claude and Qwen
        subprocesses.  Unrecognized comm prefixes are left alone (we never
        signal a PID that doesn't look like one of ours).
        """
        import time as _time
        now = _time.monotonic()

        # Pass 1: prune tracked pids that are already dead (process exited
        # normally — bookkeeping cleanup, no signals sent).
        dead_pids = [pid for pid in self._tracked_pids if not _process_alive(pid)]
        for pid in dead_pids:
            self._tracked_pids.pop(pid, None)

        # Pass 2: closed sessions whose grace period expired.
        expired = [
            sid
            for sid, (_pid, closed_at) in self._closed_session_pids.items()
            if (now - closed_at) >= orphan_grace_seconds
        ]
        for sid in expired:
            pid, _closed_at = self._closed_session_pids.pop(sid)
            if not _process_alive(pid):
                continue
            if _kill_tracked_pid(pid):
                logger.warning(
                    "Orphan reaper: killed leaked subprocess pid=%d "
                    "(session %s closed %.0fs ago)",
                    pid, sid, now - _closed_at,
                )

        # Pass 3 (paranoid): a session is *still* in the pool but its
        # claimed pid no longer matches a live process — likely the
        # subprocess crashed and we never noticed.  Mark the session as
        # dead so the next operation triggers a fresh start.
        for pid, (sid, _seen_at) in list(self._tracked_pids.items()):
            if sid not in self._sessions:
                # Session vanished without going through close() — should
                # be rare but possible if a test or code path popped it
                # directly.  Treat the pid as orphaned.
                self._tracked_pids.pop(pid, None)
                if _process_alive(pid) and _kill_tracked_pid(pid):
                    logger.warning(
                        "Orphan reaper: killed pid=%d for vanished "
                        "session %s (no close() recorded)",
                        pid, sid,
                    )

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
                payload = self._wrap_payload(sm, serialize_event(event))
                await self._broadcast_session(session_id, payload)
                if payload.get("type") in ("permission_request", "permission_resolved"):
                    # Mirror to the orchestrator so its UI can show a matching
                    # banner and (for permission_request) so the orchestrator
                    # agent can respond programmatically.  Same envelope as
                    # nested_session_event so existing dispatch logic fits.
                    await self.broadcast_orchestrator({
                        "type": "nested_session_event",
                        "session_id": session_id,
                        "event_type": payload["type"],
                        "event_data": payload,
                    })
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
    # Session-owned turn API (chat.py uses this; orchestrator runner
    # has its own task management on top of pool.send()).
    # ------------------------------------------------------------------

    def has_active_turn(self, session_id: str) -> bool:
        """True if a session-owned turn task is currently running."""
        task = self._turn_tasks.get(session_id)
        return task is not None and not task.done()

    async def start_turn(
        self,
        session_id: str,
        text: str,
        *,
        source_ws: WebSocket | None = None,
    ) -> None:
        """Spawn a session-owned task that drives the turn to completion.

        The task is owned by the pool, NOT by the caller's task.  This is
        the key invariant: the turn outlives the WebSocket that sent the
        prompt, so a page reload (or any transient disconnect) doesn't
        kill the in-flight work.  Subscribers (current and future) receive
        events via ``_broadcast_session`` as they're produced.

        If a turn is already in flight on this session, it is cancelled
        first ("interrupt + new" semantics — the bundled CLI doesn't
        accept overlapping queries, so the new prompt always supersedes
        the old).  This makes start_turn safe to call without a
        prior cancel_turn, and atomic against the rare race where two
        WSes send prompts to the same session simultaneously.

        Returns once the task is created.  Errors during the turn are
        logged and broadcast to subscribers; they do NOT propagate to
        the caller.
        """
        if session_id not in self._sessions:
            raise ValueError(f"No session with ID {session_id}")

        # Atomic interrupt + spawn.  If another caller is mid-cancel_turn,
        # the second cancel_turn here is a no-op (task already done) so
        # this remains correct under concurrent calls.
        await self.cancel_turn(session_id)

        task = asyncio.create_task(
            self._drive_turn(session_id, text, source_ws),
            name=f"turn-{session_id[:8]}",
        )
        self._turn_tasks[session_id] = task

    async def _drive_turn(
        self,
        session_id: str,
        text: str,
        source_ws: WebSocket | None,
    ) -> None:
        """Body of the session-owned turn task.

        Iterates ``self.send()`` (the existing async-gen API that handles
        per-session locking, broadcasting, and orchestrator mirroring) and
        catches everything so a stray exception doesn't show up as
        ``Task exception was never retrieved`` in the logs.  Subscribers
        already see the events via the broadcast inside ``send()``; this
        loop just consumes the iterator to completion.

        Owns the TurnAbandoned retry: if the upstream request never
        produced any messages (TCP path silently wedged), interrupt the
        wedged turn and retry once.  This logic used to live in
        ``api.routes.chat._handle_send`` — moved here because the WS task
        no longer owns the iteration and so couldn't observe the exception.

        ``TurnAbandoned`` is the provider-agnostic base for both Claude's
        ``SessionAbandoned`` and Qwen's ``QwenAbandoned``, so a single
        except clause covers both providers without forcing either SDK
        to be installed at module load time.

        The reason we don't surface exceptions to a caller is that THERE
        IS NO CALLER once the turn starts — the WebSocket that initiated
        it is just one of N possible subscribers, and may already be gone
        by the time the turn finishes.  Broadcast and logs are the right
        channels for telling everyone what happened.
        """
        from manager.base_session import TurnAbandoned

        async def _stream_once() -> None:
            async for _event in self.send(session_id, text, source_ws=source_ws):
                pass

        try:
            try:
                await _stream_once()
            except TurnAbandoned as exc:
                logger.warning(
                    "Turn abandoned for session %s after %.0fs; retrying once",
                    session_id, exc.elapsed_seconds,
                )
                await self._broadcast_session(session_id, {
                    "type": "status", "status": "retrying",
                    "detail": f"upstream silent for {exc.elapsed_seconds:.0f}s, retrying",
                })
                try:
                    await self.interrupt(session_id)
                except Exception:
                    logger.exception("Failed to interrupt abandoned turn for %s", session_id)
                await asyncio.sleep(1.0)
                await _stream_once()
        except asyncio.CancelledError:
            raise
        except TurnAbandoned as exc:
            await self._broadcast_session(session_id, {
                "type": "error", "error": "upstream_wedged",
                "detail": (
                    f"Upstream did not respond after retry "
                    f"({exc.elapsed_seconds:.0f}s). Try again in a moment."
                ),
            })
        except Exception as exc:
            logger.exception(
                "Session-owned turn for %s raised; broadcasting error",
                session_id,
            )
            try:
                await self._broadcast_session(session_id, {
                    "type": "error", "error": "send_failed",
                    "detail": str(exc),
                })
            except Exception:
                logger.exception("Failed to broadcast send_failed for %s", session_id)

    async def cancel_turn(self, session_id: str) -> bool:
        """Stop the in-flight turn for *session_id*.

        Sends an SDK interrupt (so the bundled ``claude`` subprocess
        actually halts the current request — cancelling the asyncio task
        alone wouldn't reach across the process boundary), then awaits
        the task so the caller knows the turn has fully unwound (lock
        released, drain task torn down, finally blocks run) before they
        proceed.  Returns True if a turn was actually cancelled, False
        if no turn was running.
        """
        task = self._turn_tasks.get(session_id)
        if task is None or task.done():
            return False

        # Issue the SDK interrupt FIRST.  That's the proper way to stop
        # the bundled-claude turn; cancelling our reader task alone leaves
        # the subprocess still working and emitting messages into a
        # buffer no one is reading.
        try:
            await self.interrupt(session_id)
        except Exception:
            logger.exception("interrupt() failed during cancel_turn for %s", session_id)

        # Now cancel the driver task and wait for it to unwind.
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return True

    # ------------------------------------------------------------------
    # Resume protocol — wrap broadcasts with (seq, stream_id) so a
    # reconnecting WS can resume from a checkpoint without losing events.
    # See ``manager/claude/session.py``'s ``replay_after`` for the matching
    # backend logic and ``frontend/src/utils/checkpoint.ts`` for the client.
    # ------------------------------------------------------------------

    def _wrap_payload(
        self,
        sm: BaseSessionManager,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach resume-protocol metadata to a broadcast payload.

        Reads the seq the session just yielded (set inside ``sm.send`` in
        the same coroutine — no await intervenes) and the session's
        current stream id.  Either may be missing for providers that
        don't support the protocol (Qwen, Gemini); in that case the
        payload is returned unchanged and the frontend treats this
        session as non-resumable (the protocol is purely additive).
        """
        stream_id = getattr(sm, "stream_id", None)
        seq = getattr(sm, "last_yielded_seq", None)
        if stream_id is None or seq is None:
            return payload
        # Don't overwrite if a caller already filled these (defensive —
        # the replay path stamps its own seqs in :meth:`replay_for_subscriber`).
        payload.setdefault("seq", seq)
        payload.setdefault("stream_id", stream_id)
        return payload

    def resume_state_for(self, session_id: str) -> dict[str, Any] | None:
        """Snapshot of a session's resume-protocol state, or None.

        ``{"stream_id": str, "next_seq": int}``.  Returned to the
        frontend in ``session_started`` so a fresh subscriber (no
        prior checkpoint) immediately learns the stream identity and
        can start tracking seqs from this point forward.
        """
        sm = self._sessions.get(session_id)
        if sm is None:
            return None
        stream_id = getattr(sm, "stream_id", None)
        if stream_id is None:
            return None
        # ``_next_seq`` is the seq the *next* dispatch will use; the
        # last delivered seq is one less.  Hand the frontend the
        # next-seq directly — it represents "the boundary above which
        # nothing has been delivered yet", which is the right thing
        # to compare future seqs against.
        next_seq = getattr(sm, "_next_seq", 0)
        return {"stream_id": stream_id, "next_seq": next_seq}

    def replay_for_subscriber(
        self,
        session_id: str,
        resume_from: dict[str, Any] | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Build the resume batch for a (re)connecting subscriber.

        Returns ``(status, wire_payloads)``:

        * ``"ok"``       — ``wire_payloads`` are serialized events the
                           caller can send to the WS in order.  Empty
                           if the subscriber is current.
        * ``"overflow"`` — checkpoint is older than the buffer; caller
                           must REST-refetch.  ``wire_payloads`` empty.
        * ``"mismatch"`` — checkpoint references a stale stream;
                           caller must REST-refetch.  ``wire_payloads`` empty.
        * ``"unsupported"`` — provider doesn't implement the protocol
                           (no ``replay_after``).  Treated as ``"ok"``
                           with no replay; old behavior preserved.

        ``resume_from`` is the dict the client sent in the ``start``
        handshake; ``None`` means "no checkpoint, no replay needed".
        """
        sm = self._sessions.get(session_id)
        if sm is None:
            return "ok", []
        replay = getattr(sm, "replay_after", None)
        if replay is None:
            return "unsupported", []
        if resume_from is None:
            return "ok", []

        stream_id = resume_from.get("stream_id")
        after_seq = resume_from.get("seq")
        if not isinstance(stream_id, str) or not isinstance(after_seq, int):
            # Malformed handshake — treat as no checkpoint rather than
            # erroring; the frontend will receive an empty replay and
            # behave as a fresh subscriber.
            return "ok", []

        status, sequenced = replay(stream_id, after_seq)
        if status != "ok":
            return status, []

        payloads: list[dict[str, Any]] = []
        for seq, event in sequenced:
            wire = serialize_event(event)
            wire["seq"] = seq
            wire["stream_id"] = sm.stream_id
            payloads.append(wire)
        return "ok", payloads

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
        for ws in tuple(subs):
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
        for ws in tuple(self._watchers):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._watchers.discard(ws)
