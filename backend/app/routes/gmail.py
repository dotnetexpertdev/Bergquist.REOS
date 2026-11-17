"""Gmail routes: read-only OAuth connection and 'Import From Inbox'.

The OAuth callback handler is registered at GMAIL_CALLBACK_ROUTE
(/api/gmail/callback). If GMAIL_OAUTH_CALLBACK names a different path, app.main
registers the same handler there too, so the configured redirect URI always works.
"""

from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import check_oauth_state, make_oauth_state, require_auth, require_csrf
from ..config import settings
from ..database import get_session
from ..encryption import encrypt_token
from ..models import Arrival, ArrivalMethod, ProviderConnection
from ..providers.gmail_provider import (
    exchange_gmail_code, extract_attachments, get_gmail_auth_url, parse_headers, save_attachment_temp,
    save_raw_email,
)
from ..providers.registry import open_gmail
from ..schemas import GmailImportResponse, GmailImportResult, IntegrationStatus
from ..services.intake import SUPPORTED_CONTENT_TYPES, cleanup_temp_file, ingest_document
from ..services.processing import process_documents

router = APIRouter(prefix="/api/gmail", tags=["gmail"])


@router.get("/status", response_model=IntegrationStatus)
async def gmail_status(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session)):
    conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "gmail"))).scalar_one_or_none()
    if conn and conn.is_valid:
        return IntegrationStatus(configured=settings.is_gmail_configured(), connected=True, details={
            "account": conn.connected_account, "label": settings.gmail_label_name,
            "connected_at": conn.connected_at.isoformat() if conn.connected_at else None,
            "redirect_uri": settings.gmail_redirect_uri})
    if not settings.is_gmail_configured():
        return IntegrationStatus(configured=False, connected=False, error="Gmail OAuth credentials not configured",
                                 details={"redirect_uri": settings.gmail_redirect_uri})
    return IntegrationStatus(configured=True, connected=False, error=conn.last_error if conn else None,
                             details={"redirect_uri": settings.gmail_redirect_uri, "label": settings.gmail_label_name})


@router.get("/authorize")
async def authorize_gmail(username: str = Depends(require_auth)):
    if not settings.is_gmail_configured():
        raise HTTPException(status_code=400, detail="Gmail not configured")
    return {"auth_url": get_gmail_auth_url(make_oauth_state("gmail", username)),
            "redirect_uri": settings.gmail_redirect_uri}


async def gmail_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session),
):
    """Google redirects here after the owner grants read-only access."""
    def back(params: str) -> RedirectResponse:
        return RedirectResponse(url=f"/settings?{params}", status_code=303)

    if error:
        return back(f"gmail_error={quote(error)}")
    if not check_oauth_state("gmail", state):
        return back("gmail_error=invalid_or_expired_state")
    if not code:
        return back("gmail_error=missing_code")

    tokens, err = await exchange_gmail_code(code)
    if err:
        return back(f"gmail_error={quote(err)}")
    account = "unknown"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                 headers={"Authorization": f"Bearer {tokens['access_token']}"})
            if r.status_code == 200:
                account = r.json().get("email", "unknown")
    except httpx.HTTPError:
        pass

    conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "gmail"))).scalar_one_or_none()
    if not conn:
        conn = ProviderConnection(provider="gmail")
        db.add(conn)
    conn.access_token_encrypted = encrypt_token(tokens["access_token"])
    if "refresh_token" in tokens:
        conn.refresh_token_encrypted = encrypt_token(tokens["refresh_token"])
    conn.token_expiry = datetime.utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    conn.connected_at = datetime.utcnow()
    conn.connected_account = account
    conn.is_valid = True
    conn.last_error = None
    await db.commit()
    return back("gmail_connected=true")


router.add_api_route("/callback", gmail_callback, methods=["GET"], name="gmail_callback")


@router.post("/disconnect")
async def disconnect_gmail(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                           _csrf: None = Depends(require_csrf)):
    conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "gmail"))).scalar_one_or_none()
    if conn:
        conn.is_valid = False
        conn.access_token_encrypted = None
        conn.refresh_token_encrypted = None
    return {"success": True}


