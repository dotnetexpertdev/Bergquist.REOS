"""Integration status routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_session
from ..auth import require_auth
from ..models import ProviderConnection
from ..schemas import AllIntegrationsStatus, IntegrationStatus


router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("/status", response_model=AllIntegrationsStatus)
async def get_integration_status(
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """Get status of all integrations."""
    # Gmail
    gmail_status = IntegrationStatus(
        configured=settings.is_gmail_configured(),
        connected=False,
        details={"redirect_uri": settings.gmail_redirect_uri, "label": settings.gmail_label_name},
    )
    if settings.is_gmail_configured():
        result = await db.execute(
            select(ProviderConnection).where(ProviderConnection.provider == "gmail")
        )
        conn = result.scalar_one_or_none()
        if conn and conn.is_valid:
            gmail_status.connected = True
            gmail_status.details = {
                "account": conn.connected_account,
                "label": settings.gmail_label_name,
                "redirect_uri": settings.gmail_redirect_uri,
            }
        elif conn:
            gmail_status.error = conn.last_error

    # QuickBooks
    qb_status = IntegrationStatus(
        configured=settings.is_quickbooks_configured(),
        connected=False,
    )
    if settings.is_quickbooks_configured():
        result = await db.execute(
            select(ProviderConnection).where(ProviderConnection.provider == "quickbooks")
        )
        conn = result.scalar_one_or_none()
        if conn and conn.is_valid:
            qb_status.connected = True
            qb_status.details = {
                "company_name": conn.company_name,
                "realm_id": conn.realm_id,
                "environment": settings.quickbooks_environment,
            }
            # Check for realm mismatch
            if settings.quickbooks_expected_realm_id and conn.realm_id != settings.quickbooks_expected_realm_id:
                qb_status.error = f"Connected to wrong company. Expected realm {settings.quickbooks_expected_realm_id}"
        elif conn:
            qb_status.error = conn.last_error

    # OpenAI
    openai_status = IntegrationStatus(
        configured=settings.is_openai_configured(),
        connected=settings.is_openai_configured(),  # No connection needed, just API key
    )
    if settings.is_openai_configured():
        openai_status.details = {
            "extraction_model": settings.openai_extraction_model,
            "transcription_model": settings.openai_transcription_model,
        }

    # Todoist
    todoist_status = IntegrationStatus(
        configured=settings.is_todoist_configured(),
        connected=settings.is_todoist_configured(),  # API key based
    )
    if settings.is_todoist_configured():
        todoist_status.details = {
            "project_id": settings.todoist_project_id,
            "default_review_days": settings.todoist_default_review_days,
        }
    elif settings.todoist_enabled:
        todoist_status.error = "Todoist enabled but API token or project ID not configured"

    return AllIntegrationsStatus(
        gmail=gmail_status,
        quickbooks=qb_status,
        openai=openai_status,
        todoist=todoist_status,
    )
