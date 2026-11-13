"""Authentication routes."""

import secrets
from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..auth import (
    verify_owner_credentials, set_session_cookie, clear_session_cookie,
    get_session_username, generate_csrf_token, set_csrf_cookie, require_auth
)
from ..schemas import LoginRequest, LoginResponse, SessionResponse


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    response: Response,
):
    """Log in with owner credentials."""
    if not verify_owner_credentials(request.username, request.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    set_session_cookie(response, request.username)

    # Set CSRF token
    csrf_token = generate_csrf_token()
    set_csrf_cookie(response, csrf_token)

    return LoginResponse(success=True, username=request.username, csrf_token=csrf_token)


@router.post("/logout")
async def logout(response: Response):
    """Log out and clear session."""
    clear_session_cookie(response)
    return {"success": True}


@router.get("/session", response_model=SessionResponse)
async def get_session(request: Request, response: Response):
    """Get current session status."""
    username = get_session_username(request)

    # Generate new CSRF token if authenticated
    csrf_token = None
    if username:
        csrf_token = generate_csrf_token()
        set_csrf_cookie(response, csrf_token)

    return SessionResponse(
        authenticated=username is not None,
        username=username,
        csrf_token=csrf_token,
    )
