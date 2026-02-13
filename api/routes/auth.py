"""Auth endpoints — check status and trigger OAuth login."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_auth
from api.models import AuthStatusResponse
from manager.auth import AuthManager

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(auth: AuthManager = Depends(get_auth)):
    authenticated = await auth.is_authenticated()
    return AuthStatusResponse(authenticated=authenticated)


@router.post("/login", response_model=AuthStatusResponse)
async def auth_login(auth: AuthManager = Depends(get_auth)):
    result = await auth.login()
    return AuthStatusResponse(authenticated=result)
