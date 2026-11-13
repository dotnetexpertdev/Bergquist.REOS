"""Tests for authentication module."""

import pytest

from app.auth import (
    hash_password, verify_password, create_session_token, validate_session_token,
    generate_csrf_token
)


def test_password_hashing():
    """Test password hashing and verification."""
    password = "test-password-123"
    hashed = hash_password(password)

    assert hashed != password
    assert hashed.startswith("$2b$")  # bcrypt prefix
    assert verify_password(password, hashed)
    assert not verify_password("wrong-password", hashed)


def test_session_token():
    """Test session token creation and validation."""
    username = "testuser"
    token = create_session_token(username)

    assert token is not None
    assert len(token) > 0

    # Validate returns username
    validated = validate_session_token(token)
    assert validated == username


def test_invalid_session_token():
    """Test invalid session token handling."""
    result = validate_session_token("invalid-token")
    assert result is None


def test_csrf_token_generation():
    """Test CSRF token generation."""
    token1 = generate_csrf_token()
    token2 = generate_csrf_token()

    assert token1 is not None
    assert token2 is not None
    assert len(token1) > 20  # Should be sufficiently long
    assert token1 != token2  # Should be unique


async def test_second_tab_does_not_invalidate_first_tabs_csrf_token(owner):
    """Opening or refreshing another tab calls /session; the first tab's token must keep working."""
    first_tab_token = owner.headers["X-CSRF-Token"]
    for _ in range(2):
        other_tab = (await owner.get("/api/auth/session")).json()
        assert other_tab["csrf_token"] == first_tab_token
    r = await owner.post("/api/search", json={"query": "anything"})
    assert r.status_code != 403, r.text
    owner.headers["X-CSRF-Token"] = "wrong"
    assert (await owner.post("/api/search", json={"query": "anything"})).status_code == 403
