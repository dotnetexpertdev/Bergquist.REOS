"""QuickBooks routes: connection, reference data, duplicate checks, approval, recovery.

The OAuth callback handler is registered at QUICKBOOKS_CALLBACK_ROUTE
(/api/quickbooks/callback). If QUICKBOOKS_OAUTH_CALLBACK names a different path,
app.main registers the same handler there as well, so the configured redirect
URI always reaches this handler.
"""

from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import check_oauth_state, make_oauth_state, require_auth, require_csrf
from ..config import settings
from ..database import get_session
from ..encryption import encrypt_token
from ..models import DuplicateCandidate, ProviderConnection
from ..providers.quickbooks_provider import exchange_quickbooks_code, get_company_info, get_quickbooks_auth_url
from ..providers.registry import open_quickbooks
from ..schemas import (
    ApprovalRequest, ApprovalResponse, DuplicateCandidateResponse, QBAccount, QBClass, QBConnectionStatus,
    QBReferenceData, QBVendor, ResolveDuplicateRequest,
)
from ..services import accounting
from ..services.accounting import AccountingError

router = APIRouter(prefix="/api/quickbooks", tags=["quickbooks"])


def _raise(e: AccountingError):
    raise HTTPException(status_code=e.status_code, detail={"message": e.message, "retryable": e.retryable})


@router.get("/status", response_model=QBConnectionStatus)
async def quickbooks_status(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session)):
    company = await accounting.company_status(db)
    conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "quickbooks"))).scalar_one_or_none()
    return QBConnectionStatus(
        configured=settings.is_quickbooks_configured(),
        connected=company["connected"],
        company_name=company["company_name"],
        realm_id=company["realm_id"],
        environment=settings.quickbooks_environment,
        expected_realm_id=company["expected_realm_id"],
        realm_mismatch=bool(company["connected"] and company["expected_realm_id"]
                            and company["realm_id"] != company["expected_realm_id"]),
        verified=company["verified"],
        last_error=(company["problem"] if settings.is_quickbooks_configured()
                    else "QuickBooks OAuth credentials not configured"),
        token_expires_at=conn.token_expiry if conn and conn.is_valid else None,
        redirect_uri=settings.quickbooks_redirect_uri,
    )


@router.get("/authorize")
async def authorize_quickbooks(username: str = Depends(require_auth)):
    if not settings.is_quickbooks_configured():
        raise HTTPException(status_code=400, detail="QuickBooks not configured")
    return {"auth_url": get_quickbooks_auth_url(make_oauth_state("quickbooks", username)),
            "redirect_uri": settings.quickbooks_redirect_uri}


async def quickbooks_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    realmId: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session),
):
    """Intuit redirects here after the owner authorizes a company."""
    def back(params: str) -> RedirectResponse:
        return RedirectResponse(url=f"/settings?{params}", status_code=303)

    if error:
        return back(f"qb_error={quote(error)}")
    if not check_oauth_state("quickbooks", state):
        return back("qb_error=invalid_or_expired_state")
    if not code or not realmId:
        return back("qb_error=missing_params")
    expected = settings.quickbooks_expected_realm_id
    if expected and realmId != expected:
        return back(f"qb_error=wrong_company&expected={quote(expected)}&got={quote(realmId)}")

    tokens, err = await exchange_quickbooks_code(code, realmId)
    if err:
        return back(f"qb_error={quote(err)}")
    info, info_err = await get_company_info(tokens["access_token"], realmId)

    conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "quickbooks"))).scalar_one_or_none()
    if not conn:
        conn = ProviderConnection(provider="quickbooks")
        db.add(conn)
    conn.access_token_encrypted = encrypt_token(tokens["access_token"])
    if "refresh_token" in tokens:
        conn.refresh_token_encrypted = encrypt_token(tokens["refresh_token"])
    conn.token_expiry = datetime.utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    conn.connected_at = datetime.utcnow()
    conn.realm_id = realmId
    conn.company_name = (info or {}).get("CompanyName")
    conn.is_valid = True
    conn.last_error = None if info else f"Connected, but company details could not be read: {info_err}"
    await db.commit()
    return back("qb_connected=true")


