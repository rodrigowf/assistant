"""WebSocket endpoint for the browser-control Chrome extension.

The extension (``browser-extension/``) connects here and waits for commands;
the backend drives it. This is the transport layer only — command semantics
(navigate, snapshot, click, …) live in the extension's own command registry.

Wire protocol, JSON text frames both directions::

    extension → { "type": "hello", "token": "...", "client": "chrome-extension" }
    backend   → { "id": "ab12", "type": "command", "command": "navigate", "params": {...} }
    extension → { "id": "ab12", "type": "result", "ok": true, "result": {...} }
    extension → { "id": "ab12", "type": "result", "ok": false, "error": "..." }

Every command gets exactly one ``result`` frame keyed by its request ``id``, so
``send_command`` can await one specific call rather than the next reply.

**Auth fails closed.** The extension has ``<all_urls>`` host access and (from
Phase 6) unrestricted JS execution in a logged-in profile, so anything that can
reach this socket can act as Rodrigo on every site he is signed into. If no
token is configured, no connection is accepted. See ``browser-extension/SPEC.md``
§5.2.

Note: frames are sent as *text*, not bytes. A browser ``WebSocket`` delivers a
binary frame as a ``Blob``, which ``JSON.parse`` cannot consume directly — the
extension would need explicit decoding for no benefit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import orjson
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter(tags=["browser"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

TOKEN_ENV_VAR = "BROWSER_CONTROL_TOKEN"
HELLO_TIMEOUT_S = 10.0
DEFAULT_COMMAND_TIMEOUT_S = 30.0

# Application close codes (4000-4999 is the range reserved for app use).
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_REPLACED = 4409


class BrowserNotConnected(RuntimeError):
    """No extension is currently attached."""


class BrowserCommandError(RuntimeError):
    """The extension ran the command and reported a failure."""


class BrowserCommandTimeout(RuntimeError):
    """The extension never answered within the timeout."""


def _resolve_token() -> str | None:
    """Return the shared token, preferring the process environment.

    ``context/scripts/run.sh`` sources ``context/.env`` before launching the
    backend, so the variable is normally already in ``os.environ``. If it isn't
    (backend started some other way), fall back to parsing ``context/.env``
    directly so the single source of truth stays that file. Mirrors
    ``api.routes.config._resolve_openai_key``.
    """
    token = os.environ.get(TOKEN_ENV_VAR)
    if token and token.strip():
        return token.strip()

    env_path = PROJECT_ROOT / "context" / ".env"
    if not env_path.exists():
        return None
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == TOKEN_ENV_VAR:
                # Strip quotes a shell `source` would have removed.
                resolved = value.strip().strip('"').strip("'")
                return resolved or None
    except OSError:
        logger.exception("Failed to read context/.env for %s", TOKEN_ENV_VAR)
    return None


class BrowserHub:
    """Tracks the attached extension and correlates commands to results.

    Single-client by design: one browser, one connection. A new connection
    replaces the old one (the extension's service worker is ephemeral and
    reconnects freely, so stale sockets are normal, not exceptional).
    """

    def __init__(self) -> None:
        self._ws: WebSocket | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._client: dict[str, Any] = {}
        self._connected_at: float | None = None

    # -- state ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "client": dict(self._client),
            "connected_at": self._connected_at,
            "pending_commands": len(self._pending),
        }

    # -- lifecycle ------------------------------------------------------

    async def attach(self, ws: WebSocket, client: dict[str, Any]) -> None:
        """Register a connection, displacing any previous one."""
        if self._ws is not None and self._ws is not ws:
            old = self._ws
            self._ws = None
            self._fail_pending(BrowserNotConnected("extension reconnected"))
            try:
                await old.close(code=WS_CLOSE_REPLACED)
            except Exception:
                pass  # Already gone; nothing to salvage.

        self._ws = ws
        self._client = client
        self._connected_at = time.time()
        logger.info("Browser extension attached: %s", client)

    async def detach(self, ws: WebSocket) -> None:
        """Deregister ``ws`` if it is still the active connection."""
        if self._ws is not ws:
            return  # Already displaced by a newer connection.
        self._ws = None
        self._client = {}
        self._connected_at = None
        self._fail_pending(BrowserNotConnected("extension disconnected"))
        logger.info("Browser extension detached")

    def _fail_pending(self, exc: Exception) -> None:
        """Resolve every in-flight command with ``exc``.

        Without this, a disconnect mid-command leaves the caller awaiting a
        future nothing will ever complete — it would hang until its timeout
        rather than failing immediately with the real reason.
        """
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    # -- command dispatch -----------------------------------------------

    async def send_command(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> Any:
        """Send a command and await its matching ``result`` frame."""
        ws = self._ws
        if ws is None:
            raise BrowserNotConnected("no browser extension is connected")

        request_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut

        try:
            await ws.send_text(orjson.dumps({
                "id": request_id,
                "type": "command",
                "command": command,
                "params": params or {},
            }).decode())
        except Exception as exc:
            self._pending.pop(request_id, None)
            raise BrowserNotConnected(f"failed to send command: {exc}") from exc

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise BrowserCommandTimeout(
                f"command {command!r} timed out after {timeout}s"
            ) from None
        finally:
            self._pending.pop(request_id, None)

    def handle_result(self, msg: dict[str, Any]) -> None:
        """Resolve the pending future for an inbound ``result`` frame."""
        request_id = msg.get("id")
        if not isinstance(request_id, str):
            return
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            # Late arrival after a timeout, or a duplicate. Nothing to do.
            return
        if msg.get("ok"):
            fut.set_result(msg.get("result"))
        else:
            fut.set_exception(
                BrowserCommandError(str(msg.get("error") or "unknown error"))
            )


def get_hub(request: Request) -> BrowserHub:
    return request.app.state.browser_hub


def _is_proxied_from_lan(request: Request) -> str | None:
    """Return the offending client address if the request came from the LAN.

    The Jetson's nginx (``~/nginx-server.conf``) proxies 443 → 127.0.0.1:8765
    and sets ``X-Real-IP`` to the true client address. A request made directly
    to the loopback port carries no such header. So the header's presence — and
    its value — reliably distinguishes "someone on the LAN" from "a process on
    this machine", which app-level ``request.client.host`` cannot do behind a
    proxy (everything looks like 127.0.0.1 there).

    Returns None when the caller is local.
    """
    forwarded = (
        request.headers.get("x-real-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    )
    if not forwarded:
        return None
    if forwarded in ("127.0.0.1", "::1", "localhost"):
        return None
    return forwarded


@router.get("/api/browser/status")
async def browser_status(request: Request) -> dict[str, Any]:
    """Report extension connectivity, so callers can tell 'browser offline'
    apart from 'command failed'."""
    status = get_hub(request).status()
    status["token_configured"] = _resolve_token() is not None
    return status


@router.post("/api/browser/command")
async def browser_command(request: Request) -> dict[str, Any]:
    """Run one browser command and return its result.

    The command channel for Claude Code sessions, which reach it through
    ``context/scripts/browser_cmd.py``. Sessions have Bash but no access to the
    extension's WebSocket (single-client, held by the browser), so this is how
    they drive the browser.

    Two gates, both required:

    * **Loopback only.** A request arriving via nginx from the LAN is refused.
      This channel runs arbitrary JS in a logged-in browser, so it deliberately
      does not inherit the rest of the API's open-to-the-LAN posture.
    * **Shared token**, same ``BROWSER_CONTROL_TOKEN`` the extension presents,
      in an ``X-Browser-Token`` header.
    """
    offender = _is_proxied_from_lan(request)
    if offender:
        logger.warning("Refusing browser command from LAN address %s", offender)
        raise HTTPException(
            status_code=403,
            detail=(
                "browser commands are loopback-only; this request arrived from "
                f"{offender} via the reverse proxy. Use an SSH tunnel."
            ),
        )

    expected = _resolve_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"{TOKEN_ENV_VAR} is not configured in context/.env",
        )
    presented = request.headers.get("x-browser-token") or ""
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Browser-Token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON") from None

    command = body.get("command")
    if not command or not isinstance(command, str):
        raise HTTPException(status_code=400, detail="'command' is required")

    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="'params' must be an object")

    timeout = float(body.get("timeout") or DEFAULT_COMMAND_TIMEOUT_S)
    hub: BrowserHub = request.app.state.browser_hub

    try:
        result = await hub.send_command(command, params, timeout=timeout)
    except BrowserNotConnected as e:
        # 503, not 500: the backend is fine, the browser just isn't attached.
        raise HTTPException(status_code=503, detail=f"browser not connected: {e}") from e
    except BrowserCommandTimeout as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except BrowserCommandError as e:
        # The command ran and failed — a client error (bad selector, stale
        # ref), distinct from the transport being broken.
        raise HTTPException(status_code=422, detail=str(e)) from e

    return {"ok": True, "command": command, "result": result}


@router.websocket("/api/browser/ws")
async def browser_ws(ws: WebSocket):
    await ws.accept()
    hub: BrowserHub = ws.app.state.browser_hub

    expected = _resolve_token()
    if not expected:
        logger.error(
            "Rejecting browser extension: %s is not configured "
            "(set it in context/.env)", TOKEN_ENV_VAR,
        )
        await _reject(ws, "auth_not_configured")
        return

    # --- handshake ---------------------------------------------------
    # A client that connects and says nothing would otherwise hold the slot
    # open indefinitely.
    try:
        raw = await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await _reject(ws, "hello_timeout")
        return

    try:
        hello = orjson.loads(raw)
    except (orjson.JSONDecodeError, ValueError):
        await _reject(ws, "invalid_json")
        return

    if hello.get("type") != "hello":
        await _reject(ws, "expected_hello")
        return

    presented = hello.get("token")
    # compare_digest to keep the comparison time-independent of how many
    # leading characters happen to match.
    if not isinstance(presented, str) or not secrets.compare_digest(presented, expected):
        logger.warning("Rejecting browser extension: bad token")
        await _reject(ws, "unauthorized")
        return

    await hub.attach(ws, {
        "client": hello.get("client", "unknown"),
        "version": hello.get("version"),
    })
    await ws.send_text(orjson.dumps({"type": "ready"}).decode())

    # --- receive loop --------------------------------------------------
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = orjson.loads(raw)
            except (orjson.JSONDecodeError, ValueError):
                continue  # Ignore junk rather than dropping the connection.

            msg_type = msg.get("type")
            if msg_type == "result":
                hub.handle_result(msg)
            elif msg_type == "ping":
                await ws.send_text(orjson.dumps({"type": "pong"}).decode())
            # "pong" and anything else: no action.
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Browser extension receive loop failed")
    finally:
        await hub.detach(ws)


async def _reject(ws: WebSocket, reason: str) -> None:
    """Tell the client why, then close. The reason is the only diagnostic the
    extension can surface in its popup."""
    try:
        await ws.send_text(orjson.dumps({"type": "error", "error": reason}).decode())
    except Exception:
        pass
    try:
        await ws.close(code=WS_CLOSE_UNAUTHORIZED)
    except Exception:
        pass
