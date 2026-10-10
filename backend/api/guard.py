"""Browser-origin guard: keeps other web sites from driving or reading the API.

The API has no login — anyone who can reach it can already run commands through an agent session —
so the threat handled here is a *web page* open in a browser on the LAN / tailnet: it could POST to
``/api/sessions/inject`` (agents with shell access), open the chat sockets, read history and keys,
or DNS-rebind its own name to this server. Three pieces share the same rules:

* :class:`RequestGuardMiddleware` (installed in ``api/app.py``) refuses — HTTP 403, or WebSocket
  close 1008 before ``accept`` (the server answers the handshake with 403):

  - every ``/api/*``, ``/memory``, ``/uploads`` and ``/projects`` request whose ``Host`` is not
    trusted (DNS rebinding);
  - every ``/api/*`` request with a state-changing method (POST / PUT / PATCH / DELETE) and every
    WebSocket handshake (any path) that is cross-site: ``Sec-Fetch-Site: cross-site``, or an
    ``Origin`` that is not trusted (``Origin: null`` never is).

  The web apps and ``context/public/`` pages are never looked at (any device opens them by IP or
  name), and API reads are protected by the CORS policy rather than refused.
* :class:`TrustedCORSMiddleware` — CORS that echoes only trusted origins, so a cross-site page
  cannot read API responses (same-origin pages need no CORS at all).
* :func:`require_trusted_origin` — the stricter per-route dependency of the credential routes
  (``/api/accounts``, ``/api/env``): origin checks on reads too.

Trusted ``Origin`` (:func:`origin_allowed`): the request's own host name (any scheme / port — nginx
passes ``Host $host`` without the port, the laptop is reached on ``:8765``), the web dev and mock
servers (ports 5450 / 5451 / 8799) on a trusted host, ``chrome-extension://`` (Archie's browser
extension, which also authenticates with its token), and ``ARCHIE_TRUSTED_ORIGINS``
(comma-separated, exact ``scheme://host[:port]``). Only the last two are exempt from
``Sec-Fetch-Site: cross-site`` — a page on another machine is always cross-site — so a
same-host-looking ``Origin`` on a cross-site request is still refused.

Trusted ``Host``: IP literals, single-label names, ``localhost``, ``*.local``, ``*.lan``, ``*.home``,
``*.internal``, ``*.localhost``, ``*.ts.net``, this machine's host names, or ``ARCHIE_TRUSTED_HOSTS``
(comma-separated) — names a rebinding page could not have registered.

Requests without ``Origin`` (the Android apps' OkHttp, curl, scripts, the orchestrator's tools)
pass the origin checks; they still need a trusted ``Host``.
"""

from __future__ import annotations

import contextvars
import ipaddress
import logging
import os
import socket
from functools import lru_cache
from typing import Mapping
from urllib.parse import urlsplit

import orjson
from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

DEV_PORTS = frozenset({5450, 5451, 8799})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localhost", ".ts.net")
_EXTENSION_SCHEMES = frozenset({"chrome-extension"})


@lru_cache(maxsize=1)
def _machine_names() -> frozenset[str]:
    names = {socket.gethostname().lower()}
    try:
        names.add(socket.getfqdn().lower())
    except OSError:
        pass
    return frozenset(n for n in names if n)


def _extra(var: str) -> set[str]:
    return {x.strip().lower().rstrip("/") for x in os.environ.get(var, "").split(",") if x.strip()}


def _hostname(host_header: str) -> str:
    """`Host` value without the port (handles `[v6]:port`)."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h


def _normal(origin: str) -> str:
    return origin.strip().lower().rstrip("/")


def host_trusted(hostname: str) -> bool:
    h = hostname.strip().lower().rstrip(".")
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if "." not in h or h == "localhost" or h.endswith(_PRIVATE_SUFFIXES):
        return True
    return h in _machine_names() or h in _extra("ARCHIE_TRUSTED_HOSTS")


def origin_explicitly_trusted(origin: str) -> bool:
    """Archie's browser extension or ``ARCHIE_TRUSTED_ORIGINS`` — origins no web page can claim."""
    origin = _normal(origin)
    if origin == "null":
        return False
    return urlsplit(origin).scheme in _EXTENSION_SCHEMES or origin in _extra("ARCHIE_TRUSTED_ORIGINS")