def _failure(error: str) -> GmailImportResponse:
    return GmailImportResponse(success=False, messages_found=0, attachments_processed=0, imported=0,
                               already_processed=0, skipped=0, failed=0, error=error)


@router.post("/import", response_model=GmailImportResponse)
async def import_gmail_messages(background_tasks: BackgroundTasks, username: str = Depends(require_auth),
                                db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    """Import attachments from messages carrying the configured label. Safe to repeat."""
    client, error = await open_gmail(db)
    await db.commit()
    if error:
        return _failure(error)
    label_id, error = await client.get_label_id(settings.gmail_label_name)
    if error:
        return _failure(error)
    messages, error = await client.list_labeled_messages(label_id)
    if error:
        return _failure(error)

    settings.emails_path.mkdir(parents=True, exist_ok=True)
    settings.temp_path.mkdir(parents=True, exist_ok=True)
    results: List[GmailImportResult] = []
    counts = {"imported": 0, "already": 0, "skipped": 0, "failed": 0, "attachments": 0}
    new_ids: List[str] = []

    for msg_ref in messages:
        message_id = msg_ref["id"]
        raw_email, error = await client.get_message_raw(message_id)
        if error:
            results.append(GmailImportResult(message_id=message_id, attachment_filename="", success=False,
                                             error=error))
            counts["failed"] += 1
            continue
        headers = parse_headers(raw_email)
        attachments = extract_attachments(raw_email, message_id)
        if not attachments:
            results.append(GmailImportResult(message_id=message_id, attachment_filename="", success=True,
                                             skipped_reason="No attachments found"))
            counts["skipped"] += 1
            continue
        email_path = await save_raw_email(raw_email, message_id)  # preserved once per message

        for attachment in attachments:
            counts["attachments"] += 1
            if attachment.content_type not in SUPPORTED_CONTENT_TYPES:
                results.append(GmailImportResult(message_id=message_id, attachment_filename=attachment.filename,
                                                 success=True, skipped_reason=f"Unsupported type: {attachment.content_type}"))
                counts["skipped"] += 1
                continue
            existing = (await db.execute(select(Arrival).where(
                Arrival.gmail_message_id == message_id,
                Arrival.gmail_attachment_id == attachment.attachment_id))).scalar_one_or_none()
            if existing:
                await db.commit()
                results.append(GmailImportResult(message_id=message_id, attachment_filename=attachment.filename,
                                                 success=True, document_id=existing.document_id, is_duplicate=True))
                counts["already"] += 1
                continue
            temp_path = None
            try:
                temp_path = await save_attachment_temp(attachment)
                document, _arrival, is_new = await ingest_document(
                    db, temp_path, attachment.content_type, ArrivalMethod.EMAIL,
                    original_filename=attachment.filename, gmail_message_id=message_id,
                    gmail_attachment_id=attachment.attachment_id, email_from=headers["from"],
                    email_subject=headers["subject"], email_received_at=headers["date"],
                    email_source_path=email_path.relative_to(settings.emails_path).as_posix(),
                )
                await db.commit()
                if is_new:
                    new_ids.append(document.id)
                results.append(GmailImportResult(message_id=message_id, attachment_filename=attachment.filename,
                                                 success=True, document_id=document.id, is_duplicate=not is_new))
                counts["imported"] += 1
            except Exception as e:
                await db.rollback()
                results.append(GmailImportResult(message_id=message_id, attachment_filename=attachment.filename,
                                                 success=False, error=f"{type(e).__name__}: {e}"))
                counts["failed"] += 1
            finally:
                if temp_path:
                    cleanup_temp_file(temp_path)

    if new_ids:
        background_tasks.add_task(process_documents, new_ids)
    return GmailImportResponse(
        success=counts["failed"] == 0, messages_found=len(messages), attachments_processed=counts["attachments"],
        imported=counts["imported"], already_processed=counts["already"], skipped=counts["skipped"],
        failed=counts["failed"], results=results,
    )
