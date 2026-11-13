"""Provider registry.

Production code obtains every external client through this module. The real
adapters are installed by default. Deterministic test adapters live under
``backend/tests`` and are installed only by the test suite or by the test-only
end-to-end server (``backend/tests/e2e_server.py``); nothing in ``app`` imports
them, so fault injection is unreachable in normal operation.
"""

from types import SimpleNamespace
from typing import Any, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession


async def _open_quickbooks(db: AsyncSession) -> Tuple[Optional[Any], Optional[str]]:
    from .quickbooks_provider import QuickBooksAPI, get_quickbooks_tokens

    access_token, realm_id, error = await get_quickbooks_tokens(db)
    if error:
        return None, error
    return QuickBooksAPI(access_token, realm_id), None


async def _open_gmail(db: AsyncSession) -> Tuple[Optional[Any], Optional[str]]:
    from .gmail_provider import GmailAPI, get_gmail_tokens

    access_token, error = await get_gmail_tokens(db)
    if error:
        return None, error
    return GmailAPI(access_token), None


def _openai() -> Any:
    from . import openai_provider

    return openai_provider


def _default() -> SimpleNamespace:
    return SimpleNamespace(open_quickbooks=_open_quickbooks, open_gmail=_open_gmail, ai=_openai)


providers = _default()


def reset() -> None:
    """Restore the real adapters (used by tests)."""
    global providers
    fresh = _default()
    providers.open_quickbooks = fresh.open_quickbooks
    providers.open_gmail = fresh.open_gmail
    providers.ai = fresh.ai


async def open_quickbooks(db: AsyncSession):
    return await providers.open_quickbooks(db)


async def open_gmail(db: AsyncSession):
    return await providers.open_gmail(db)


def ai():
    return providers.ai()
