"""Auth endpoints — check status and trigger OAuth login."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import get_auth
from api.models import AuthStatusResponse, SetCredentialsRequest
from manager.auth import AuthManager

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(auth: AuthManager = Depends(get_auth)):
    """Check authentication status.

    Returns authenticated=True if valid credentials exist.
    In headless mode, also returns auth_url for manual credential setup.
    """
    authenticated = await auth.is_authenticated()
    return AuthStatusResponse(
        authenticated=authenticated,
        auth_url=auth.get_auth_url() if not authenticated else None,
        headless=auth.is_headless,
    )


@router.post("/login", response_model=AuthStatusResponse)
async def auth_login(auth: AuthManager = Depends(get_auth)):
    """Trigger OAuth browser login flow.

    This opens a browser on the server machine. For headless environments,
    use /api/auth/credentials instead.
    """
    result = await auth.login()
    return AuthStatusResponse(
        authenticated=result,
        auth_url=auth.get_auth_url() if not result else None,
        headless=auth.is_headless,
    )


@router.post("/credentials", response_model=AuthStatusResponse)
async def set_credentials(
    request: SetCredentialsRequest,
    auth: AuthManager = Depends(get_auth),
):
    """Set credentials directly (headless authentication).

    Used in headless environments where browser login isn't possible.
    Copy the full contents of ~/.claude/.credentials.json from an
    authenticated machine and paste it here.
    """
    result = auth.set_credentials(request.credentials_json)
    return AuthStatusResponse(
        authenticated=result,
        headless=auth.is_headless,
    )
