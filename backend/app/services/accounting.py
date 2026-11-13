"""QuickBooks accounting workflow: duplicate checks, approval, recovery.

Ordering guarantees (US-07, US-09, US-10)
-----------------------------------------
1. The connected company is verified against QUICKBOOKS_EXPECTED_REALM_ID and
   re-read from QuickBooks before any write.
2. A complete, paginated, vendor-scoped bill lookup must succeed immediately
   before a create. A failed page or an unresolved match means zero create calls.
3. In one committed transaction the approval is claimed with a conditional
   ``UPDATE ... WHERE revision = ? AND status = 'ready_for_approval'``, the
   vendor invoice is claimed in ``accounting_claims`` (unique per company,
   vendor and invoice number) and the operation with its idempotency key is
   recorded. Only then is the bill created.
4. The returned bill ID is committed before the attachment upload starts.
5. An uncertain result (timeout, dropped connection, 5xx) is recorded as
   unknown; the only way forward is :func:`reconcile_bill`, which searches the
   company for the bill carrying this operation's marker before re-sending the
   *same* request ID.
6. An attachment is complete only when an attachment on the same bill is shown
   to be this exact file (our SHA-256 note, or downloaded bytes that hash equal).
"""

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, literal_column, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    AccountingClaim, AuditEvent, Document, DocumentStatus, DuplicateCandidate, EventType, Operation,
    OperationStatus, OperationType, ProviderConnection,
)
from ..providers.quickbooks_provider import (
    attachment_note, bill_operation_marker, bill_record_url, build_bill_payload, payload_hash,
)
from ..providers.registry import open_quickbooks
from .documents import get_pending_question, missing_fields, normalize_invoice_number, update_document_status


