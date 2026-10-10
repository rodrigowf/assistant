"""Accounts and environment keys (Settings → Accounts).

``/api/accounts`` — sign-in status of every service and the sign-in methods
(``manager.accounts``); ``/api/env`` — the ``context/.env`` key manager.

Trust model: the API has no authentication of its own — anyone who can reach it can already run
commands through an agent session — so these routes add no new capability; they just make the
credentials easier to manage. Browser requests from other sites are refused — reads included, a
step stricter than the API-wide guard (``api/guard.py``: ``require_trusted_origin``) — and
secrets stay off the wire by default: lists and statuses
carry masked previews only, a full value is sent only by the explicit ``POST /api/env/{name}/reveal``
(``Cache-Control: no-store``), and raw CLI output never leaves the server. The legacy Claude routes
(``/api/auth/*``) keep working for the AuthGate and older clients.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from api.deps import get_accounts
from api.guard import require_trusted_origin
from manager.accounts import AccountError, AccountsManager
from manager.accounts import envfile

# Every route here can read or change credentials: browser requests from other sites are refused
# on every method (api/guard.py), not only the writes the API-wide middleware covers.
router = APIRouter(tags=["accounts"], dependencies=[Depends(require_trusted_origin)])


class LoginRequest(BaseModel):
    method: str


class CodeRequest(BaseModel):
    code: str


class CredentialsRequest(BaseModel):
    method: str
    content: str


class EnvValue(BaseModel):
    value: str


class EnvCreate(BaseModel):
    name: str
    value: str


def _http(e: AccountError | envfile.EnvError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


# ─────────────────────────────── accounts ───────────────────────────────


@router.get("/api/accounts")
async def list_accounts(accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    return {"services": await accounts.status_all(), "env_path": str(envfile.env_path())}


@router.get("/api/accounts/{service}")
async def get_account(service: str, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    try:
        return await accounts.status(service)
    except AccountError as e:
        raise _http(e) from None


@router.post("/api/accounts/{service}/login")
async def start_login(service: str, req: LoginRequest, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    """Start a link sign-in. Answers once the URL is known (or after ~12 s with ``starting``)."""
    try:
        spec = accounts.service(service).flow_spec(req.method)
        flow = await accounts.flows.start(spec)
    except AccountError as e:
        raise _http(e) from None
    return flow.to_dict()


@router.get("/api/accounts/{service}/login")
async def get_login(service: str, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    """Poll the current (or last) flow. 404 when there is none."""
    try:
        accounts.service(service)
    except AccountError as e:
        raise _http(e) from None
    flow = accounts.flows.get(service)
    if flow is None:
        raise HTTPException(status_code=404, detail="No sign-in in progress.")
    return flow.to_dict()


@router.post("/api/accounts/{service}/login/code")
async def submit_code(service: str, req: CodeRequest, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    """Paste back the code (or redirect address); waits briefly for the CLI's verdict."""
    flow = accounts.flows.get(service)
    if flow is None:
        raise HTTPException(status_code=404, detail="No sign-in in progress. Start a new one.")
    try:
        await flow.submit_code(req.code)
    except AccountError as e:
        raise _http(e) from None
    await flow.wait_done(timeout=15)
    return flow.to_dict()


@router.delete("/api/accounts/{service}/login")
async def cancel_login(service: str, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    flow = await accounts.flows.cancel(service)
    if flow is None:
        raise HTTPException(status_code=404, detail="No sign-in in progress.")
    return flow.to_dict()


@router.post("/api/accounts/{service}/credentials")
async def save_credentials(service: str, req: CredentialsRequest, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    try:
        message = await accounts.service(service).save_credentials(req.method, req.content)
        accounts.forget_verification(service)
        status = await accounts.status(service)
    except AccountError as e:
        raise _http(e) from None
    return {"message": message, "service": status}


@router.post("/api/accounts/{service}/logout")
async def sign_out(service: str, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    try:
        message = await accounts.service(service).sign_out()
        accounts.forget_verification(service)
        status = await accounts.status(service)
    except AccountError as e:
        raise _http(e) from None
    return {"message": message, "service": status}


@router.post("/api/accounts/{service}/verify")
async def verify(service: str, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    """Test the credential against the provider's free "list models" endpoint."""
    try:
        return await accounts.verify(service)
    except AccountError as e:
        raise _http(e) from None


# ─────────────────────────────── env keys ───────────────────────────────


def _change(key_name: str, accounts: AccountsManager, **extra: Any) -> dict[str, Any]:
    accounts.forget_verification()
    applies = envfile.restart_scope(key_name)
    note = (
        "Restart the backend for this to take effect."
        if applies == "backend_restart"
        else "Applies now to the backend and to agent sessions started from now on; restart running sessions to give them the new value."
    )
    return {"applies": applies, "note": note, **extra}


@router.get("/api/env")
async def list_env() -> dict[str, Any]:
    """Every key with a masked preview — never a full value."""
    path = envfile.env_path()
    return {
        "path": str(path),
        "exists": path.exists(),
        "keys": [k.to_dict() for k in envfile.list_keys()],
    }


@router.post("/api/env/{name}/reveal")
async def reveal_env(name: str, response: Response) -> dict[str, Any]:
    """The one route that returns a full value, on explicit request."""
    response.headers["Cache-Control"] = "no-store"
    try:
        value = envfile.get_value(name)
    except envfile.EnvError as e:
        raise _http(e) from None
    if value is None:
        raise HTTPException(status_code=404, detail=f"{name} isn't in the .env file.")
    return {"name": name, "value": value}


@router.post("/api/env")
async def create_env(req: EnvCreate, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    try:
        key = envfile.set_value(req.name, req.value, create=True)
    except envfile.EnvError as e:
        raise _http(e) from None
    return _change(req.name, accounts, key=key.to_dict())


@router.put("/api/env/{name}")
async def put_env(name: str, req: EnvValue, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    """Create or update (services' API-key fields use this)."""
    try:
        key = envfile.set_value(name, req.value)
    except envfile.EnvError as e:
        raise _http(e) from None
    return _change(name, accounts, key=key.to_dict())


@router.delete("/api/env/{name}")
async def delete_env(name: str, accounts: AccountsManager = Depends(get_accounts)) -> dict[str, Any]:
    try:
        removed = envfile.delete(name)
    except envfile.EnvError as e:
        raise _http(e) from None
    return _change(name, accounts, name=name, removed=removed)