def origin_allowed(origin: str, host_header: str) -> bool:
    if origin_explicitly_trusted(origin):
        return True
    parts = urlsplit(_normal(origin))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False  # "null" (sandboxed frames, file://) and anything odd
    if parts.hostname == _hostname(host_header):
        return True
    try:
        port = parts.port
    except ValueError:
        return False
    return port in DEV_PORTS and host_trusted(parts.hostname)


def rejection_reason(headers: Mapping[str, str], *, check_origin: bool = True) -> str | None:
    """Why a request with these headers must be refused, or ``None`` when it may pass.

    The ``Host`` is always checked; ``check_origin`` adds the cross-site checks
    (``Sec-Fetch-Site``, ``Origin``).
    """
    host = headers.get("host", "")
    if not host_trusted(_hostname(host)):
        return f"Unexpected Host {host!r}; add it to ARCHIE_TRUSTED_HOSTS if it is yours."
    if not check_origin:
        return None
    origin = headers.get("origin")
    cross_site = headers.get("sec-fetch-site", "").lower() == "cross-site"
    if cross_site and not (origin is not None and origin_explicitly_trusted(origin)):
        return "Cross-site request rejected; add the page's origin to ARCHIE_TRUSTED_ORIGINS if it is yours."
    if origin is not None and not origin_allowed(origin, host):
        return f"Cross-site request rejected: {origin} is not trusted; add it to ARCHIE_TRUSTED_ORIGINS if it is yours."
    return None


def _short(value: str | None) -> str:
    return repr(value if value is None or len(value) <= 120 else value[:120] + "…")


def _log_rejection(method: str, path: str, headers: Mapping[str, str], reason: str) -> None:
    logger.warning(
        "Rejected %s %s (origin=%s host=%s sec-fetch-site=%s): %s",
        method, path, _short(headers.get("origin")), _short(headers.get("host")),
        _short(headers.get("sec-fetch-site")), reason.split(";")[0],
    )


def require_trusted_origin(request: Request) -> None:
    """FastAPI dependency for the credential routes: origin checks on every method, reads included."""
    reason = rejection_reason(request.headers)
    if reason is not None:
        _log_rejection(request.method, request.url.path, request.headers, reason)
        raise HTTPException(status_code=403, detail=reason)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


# Private static content: only the Host check (DNS rebinding), like API reads. The web apps and
# context/public pages stay reachable under any Host.
_API_PREFIXES = ("/api",)
_PRIVATE_STATIC_PREFIXES = ("/memory", "/uploads", "/projects")


class RequestGuardMiddleware:
    """Applies :func:`rejection_reason` to ``/api/*`` (origin checks on writes), every WebSocket, and
    (Host only) the private static prefixes ``/memory``, ``/uploads``, ``/projects``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            headers = Headers(scope=scope)
            reason = rejection_reason(headers)
            if reason is not None:
                _log_rejection("WS", scope["path"], headers, reason)
                # Close before accept: the server answers the handshake with HTTP 403.
                if (await receive())["type"] == "websocket.connect":
                    await send({"type": "websocket.close", "code": 1008, "reason": "cross-site request rejected"})
                return
        elif scope["type"] == "http" and (
            (api := _under(scope["path"], _API_PREFIXES)) or _under(scope["path"], _PRIVATE_STATIC_PREFIXES)
        ):
            headers = Headers(scope=scope)
            reason = rejection_reason(headers, check_origin=api and scope["method"] in MUTATING_METHODS)
            if reason is not None:
                _log_rejection(scope["method"], scope["path"], headers, reason)
                body = orjson.dumps({"detail": reason})
                await send({
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


_request_host: contextvars.ContextVar[str] = contextvars.ContextVar("archie_cors_request_host", default="")


class TrustedCORSMiddleware(CORSMiddleware):
    """Starlette's CORS, echoing an ``Origin`` only when :func:`origin_allowed` trusts it.

    The same-host rule needs the request's ``Host``, which ``is_allowed_origin`` is not given, so
    the request's ``Host`` is kept in a context variable while the request runs.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app, allow_origins=(), allow_methods=["*"], allow_headers=["*"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _request_host.set(Headers(scope=scope).get("host", ""))
        try:
            await super().__call__(scope, receive, send)
        finally:
            _request_host.reset(token)

    def is_allowed_origin(self, origin: str) -> bool:
        return origin_allowed(origin, _request_host.get())