router.add_api_route("/callback", quickbooks_callback, methods=["GET"], include_in_schema=True,
                     name="quickbooks_callback")


@router.post("/disconnect")
async def disconnect_quickbooks(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                                _csrf: None = Depends(require_csrf)):
    conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "quickbooks"))).scalar_one_or_none()
    if conn:
        conn.is_valid = False
        conn.access_token_encrypted = None
        conn.refresh_token_encrypted = None
    return {"success": True}


@router.get("/reference-data", response_model=QBReferenceData)
async def get_reference_data(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session)):
    client, error = await open_quickbooks(db)
    await db.commit()
    if error:
        raise HTTPException(status_code=400, detail=error)
    vendors, err = await client.get_vendors()
    if err:
        raise HTTPException(status_code=502, detail=err)
    accounts, err = await client.get_accounts()
    if err:
        raise HTTPException(status_code=502, detail=err)
    classes, err = await client.get_classes()
    if err:
        classes = []  # classes may be disabled in the company
    return QBReferenceData(
        realm_id=client.realm_id,
        vendors=[QBVendor(id=str(v["Id"]), name=v.get("DisplayName", ""), display_name=v.get("DisplayName", ""))
                 for v in vendors],
        accounts=[QBAccount(id=str(a["Id"]), name=a.get("Name", ""), account_type=a.get("AccountType", ""),
                            fully_qualified_name=a.get("FullyQualifiedName", a.get("Name", ""))) for a in accounts],
        classes=[QBClass(id=str(c["Id"]), name=c.get("Name", "")) for c in classes],
    )


@router.post("/{document_id}/check-duplicates")
async def check_duplicates(document_id: str, username: str = Depends(require_auth),
                           db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    try:
        await accounting._reload(db, document_id)
        client, _ = await accounting.open_verified_company(db)
        result = await accounting.run_duplicate_check(db, document_id, client, username)
    except AccountingError as e:
        _raise(e)
    return {"success": True, **result}


@router.get("/{document_id}/duplicate-candidates", response_model=List[DuplicateCandidateResponse])
async def get_duplicate_candidates(document_id: str, username: str = Depends(require_auth),
                                   db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(DuplicateCandidate).where(DuplicateCandidate.document_id == document_id)
                             .order_by(DuplicateCandidate.found_at.desc()))).scalars().all()
    return [DuplicateCandidateResponse.model_validate(c) for c in rows]


@router.post("/{document_id}/resolve-duplicate")
async def resolve_duplicate(document_id: str, request: ResolveDuplicateRequest,
                            username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                            _csrf: None = Depends(require_csrf)):
    try:
        doc = await accounting.resolve_candidate(db, document_id, request.candidate_id, request.resolution,
                                                 request.reason, username)
    except AccountingError as e:
        _raise(e)
    return {"success": True, "status": doc.status.value, "revision": doc.revision}


@router.post("/{document_id}/approve", response_model=ApprovalResponse)
async def approve_document(document_id: str, request: ApprovalRequest, username: str = Depends(require_auth),
                           db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    """Explicit 'Create unpaid bill' action. Never calls a payment API."""
    try:
        return await accounting.approve_and_create(db, document_id, request.revision, username)
    except AccountingError as e:
        _raise(e)


@router.post("/{document_id}/reconcile", response_model=ApprovalResponse)
async def reconcile_document(document_id: str, username: str = Depends(require_auth),
                             db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    """Check QuickBooks for an unconfirmed bill and resume safely."""
    try:
        return await accounting.reconcile_bill(db, document_id, username)
    except AccountingError as e:
        _raise(e)


@router.post("/{document_id}/retry-attachment", response_model=ApprovalResponse)
async def retry_attachment(document_id: str, username: str = Depends(require_auth),
                           db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    try:
        return await accounting.retry_attachment(db, document_id, username)
    except AccountingError as e:
        _raise(e)
