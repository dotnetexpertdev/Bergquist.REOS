"""Encryption utilities for storing sensitive data like OAuth tokens."""

from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def get_fernet() -> Optional[Fernet]:
    """Get the Fernet encryption instance."""
    if not settings.token_encryption_key:
        return None
    try:
        return Fernet(settings.token_encryption_key.encode())
    except Exception:
        return None


def encrypt_token(token: str) -> Optional[str]:
    """
    Encrypt a token for storage.
    Returns None if encryption is not configured.
    """
    fernet = get_fernet()
    if not fernet:
        return None
    try:
        return fernet.encrypt(token.encode()).decode()
    except Exception:
        return None


def decrypt_token(encrypted_token: str) -> Optional[str]:
    """
    Decrypt a stored token.
    Returns None if decryption fails.
    """
    fernet = get_fernet()
    if not fernet:
        return None
    try:
        return fernet.decrypt(encrypted_token.encode()).decode()
    except InvalidToken:
        return None
    except Exception:
        return None


def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key."""
    return Fernet.generate_key().decode()