class AccountingError(Exception):
    def __init__(self, status_code: int, message: str, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.retryable = retryable


def _event(db: AsyncSession, doc_id: str, event_type: EventType, actor: str, **details: Any) -> None:
    db.add(AuditEvent(document_id=doc_id, event_type=event_type, actor=actor, details=details or None))


async def _reload(db: AsyncSession, document_id: str) -> Document:
    doc = (await db.execute(
        select(Document).where(Document.id == document_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not doc:
        raise AccountingError(404, "Document not found")
    return doc


# ---------------------------------------------------------------------------
# Company verification
# ---------------------------------------------------------------------------

async def company_status(db: AsyncSession) -> Dict[str, Any]:
    conn = (await db.execute(
        select(ProviderConnection).where(ProviderConnection.provider == "quickbooks")
    )).scalar_one_or_none()
    expected = settings.quickbooks_expected_realm_id or None
    connected = bool(conn and conn.is_valid)
    realm = conn.realm_id if connected else None
    problem = None
    if not connected:
        problem = (conn.last_error if conn and conn.last_error else "QuickBooks is not connected")
    elif not expected:
        problem = "QUICKBOOKS_EXPECTED_REALM_ID is not set, so accounting writes are blocked"
    elif realm != expected:
        problem = f"Connected to company {realm}, but the configured test company is {expected}"
    return {
        "connected": connected,
        "company_name": conn.company_name if connected else None,
        "realm_id": realm,
        "expected_realm_id": expected,
        "environment": settings.quickbooks_environment,
        "verified": connected and problem is None,
        "problem": problem,
    }


async def open_verified_company(db: AsyncSession, required_realm: Optional[str] = None):
    """Return (client, company_name) for the configured test company, or raise."""
    expected = settings.quickbooks_expected_realm_id
    if not expected:
        raise AccountingError(400, "Set QUICKBOOKS_EXPECTED_REALM_ID to your QuickBooks test company ID "
                                   "before creating bills.")
    client, error = await open_quickbooks(db)
    await db.commit()  # never hold the write lock across network calls
    if error or client is None:
        raise AccountingError(400, f"QuickBooks unavailable: {error}. Reconnect in Settings; the bill stays "
                                   "available for review.", retryable=True)
    if client.realm_id != expected:
        raise AccountingError(409, f"Connected QuickBooks company {client.realm_id} is not the configured test "
                                   f"company {expected}. Reconnect the correct company.")
    if required_realm and client.realm_id != required_realm:
        raise AccountingError(409, f"This bill belongs to company {required_realm}, but {client.realm_id} is "
                                   "connected.")
    info = await client.get_company_info()
    if not info.ok:
        raise AccountingError(503, f"Could not verify the QuickBooks company ({info.error}). Try again; "
                                   "nothing was written.", retryable=True)
    return client, (info.data or {}).get("CompanyName")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def _dec(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01")) if value is not None else None
    except Exception:
        return None


def compare_bill(doc: Document, bill: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Field-by-field comparison of a local document with a QuickBooks bill."""
    local_total = _dec(doc.effective_total)
    remote_total = _dec(bill.get("TotalAmt"))
    remote_currency = (bill.get("CurrencyRef") or {}).get("value") or settings.quickbooks_home_currency
    local_date = doc.effective_invoice_date.isoformat() if doc.effective_invoice_date else None
    details = {
        "vendor": {"local": doc.qb_vendor_id, "remote": (bill.get("VendorRef") or {}).get("value"),
                   "remote_name": (bill.get("VendorRef") or {}).get("name")},
        "invoice_number": {"local": doc.effective_invoice_number, "remote": bill.get("DocNumber")},
        "total": {"local": str(local_total) if local_total is not None else None,
                  "remote": str(remote_total) if remote_total is not None else None},
        "currency": {"local": doc.effective_currency, "remote": remote_currency},
        "date": {"local": local_date, "remote": bill.get("TxnDate")},
    }
    details["vendor"]["match"] = details["vendor"]["local"] == details["vendor"]["remote"]
    key = normalize_invoice_number(doc.effective_invoice_number)
    details["invoice_number"]["match"] = bool(key) and key == normalize_invoice_number(bill.get("DocNumber"))
    details["total"]["match"] = local_total is not None and local_total == remote_total
    details["currency"]["match"] = (doc.effective_currency or "") == remote_currency
    details["date"]["match"] = bool(local_date) and local_date == bill.get("TxnDate")

    if not details["vendor"]["match"]:
        return False, "", details
    if details["invoice_number"]["match"]:
        same = [f for f in ("total", "currency", "date") if details[f]["match"]]
        differ = [f for f in ("total", "currency", "date") if not details[f]["match"]]
        reason = f"Same vendor and invoice number {bill.get('DocNumber')}"
        if same:
            reason += f"; {', '.join(same)} also match"
        if differ:
            reason += f"; {', '.join(differ)} differ"
        return True, reason, details
    # Equal amount alone is never enough; amount AND date together make a possible match.
    if details["total"]["match"] and details["date"]["match"]:
        return True, (f"Same vendor, amount and date, but invoice number differs "
                      f"({doc.effective_invoice_number or 'unknown'} vs {bill.get('DocNumber') or 'none'})"), details
    return False, "", details


async def open_candidates(db: AsyncSession, document_id: str) -> List[DuplicateCandidate]:
    return list((await db.execute(select(DuplicateCandidate).where(
        DuplicateCandidate.document_id == document_id, DuplicateCandidate.resolved == False  # noqa: E712
    ))).scalars().all())


async def run_duplicate_check(db: AsyncSession, document_id: str, client, actor: str) -> Dict[str, Any]:
    """Complete lookup in the connected company; upserts candidates. Raises on lookup failure."""
    doc = await _reload(db, document_id)
    if not doc.qb_vendor_id or not doc.effective_invoice_number:
        raise AccountingError(400, "Choose the QuickBooks vendor and invoice number before checking.")
    vendor_id = doc.qb_vendor_id
    await db.commit()

    lookup = await client.find_vendor_bills(vendor_id)

    doc = await _reload(db, document_id)
    now = datetime.utcnow()
    doc.last_accounting_check_at = now
    if not lookup.complete:
        doc.last_accounting_check_ok = False
        doc.last_accounting_check_error = lookup.error
        _event(db, doc.id, EventType.ACCOUNTING_CHECK_FAILED, actor, error=lookup.error,
               pages_read=lookup.pages_read, realm_id=client.realm_id)
        await db.commit()
        raise AccountingError(502, f"The QuickBooks duplicate check did not complete ({lookup.error}). "
                                   "Nothing was posted. Retry the check.", retryable=True)

    existing = {c.qb_bill_id: c for c in (await db.execute(
        select(DuplicateCandidate).where(DuplicateCandidate.document_id == doc.id))).scalars().all()}
    had_previous_check = bool(existing) or doc.last_accounting_check_ok is not None
    new_ids: List[str] = []
    dismissed_seen: List[str] = []
    for bill in lookup.bills:
        bill_id = str(bill.get("Id"))
        if doc.qb_bill_id and bill_id == doc.qb_bill_id:
            continue
        is_match, reason, details = compare_bill(doc, bill)
        if not is_match:
            continue
        if f"document {doc.id}" in (bill.get("PrivateNote") or ""):
            reason += " (created earlier by this app for this document)"
        cand = existing.get(bill_id)
        fields = dict(
            realm_id=client.realm_id, qb_doc_number=bill.get("DocNumber"),
            qb_vendor_id=(bill.get("VendorRef") or {}).get("value"),
            qb_vendor_name=(bill.get("VendorRef") or {}).get("name"),
            qb_total=_dec(bill.get("TotalAmt")), qb_currency=details["currency"]["remote"],
            qb_txn_date=date.fromisoformat(bill["TxnDate"]) if bill.get("TxnDate") else None,
            match_reason=reason, match_fields=[k for k, v in details.items() if v.get("match")],
            match_details=details, last_seen_at=now,
        )
        if cand:
            for k, v in fields.items():
                setattr(cand, k, v)
            cand.seen_count = (cand.seen_count or 1) + 1
            if cand.resolved and cand.resolution == "dismissed":
                dismissed_seen.append(bill_id)
        else:
            db.add(DuplicateCandidate(document_id=doc.id, qb_bill_id=bill_id, found_at=now, seen_count=1, **fields))
            new_ids.append(bill_id)

    doc.last_accounting_check_ok = True
    doc.last_accounting_check_error = None
    _event(db, doc.id, EventType.DUPLICATE_CHECK_COMPLETED, actor, realm_id=client.realm_id,
           pages_read=lookup.pages_read, bills_scanned=len(lookup.bills), new_matches=new_ids,
           previously_dismissed_still_present=dismissed_seen, newly_discovered=had_previous_check and bool(new_ids))
    await db.flush()
    await update_document_status(db, doc, actor)
    await db.commit()
    open_now = await open_candidates(db, doc.id)
    return {"candidates_found": len(open_now), "new_candidates": new_ids, "pages_read": lookup.pages_read,
            "dismissed_still_present": dismissed_seen}


async def resolve_candidate(db: AsyncSession, document_id: str, candidate_id: str, resolution: str,
                            reason: Optional[str], actor: str) -> Document:
    doc = await _reload(db, document_id)
    cand = (await db.execute(select(DuplicateCandidate).where(DuplicateCandidate.id == candidate_id))).scalar_one_or_none()
    if not cand or cand.document_id != document_id:
        raise AccountingError(404, "Match not found")
    if cand.resolved:
        raise AccountingError(409, "This match was already resolved")
    if resolution not in ("linked", "dismissed"):
        raise AccountingError(400, "Resolution must be 'linked' or 'dismissed'")
    if doc.status not in (DocumentStatus.DUPLICATE_CANDIDATE, DocumentStatus.NEEDS_REVIEW,
                          DocumentStatus.READY_FOR_APPROVAL):
        raise AccountingError(409, f"Matches cannot be resolved while the bill is {doc.status.value}")

    if resolution == "dismissed":
        if not (reason or "").strip():
            raise AccountingError(400, "Give a reason for dismissing this match")
        cand.resolved, cand.resolution = True, "dismissed"
        cand.resolved_at, cand.resolved_by, cand.resolution_reason = datetime.utcnow(), actor, reason.strip()
        _event(db, doc.id, EventType.DUPLICATE_DISMISSED, actor, candidate_id=cand.id,
               qb_bill_id=cand.qb_bill_id, qb_doc_number=cand.qb_doc_number, reason=reason.strip())
        doc.revision += 1
        await db.flush()
        await update_document_status(db, doc, actor)
        await db.commit()
        return doc

    # Link: confirm the bill still exists in the verified company first.
    realm = cand.realm_id
    await db.commit()
    client, _company = await open_verified_company(db, required_realm=realm)
    bill = await client.get_bill(cand.qb_bill_id)
    if not bill.ok or not bill.data:
        raise AccountingError(502, f"Could not confirm QuickBooks bill {cand.qb_doc_number or cand.qb_bill_id} "
                                   f"({bill.error}). Nothing was linked; try again.", retryable=True)
    doc = await _reload(db, document_id)
    cand = await db.get(DuplicateCandidate, candidate_id)
    if cand.resolved:
        raise AccountingError(409, "This match was already resolved")
    now = datetime.utcnow()
    cand.resolved, cand.resolution = True, "linked"
    cand.resolved_at, cand.resolved_by, cand.resolution_reason = now, actor, (reason or "").strip() or None
    doc.qb_bill_id = cand.qb_bill_id
    doc.qb_bill_doc_number = bill.data.get("DocNumber") or cand.qb_doc_number
    doc.qb_realm_id = client.realm_id
    old_status = doc.status
    doc.status = DocumentStatus.LINKED_EXISTING
    doc.status_reason = None
    doc.revision += 1
    _event(db, doc.id, EventType.LINKED_TO_EXISTING, actor, qb_bill_id=cand.qb_bill_id,
           qb_doc_number=doc.qb_bill_doc_number, realm_id=client.realm_id, reason=cand.resolution_reason,
           matched_fields=cand.match_fields, created_by_app=False)
    _event(db, doc.id, EventType.STATUS_CHANGED, actor, old_status=old_status.value,
           new_status=DocumentStatus.LINKED_EXISTING.value)
    await db.commit()
    return doc


# ---------------------------------------------------------------------------
# Approval and bill creation
# ---------------------------------------------------------------------------

def _approval_snapshot(doc: Document, realm_id: str, company_name: Optional[str]) -> Dict[str, Any]:
    return {
        "company_realm_id": realm_id,
        "company_name": company_name,
        "vendor_name": doc.effective_vendor_name,
        "qb_vendor_id": doc.qb_vendor_id,
        "invoice_number": doc.effective_invoice_number,
        "invoice_date": doc.effective_invoice_date.isoformat() if doc.effective_invoice_date else None,
        "due_date": doc.effective_due_date.isoformat() if doc.effective_due_date else None,
        "total": str(doc.effective_total),
        "currency": doc.effective_currency,
        "qb_account_id": doc.qb_account_id,
        "qb_class_id": doc.qb_class_id,
        "property_id": doc.property_id,
        "revision": doc.revision,
    }


def response(doc: Document, success: bool, error: Optional[str] = None,
             retry_action: Optional[str] = None) -> Dict[str, Any]:
    return {
        "success": success,
        "document_id": doc.id,
        "status": doc.status.value,
        "revision": doc.revision,
        "qb_bill_id": doc.qb_bill_id,
        "qb_bill_doc_number": doc.qb_bill_doc_number,
        "qb_attachment_id": doc.qb_attachment_id,
        "qb_bill_url": bill_record_url(doc.qb_bill_id) if doc.qb_bill_id else None,
        "error": error,
        "requires_retry": retry_action is not None,
        "retry_action": retry_action,
    }


async def approve_and_create(db: AsyncSession, document_id: str, revision: int, actor: str) -> Dict[str, Any]:
    doc = await _reload(db, document_id)
    if doc.revision != revision:
        raise AccountingError(409, "This bill changed since you loaded it. Reload and review again.")
    if doc.status != DocumentStatus.READY_FOR_APPROVAL:
        raise AccountingError(409, f"This bill cannot be approved while it is {doc.status.value.replace('_', ' ')}.")
    missing = missing_fields(doc)
    if missing:
        raise AccountingError(400, f"Complete these fields first: {', '.join(missing)}")
    currency = doc.effective_currency
    if currency != settings.quickbooks_home_currency:
        raise AccountingError(400, f"Bills in {currency} are not supported in this pilot (company currency "
                                   f"{settings.quickbooks_home_currency}). Keep it in review.")
    if await open_candidates(db, doc.id):
        raise AccountingError(409, "Resolve the possible QuickBooks match first.")
    await db.commit()

    client, company_name = await open_verified_company(db)

    # Final accounting check immediately before posting (complete + all pages).
    check = await run_duplicate_check(db, document_id, client, actor)
    if check["candidates_found"]:
        raise AccountingError(409, "A possible existing bill was found in QuickBooks. Review the match; "
                                   "nothing was posted.")

    doc = await _reload(db, document_id)
    op_id = str(uuid.uuid4())
    snapshot = _approval_snapshot(doc, client.realm_id, company_name)
    invoice_number = doc.effective_invoice_number  # plain values: ORM objects expire on rollback
    vendor_id = doc.qb_vendor_id
    invoice_key = normalize_invoice_number(invoice_number)
    identity = f"{client.realm_id}|{vendor_id}|{invoice_key}"
    payload = build_bill_payload(
        vendor_id=doc.qb_vendor_id, account_id=doc.qb_account_id, doc_number=doc.effective_invoice_number,
        txn_date=doc.effective_invoice_date, due_date=doc.effective_due_date, total=doc.effective_total,
        class_id=doc.qb_class_id, currency=currency, memo=bill_operation_marker(doc.id, op_id),
    )

    # --- durable claim (one committed transaction, before the remote write) ---
    claimed = await db.execute(
        update(Document)
        .where(Document.id == document_id, Document.revision == revision,
               Document.status == DocumentStatus.READY_FOR_APPROVAL)
        .values(status=DocumentStatus.APPROVED, revision=revision + 1, status_reason=None,
                approved_at=datetime.utcnow(), approved_by=actor, approved_revision=revision,
                approved_values=snapshot, qb_realm_id=client.realm_id)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise AccountingError(409, "This bill is already being approved (another tab or click) or has changed. "
                                   "Reload to see its status.")
    other = (await db.execute(select(AccountingClaim).where(
        AccountingClaim.realm_id == client.realm_id, AccountingClaim.qb_vendor_id == vendor_id,
        AccountingClaim.invoice_key == invoice_key))).scalar_one_or_none()
    if other and other.document_id != document_id:
        other_doc = other.document_id
        await db.rollback()
        raise AccountingError(409, f"Another document ({other_doc}) already holds invoice "
                                   f"{invoice_number} for this vendor. Open that document or link "
                                   "its bill instead.")
    try:
        if not other:
            db.add(AccountingClaim(realm_id=client.realm_id, qb_vendor_id=vendor_id,
                                   invoice_key=invoice_key, document_id=document_id))
        db.add(Operation(
            id=op_id, request_id=op_id, document_id=document_id, operation_type=OperationType.CREATE_BILL,
            status=OperationStatus.PENDING, started_at=datetime.utcnow(), realm_id=client.realm_id,
            accounting_identity=identity, approved_revision=revision, payload=payload,
            request_payload_hash=payload_hash(payload),
        ))
        _event(db, document_id, EventType.APPROVAL_GRANTED, actor, **snapshot, operation_id=op_id)
        _event(db, document_id, EventType.BILL_CREATION_STARTED, "system", operation_id=op_id,
               realm_id=client.realm_id, request_id=op_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AccountingError(409, f"Another document already holds invoice {invoice_number} for "
                                   "this vendor.")

    op = await db.get(Operation, op_id)
    return await execute_create(db, document_id, op, client)


async def execute_create(db: AsyncSession, document_id: str, op: Operation, client) -> Dict[str, Any]:
    op_id, request_id, payload = op.id, op.request_id, op.payload
    await db.commit()
    try:
        result = await client.create_bill(payload, request_id=request_id)
    except Exception as e:  # unexpected adapter failure: treat as uncertain
        from ..providers.quickbooks_provider import WriteResult
        result = WriteResult("unknown", error=f"{type(e).__name__}: {e}")

    doc = await _reload(db, document_id)
    op = await db.get(Operation, op_id)
    op.retry_count = (op.retry_count or 0) + (1 if op.error_message else 0)

    if result.ok:
        await _finish_bill_created(db, doc, op, result.data, EventType.BILL_CREATED)
        return await attach_original(db, document_id, client, "system")

    if result.outcome == "failed":
        op.status, op.error_message, op.completed_at = OperationStatus.FAILED, result.error, datetime.utcnow()
        # Definitely not created: release this document's claim on the invoice.
        await db.execute(delete(AccountingClaim).where(AccountingClaim.document_id == doc.id))
        doc.status = DocumentStatus.NEEDS_REVIEW
        doc.approved_at = doc.approved_by = doc.approved_revision = doc.approved_values = None
        doc.status_reason = f"QuickBooks did not create the bill: {result.error}"
        doc.revision += 1
        _event(db, doc.id, EventType.BILL_CREATION_FAILED, "system", operation_id=op_id, error=result.error)
        _event(db, doc.id, EventType.APPROVAL_RELEASED, "system", operation_id=op_id,
               reason="Provider rejected the request; nothing was created")
        await update_document_status(db, doc, "system")
        await db.commit()
        return response(doc, False, doc.status_reason)

    op.status, op.error_message = OperationStatus.UNKNOWN, result.error
    doc.status = DocumentStatus.REMOTE_OUTCOME_UNKNOWN
    doc.status_reason = (f"QuickBooks did not confirm the bill ({result.error}). It may have been created. "
                         "Use 'Check QuickBooks and resume'; no new bill will be sent until it is checked.")
    doc.revision += 1
    _event(db, doc.id, EventType.BILL_OUTCOME_UNKNOWN, "system", operation_id=op_id, error=result.error)
    await db.commit()
    return response(doc, False, doc.status_reason, retry_action="reconcile")


async def _finish_bill_created(db: AsyncSession, doc: Document, op: Operation, bill: Dict[str, Any],
                               event: EventType) -> None:
    """Commit the bill ID and queue the attachment operation, in one transaction."""
    now = datetime.utcnow()
    op.status, op.result_id, op.completed_at, op.error_message = OperationStatus.COMPLETED, str(bill["Id"]), now, None
    doc.qb_bill_id = str(bill["Id"])
    doc.qb_bill_doc_number = bill.get("DocNumber")
    doc.qb_realm_id = op.realm_id
    doc.status = DocumentStatus.BILL_CREATED
    doc.status_reason = "Bill created; attaching the original PDF."
    doc.revision += 1
    attach_id = str(uuid.uuid4())
    db.add(Operation(id=attach_id, request_id=attach_id, document_id=doc.id,
                     operation_type=OperationType.ATTACH_FILE, status=OperationStatus.PENDING,
                     started_at=now, realm_id=op.realm_id, target_id=doc.qb_bill_id,
                     payload={"sha256": doc.sha256_hash}))
    _event(db, doc.id, event, "system", qb_bill_id=doc.qb_bill_id, doc_number=doc.qb_bill_doc_number,
           realm_id=op.realm_id, operation_id=op.id, total=str(bill.get("TotalAmt")),
           balance=str(bill.get("Balance")) if bill.get("Balance") is not None else None, created_by_app=True)
    await db.commit()


async def _latest_op(db: AsyncSession, document_id: str, op_type: OperationType) -> Optional[Operation]:
    return (await db.execute(
        select(Operation).where(Operation.document_id == document_id, Operation.operation_type == op_type)
        .order_by(Operation.created_at.desc(), literal_column("operations.rowid").desc())
    )).scalars().first()


async def reconcile_bill(db: AsyncSession, document_id: str, actor: str) -> Dict[str, Any]:
    """Resolve an uncertain create by searching QuickBooks before any new request."""
    doc = await _reload(db, document_id)
    if doc.status not in (DocumentStatus.REMOTE_OUTCOME_UNKNOWN, DocumentStatus.APPROVED):
        raise AccountingError(409, "There is no unconfirmed QuickBooks write for this bill.")
    op = await _latest_op(db, document_id, OperationType.CREATE_BILL)
    if not op or op.status not in (OperationStatus.UNKNOWN, OperationStatus.PENDING, OperationStatus.IN_PROGRESS):
        raise AccountingError(409, "No unconfirmed bill operation was found.")
    vendor_id = (op.payload or {}).get("VendorRef", {}).get("value")
    realm, op_id = op.realm_id, op.id
    await db.commit()

    client, _company = await open_verified_company(db, required_realm=realm)
    lookup = await client.find_vendor_bills(vendor_id)
    if not lookup.complete:
        doc = await _reload(db, document_id)
        _event(db, doc.id, EventType.ACCOUNTING_CHECK_FAILED, actor, error=lookup.error, purpose="reconcile",
               operation_id=op_id)
        await db.commit()
        raise AccountingError(502, f"Could not finish searching QuickBooks ({lookup.error}). The outcome is still "
                                   "unknown; nothing was sent. Try again.", retryable=True)

    marker = f"operation {op_id}"
    ours = [b for b in lookup.bills if marker in (b.get("PrivateNote") or "")]
    doc = await _reload(db, document_id)
    op = await db.get(Operation, op_id)
    if ours:
        _event(db, doc.id, EventType.BILL_RECONCILED, actor, found=True, qb_bill_id=str(ours[0]["Id"]),
               operation_id=op_id, matches=len(ours))
        await _finish_bill_created(db, doc, op, ours[0], EventType.BILL_CREATED)
        return await attach_original(db, document_id, client, actor)

    others = [b for b in lookup.bills if compare_bill(doc, b)[0]]
    if others:
        op.status, op.completed_at = OperationStatus.FAILED, datetime.utcnow()
        op.error_message = "Not created by this app; a matching bill already exists"
        _event(db, doc.id, EventType.BILL_RECONCILED, actor, found=False, operation_id=op_id,
               matching_existing=[str(b["Id"]) for b in others])
        doc.status = DocumentStatus.NEEDS_REVIEW
        doc.approved_at = doc.approved_by = doc.approved_revision = doc.approved_values = None
        doc.revision += 1
        await db.commit()
        await run_duplicate_check(db, document_id, client, actor)
        doc = await _reload(db, document_id)
        return response(doc, False, "QuickBooks already has a matching bill that this app did not create. "
                                    "Review the match.")

    # Confirmed absent: resend with the SAME request ID (Intuit returns the original
    # response if that request had in fact been processed).
    _event(db, doc.id, EventType.BILL_RECONCILED, actor, found=False, operation_id=op_id,
           action="resend_same_request_id", pages_read=lookup.pages_read)
    doc.status = DocumentStatus.APPROVED
    doc.status_reason = "Not found in QuickBooks; re-sending the same request."
    await db.commit()
    return await execute_create(db, document_id, op, client)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def _attachment_filename(doc: Document) -> str:
    ext = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}.get(doc.content_type, "")
    stem = "".join(ch for ch in (doc.effective_invoice_number or "bill") if ch.isalnum() or ch in "-_") or "bill"
    return f"{stem}-{doc.sha256_hash[:12]}{ext}"


async def verify_existing_attachments(client, doc: Document) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """Return (verdict, attachable, explanation); verdict in match|none|inconclusive|lookup_failed."""
    listing = await client.list_attachments("Bill", doc.qb_bill_id)
    if not listing.ok:
        return "lookup_failed", None, listing.error or "attachment lookup failed"
    inconclusive: List[str] = []
    for att in listing.data or []:
        note = att.get("Note") or ""
        size = att.get("Size")
        if f"sha256:{doc.sha256_hash}" in note and (size in (None, "") or int(float(size)) == doc.file_size):
            return "match", att, "attachment note carries this file's SHA-256"
        if size not in (None, "") and int(float(size)) != doc.file_size:
            continue  # different size: definitely another file
        content = await client.download_attachment(att)
        if content.ok:
            if hashlib.sha256(content.data).hexdigest() == doc.sha256_hash:
                return "match", att, "downloaded attachment bytes match this file's SHA-256"
            continue
        inconclusive.append(f"{att.get('FileName') or att.get('Id')}: {content.error}")
    if inconclusive:
        return "inconclusive", None, "could not verify " + "; ".join(inconclusive)
    return "none", None, "no attachment on the bill is this file"


async def attach_original(db: AsyncSession, document_id: str, client, actor: str) -> Dict[str, Any]:
    """Attach the stored original to the document's bill, verifying before and after."""
    doc = await _reload(db, document_id)
    op = await _latest_op(db, document_id, OperationType.ATTACH_FILE)
    if not doc.qb_bill_id or not op:
        raise AccountingError(409, "No bill to attach to.")
    if op.target_id and op.target_id != doc.qb_bill_id:
        raise AccountingError(409, "Attachment operation targets a different bill; refusing to continue.")
    op_id, bill_id = op.id, doc.qb_bill_id
    await db.commit()

    verdict, att, why = await verify_existing_attachments(client, doc)
    doc = await _reload(db, document_id)
    op = await db.get(Operation, op_id)
    if verdict == "match":
        return await _mark_recorded(db, doc, op, att, EventType.ATTACHMENT_VERIFIED, actor, why)
    if verdict in ("lookup_failed", "inconclusive"):
        doc.status = DocumentStatus.ATTACHMENT_PENDING
        doc.status_reason = (f"Bill {doc.qb_bill_doc_number or bill_id} exists, but its attachments could not be "
                             f"checked ({why}). Retry, or open the bill in QuickBooks to check.")
        doc.revision += 1
        op.error_message = why
        _event(db, doc.id, EventType.ATTACHMENT_FAILED, "system", qb_bill_id=bill_id, reason=why,
               stage="verify_existing")
        await db.commit()
        return response(doc, True, doc.status_reason, retry_action="retry_attachment")

    file_path = settings.documents_path / doc.storage_path
    content = file_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != doc.sha256_hash:
        raise AccountingError(500, "Stored original does not match its fingerprint; not uploading.")
    _event(db, doc.id, EventType.ATTACHMENT_STARTED, "system", qb_bill_id=bill_id, operation_id=op_id)
    await db.commit()
    try:
        result = await client.upload_attachment(
            entity_type="Bill", entity_id=bill_id, content=content, filename=_attachment_filename(doc),
            content_type=doc.content_type, note=attachment_note(doc.sha256_hash, doc.id),
            request_id=op.request_id,
        )
    except Exception as e:
        from ..providers.quickbooks_provider import WriteResult
        result = WriteResult("unknown", error=f"{type(e).__name__}: {e}")

    doc = await _reload(db, document_id)
    op = await db.get(Operation, op_id)
    if result.ok:
        return await _mark_recorded(db, doc, op, result.data, EventType.ATTACHMENT_COMPLETED, actor,
                                    "uploaded and confirmed by QuickBooks")
    op.status = OperationStatus.UNKNOWN if result.outcome == "unknown" else OperationStatus.FAILED
    op.error_message = result.error
    doc.status = DocumentStatus.ATTACHMENT_PENDING
    if result.outcome == "unknown":
        doc.status_reason = (f"Bill {doc.qb_bill_doc_number or bill_id} created; the PDF upload was not confirmed "
                             f"({result.error}). Retry checks the bill's attachments before uploading again.")
        _event(db, doc.id, EventType.ATTACHMENT_OUTCOME_UNKNOWN, "system", qb_bill_id=bill_id, error=result.error)
    else:
        doc.status_reason = f"Bill {doc.qb_bill_doc_number or bill_id} created; attaching the PDF failed: {result.error}"
        _event(db, doc.id, EventType.ATTACHMENT_FAILED, "system", qb_bill_id=bill_id, error=result.error)
    doc.revision += 1
    await db.commit()
    return response(doc, True, doc.status_reason, retry_action="retry_attachment")


async def _mark_recorded(db: AsyncSession, doc: Document, op: Operation, att: Dict[str, Any],
                         event: EventType, actor: str, evidence: str) -> Dict[str, Any]:
    op.status, op.result_id, op.completed_at = OperationStatus.COMPLETED, str(att.get("Id")), datetime.utcnow()
    doc.qb_attachment_id = str(att.get("Id"))
    doc.status = DocumentStatus.RECORDED
    doc.status_reason = None
    doc.revision += 1
    _event(db, doc.id, event, actor, attachment_id=doc.qb_attachment_id, qb_bill_id=doc.qb_bill_id,
           filename=att.get("FileName"), evidence=evidence, sha256=doc.sha256_hash)
    await db.commit()
    return response(doc, True)


async def retry_attachment(db: AsyncSession, document_id: str, actor: str) -> Dict[str, Any]:
    doc = await _reload(db, document_id)
    if doc.status not in (DocumentStatus.ATTACHMENT_PENDING, DocumentStatus.BILL_CREATED) or not doc.qb_bill_id:
        raise AccountingError(409, "This bill is not waiting for its attachment.")
    realm = doc.qb_realm_id
    op = await _latest_op(db, document_id, OperationType.ATTACH_FILE)
    if not op or op.status == OperationStatus.FAILED or op.target_id != doc.qb_bill_id:
        # A definite failure gets a fresh idempotency key; an unknown one keeps its key.
        new_id = str(uuid.uuid4())
        db.add(Operation(id=new_id, request_id=new_id, document_id=doc.id, operation_type=OperationType.ATTACH_FILE,
                         status=OperationStatus.PENDING, started_at=datetime.utcnow(), realm_id=realm,
                         target_id=doc.qb_bill_id, payload={"sha256": doc.sha256_hash}))
    elif op.status in (OperationStatus.UNKNOWN, OperationStatus.IN_PROGRESS):
        op.status = OperationStatus.PENDING
        op.retry_count = (op.retry_count or 0) + 1
    await db.commit()
    client, _ = await open_verified_company(db, required_realm=realm)
    return await attach_original(db, document_id, client, actor)


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

async def recover_interrupted_operations(db: AsyncSession) -> int:
    """After a restart, surface writes that were in flight with a safe resume action."""
    ops = (await db.execute(select(Operation).where(
        Operation.status.in_([OperationStatus.PENDING, OperationStatus.IN_PROGRESS]),
        Operation.operation_type.in_([OperationType.CREATE_BILL, OperationType.ATTACH_FILE]),
    ))).scalars().all()
    count = 0
    for op in ops:
        doc = await db.get(Document, op.document_id)
        op.status = OperationStatus.UNKNOWN
        op.error_message = "Application restarted before the result was recorded"
        if op.operation_type == OperationType.CREATE_BILL and doc.status == DocumentStatus.APPROVED:
            doc.status = DocumentStatus.REMOTE_OUTCOME_UNKNOWN
            doc.status_reason = ("The app restarted while creating this bill. Use 'Check QuickBooks and resume'; "
                                 "it searches QuickBooks before sending anything.")
        elif op.operation_type == OperationType.ATTACH_FILE and doc.status in (
                DocumentStatus.BILL_CREATED, DocumentStatus.ATTACHMENT_PENDING):
            doc.status = DocumentStatus.ATTACHMENT_PENDING
            doc.status_reason = (f"The app restarted before the PDF was attached to bill "
                                 f"{doc.qb_bill_doc_number or doc.qb_bill_id}. Retry attachment resumes on the same bill.")
        else:
            continue
        doc.revision += 1
        _event(db, doc.id, EventType.OPERATION_INTERRUPTED, "system", operation_id=op.id,
               operation_type=op.operation_type.value, qb_bill_id=doc.qb_bill_id)
        count += 1
    await db.commit()
    return count
