"""Authentication and session management."""

import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .config import settings

# Password hashing uses the `bcrypt` package directly (pinned in requirements.txt).
# Passlib 1.7.4 is unmaintained and crashes with bcrypt >= 4.1 during its
# backend self-test, so it is no longer used. Hashes produced earlier by Passlib
# ($2a$/$2b$/$2y$ bcrypt strings) remain valid and verify unchanged.
BCRYPT_ROUNDS = 12
BCRYPT_MAX_PASSWORD_BYTES = 72
SUPPORTED_HASH_PREFIXES = ("$2a$", "$2b$", "$2y$")

# Session serializer
session_serializer = URLSafeTimedSerializer(settings.session_secret)

# Session cookie settings
SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE = 86400 * 7  # 7 days


class PasswordTooLong(ValueError):
    pass


def hash_password(password: str) -> str:
    """Hash a password for storage (bcrypt, cost 12)."""
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooLong(f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a stored bcrypt hash. Never raises."""
    if not hashed_password or not hashed_password.startswith(SUPPORTED_HASH_PREFIXES):
        return False
    raw = plain_password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        return False
    stored = hashed_password
    if stored.startswith("$2y$"):
        # PHP-style prefix; the algorithm is identical to $2b$.
        stored = "$2b$" + stored[4:]
    try:
        return bcrypt.checkpw(raw, stored.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_session_token(username: str) -> str:
    """Create a signed session token."""
    data = {
        "username": username,
        "created": datetime.utcnow().isoformat(),
    }
    return session_serializer.dumps(data)


def validate_session_token(token: str) -> Optional[str]:
    """
    Validate a session token and return the username.
    Returns None if invalid or expired.
    """
    try:
        data = session_serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("username")
    except (BadSignature, SignatureExpired):
        return None


def get_session_username(request: Request) -> Optional[str]:
    """Get the username from the session cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return validate_session_token(token)


async def require_auth(request: Request) -> str:
    """
    Dependency that requires authentication.
    Returns the username if authenticated, raises 401 otherwise.
    """
    username = get_session_username(request)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return username


async def optional_auth(request: Request) -> Optional[str]:
    """
    Dependency that optionally gets authentication.
    Returns the username if authenticated, None otherwise.
    """
    return get_session_username(request)


def set_session_cookie(response: Response, username: str) -> None:
    """Set the session cookie on a response."""
    token = create_session_token(username)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.public_origin.startswith("https"),
    )


def clear_session_cookie(response: Response) -> None:
    """Clear the session cookie."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.public_origin.startswith("https"),
    )


def verify_owner_credentials(username: str, password: str) -> bool:
    """Verify the owner's credentials."""
    if username != settings.owner_username:
        return False
    if not settings.owner_password_hash:
        return False
    return verify_password(password, settings.owner_password_hash)


# CSRF Protection
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_COOKIE_NAME = "csrf_token"


def generate_csrf_token() -> str:
    """Generate a new CSRF token."""
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    """Set the CSRF cookie."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=False,  # Must be readable by JavaScript
        samesite="strict",
        secure=settings.public_origin.startswith("https"),
    )


def verify_csrf_token(request: Request) -> bool:
    """Verify CSRF token matches between header and cookie."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)

    if not cookie_token or not header_token:
        return False

    return secrets.compare_digest(cookie_token, header_token)


async def require_csrf(request: Request) -> None:
    """Dependency that requires valid CSRF token for mutations."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not verify_csrf_token(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid CSRF token",
            )


# OAuth state: signed and time-limited, so it survives a server restart or
# --reload and cannot be forged or replayed for another provider.
OAUTH_STATE_MAX_AGE = 600


def make_oauth_state(provider: str, username: str) -> str:
    return URLSafeTimedSerializer(settings.session_secret, salt=f"oauth-{provider}").dumps(
        {"u": username, "n": secrets.token_urlsafe(8)}
    )


def check_oauth_state(provider: str, state: Optional[str]) -> Optional[str]:
    """Return the username that started the flow, or None if the state is invalid/expired."""
    if not state:
        return None
    try:
        data = URLSafeTimedSerializer(settings.session_secret, salt=f"oauth-{provider}").loads(
            state, max_age=OAUTH_STATE_MAX_AGE
        )
    except (BadSignature, SignatureExpired):
        return None
    return data.get("u") if data.get("u") == settings.owner_username else None
