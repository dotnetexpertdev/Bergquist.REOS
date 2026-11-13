"""Document review service: questions, actions, versioned corrections and status."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..models import (
    AccountMapping, Arrival, AuditEvent, Document, DocumentStatus, DuplicateCandidate, EventType,
    Operation, OperationStatus, Property, VendorMapping,
)
from ..schemas import ArrivalResponse, DocumentDetail, DocumentListItem, DocumentUpdateRequest

# Field -> question, in the order they are asked.
REQUIRED_FIELDS: List[Tuple[str, str, str]] = [
    ("vendor_name", "Vendor", "Who is the vendor for this bill?"),
    ("total", "Total", "What is the total amount due?"),
    ("currency", "Currency", "What currency is this bill in?"),
    ("invoice_number", "Invoice number", "What is the invoice number?"),
    ("invoice_date", "Invoice date", "What is the invoice date?"),
    ("property", "Property", "Which property is this for?"),
    ("qb_vendor", "QuickBooks vendor", "Which QuickBooks vendor should this bill use?"),
    ("qb_account", "Expense account", "Which expense account should this bill use?"),
]

# Statuses in which a remote accounting write is in flight or its result is uncertain.
POSTING_STATUSES = (DocumentStatus.APPROVED, DocumentStatus.BILL_CREATED, DocumentStatus.REMOTE_OUTCOME_UNKNOWN)
# Statuses in which a QuickBooks bill already exists for this document.
POSTED_STATUSES = (DocumentStatus.RECORDED, DocumentStatus.LINKED_EXISTING, DocumentStatus.ATTACHMENT_PENDING)
REVIEW_STATUSES = (
    DocumentStatus.NEEDS_REVIEW, DocumentStatus.READY_FOR_APPROVAL, DocumentStatus.EXTRACTION_FAILED,
    DocumentStatus.DUPLICATE_CANDIDATE,
)


def normalize_invoice_number(value: Optional[str]) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


def missing_fields(doc: Document) -> List[str]:
    present = {
        "vendor_name": bool(doc.effective_vendor_name),
        "total": doc.effective_total is not None,
        "currency": bool(doc.effective_currency),
        "invoice_number": bool(doc.effective_invoice_number),
        "invoice_date": doc.effective_invoice_date is not None,
        "property": bool(doc.property_id),
        "qb_vendor": bool(doc.qb_vendor_id),
        "qb_account": bool(doc.qb_account_id),
    }
    return [name for name, _label, _q in REQUIRED_FIELDS if not present[name]]


def get_pending_question(doc: Document, open_candidates: Optional[List[DuplicateCandidate]] = None) -> Optional[str]:
    """The next question the owner must answer, phrased for the list and detail views."""
    if doc.status == DocumentStatus.EXTRACTION_FAILED:
        return "The bill could not be read automatically. Retry reading or enter the values yourself."
    if doc.status == DocumentStatus.REMOTE_OUTCOME_UNKNOWN:
        return "QuickBooks did not confirm whether the bill was created. Check QuickBooks before trying again."
    if doc.status == DocumentStatus.ATTACHMENT_PENDING:
        return f"Bill {doc.qb_bill_doc_number or doc.qb_bill_id} exists in QuickBooks but the PDF is not attached yet."
    if doc.status == DocumentStatus.DUPLICATE_CANDIDATE:
        if open_candidates:
            c = open_candidates[0]
            return f"Is this the same invoice as QuickBooks bill {c.qb_doc_number or c.qb_bill_id}?"
        return "A possible matching bill was found in QuickBooks. Is it the same invoice?"
    if doc.status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.READY_FOR_APPROVAL, DocumentStatus.RECEIVED):
        missing = missing_fields(doc)
        if missing:
            return dict((n, q) for n, _l, q in REQUIRED_FIELDS)[missing[0]]
        if doc.last_accounting_check_ok is False:
            return "The QuickBooks duplicate check failed. Retry the check before approving."
    if doc.qb_discrepancy:
        return "This bill was corrected after it was recorded. Update QuickBooks manually if needed."
    return None


def get_available_actions(doc: Document) -> List[str]:
    actions = ["view_original", "view_history"]
    if doc.status == DocumentStatus.EXTRACTION_FAILED:
        actions.append("retry_extraction")
    if doc.status in REVIEW_STATUSES or doc.status in POSTED_STATUSES:
        actions.append("update_fields")
    if doc.status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.READY_FOR_APPROVAL,
                      DocumentStatus.DUPLICATE_CANDIDATE) and doc.qb_vendor_id and doc.effective_invoice_number:
        actions.append("check_duplicates")
    if doc.status == DocumentStatus.READY_FOR_APPROVAL and not missing_fields(doc):
        actions.append("approve")
    if doc.status == DocumentStatus.DUPLICATE_CANDIDATE:
        actions.extend(["link_existing", "dismiss_duplicate"])
    if doc.status in (DocumentStatus.REMOTE_OUTCOME_UNKNOWN, DocumentStatus.APPROVED):
        actions.append("reconcile")
    if doc.status in (DocumentStatus.ATTACHMENT_PENDING, DocumentStatus.BILL_CREATED):
        actions.append("retry_attachment")
    if doc.qb_bill_id:
        actions.append("view_in_quickbooks")
    return actions


async def _open_candidates(db: AsyncSession, document_id: str) -> List[DuplicateCandidate]:
    return list((await db.execute(
        select(DuplicateCandidate).where(
            DuplicateCandidate.document_id == document_id, DuplicateCandidate.resolved == False  # noqa: E712
        ).order_by(DuplicateCandidate.found_at)
    )).scalars().all())


def _list_item(doc: Document, open_candidates: Optional[List[DuplicateCandidate]] = None) -> DocumentListItem:
    arrivals = sorted(doc.arrivals, key=lambda a: a.arrived_at)
    first = arrivals[0] if arrivals else None
    return DocumentListItem(
        id=doc.id,
        status=doc.status.value,
        status_reason=doc.status_reason,
        created_at=doc.created_at,
        revision=doc.revision,
        vendor_name=doc.effective_vendor_name,
        invoice_number=doc.effective_invoice_number,
        invoice_date=doc.effective_invoice_date,
        total=doc.effective_total,
        currency=doc.effective_currency,
        document_type=doc.extracted_document_type,
        property_id=doc.property_id,
        property_name=doc.property_name,
        qb_bill_id=doc.qb_bill_id,
        arrival_count=len(arrivals),
        first_arrival_method=first.method.value if first else None,
        first_arrival_filename=first.original_filename if first else None,
        pending_question=get_pending_question(doc, open_candidates),
        missing_fields=missing_fields(doc),
    )


async def get_document_list(
    db: AsyncSession,
    status_filter: Optional[List[DocumentStatus]] = None,
    property_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[DocumentListItem]:
    query = (
        select(Document)
        .options(selectinload(Document.arrivals), selectinload(Document.assigned_property),
                 selectinload(Document.duplicate_candidates))
        .order_by(Document.created_at.desc())
    )
    if status_filter:
        query = query.where(Document.status.in_(status_filter))
    if property_id:
        query = query.where(Document.property_id == property_id)
    documents = (await db.execute(query.limit(limit).offset(offset))).scalars().all()
    return [
        _list_item(doc, [c for c in doc.duplicate_candidates if not c.resolved])
        for doc in documents
    ]


async def load_document(db: AsyncSession, document_id: str) -> Optional[Document]:
    return (await db.execute(
        select(Document)
        .options(selectinload(Document.arrivals), selectinload(Document.assigned_property))
        .where(Document.id == document_id)
    )).scalar_one_or_none()


async def get_document_detail(db: AsyncSession, document_id: str) -> Optional[DocumentDetail]:
    from ..providers.quickbooks_provider import bill_record_url
    from .accounting import company_status

    doc = await load_document(db, document_id)
    if not doc:
        return None

    qb_vendor_name = None
    qb_account_name = None
    if doc.qb_vendor_id:
        m = (await db.execute(select(VendorMapping).where(VendorMapping.qb_vendor_id == doc.qb_vendor_id))).scalars().first()
        qb_vendor_name = m.qb_vendor_name if m else None
    if doc.qb_account_id:
        m = (await db.execute(select(AccountMapping).where(AccountMapping.qb_account_id == doc.qb_account_id))).scalars().first()
        qb_account_name = m.qb_account_name if m else None

    open_candidates = await _open_candidates(db, doc.id)
    company = await company_status(db)
    raw = doc.extracted_raw if isinstance(doc.extracted_raw, dict) else {}

    arrivals = [
        ArrivalResponse(
            id=a.id, arrived_at=a.arrived_at, method=a.method.value, original_filename=a.original_filename,
            email_from=a.email_from, email_subject=a.email_subject, email_received_at=a.email_received_at,
            gmail_message_id=a.gmail_message_id, uploaded_by=a.uploaded_by,
        )
        for a in sorted(doc.arrivals, key=lambda a: a.arrived_at)
    ]

    return DocumentDetail(
        id=doc.id, sha256_hash=doc.sha256_hash, content_type=doc.content_type, file_size=doc.file_size,
        status=doc.status.value, status_reason=doc.status_reason, revision=doc.revision, created_at=doc.created_at,
        extracted_at=doc.extracted_at, extraction_model=doc.extraction_model,
        extracted_document_type=doc.extracted_document_type, extracted_vendor_name=doc.extracted_vendor_name,
        extracted_invoice_number=doc.extracted_invoice_number, extracted_invoice_date=doc.extracted_invoice_date,
        extracted_due_date=doc.extracted_due_date, extracted_total=doc.extracted_total,
        extracted_currency=doc.extracted_currency, extracted_service_address=doc.extracted_service_address,
        extraction_uncertain_fields=raw.get("uncertain_fields") or [],
        extraction_source_text=raw.get("source_text"),
        reviewed_vendor_name=doc.reviewed_vendor_name, reviewed_invoice_number=doc.reviewed_invoice_number,
        reviewed_invoice_date=doc.reviewed_invoice_date, reviewed_due_date=doc.reviewed_due_date,
        reviewed_total=doc.reviewed_total, reviewed_currency=doc.reviewed_currency,
        effective_vendor_name=doc.effective_vendor_name, effective_invoice_number=doc.effective_invoice_number,
        effective_invoice_date=doc.effective_invoice_date, effective_due_date=doc.effective_due_date,
        effective_total=doc.effective_total, effective_currency=doc.effective_currency,
        property_id=doc.property_id, property_name=doc.property_name,
        qb_vendor_id=doc.qb_vendor_id, qb_vendor_name=qb_vendor_name,
        qb_account_id=doc.qb_account_id, qb_account_name=qb_account_name, qb_class_id=doc.qb_class_id,
        qb_bill_id=doc.qb_bill_id, qb_bill_doc_number=doc.qb_bill_doc_number, qb_realm_id=doc.qb_realm_id,
        qb_attachment_id=doc.qb_attachment_id,
        qb_bill_url=bill_record_url(doc.qb_bill_id) if doc.qb_bill_id else None,
        qb_discrepancy=doc.qb_discrepancy,
        todoist_task_id=doc.todoist_task_id,
        approved_at=doc.approved_at, approved_by=doc.approved_by, approved_revision=doc.approved_revision,
        last_accounting_check_at=doc.last_accounting_check_at,
        last_accounting_check_ok=doc.last_accounting_check_ok,
        last_accounting_check_error=doc.last_accounting_check_error,
        arrivals=arrivals,
        available_actions=get_available_actions(doc),
        pending_question=get_pending_question(doc, open_candidates),
        missing_fields=missing_fields(doc),
        company=company,
    )


FIELD_MAP = [
    # (request attribute, document attribute, audit name)
    ("reviewed_vendor_name", "reviewed_vendor_name", "vendor_name"),
    ("reviewed_invoice_number", "reviewed_invoice_number", "invoice_number"),
    ("reviewed_invoice_date", "reviewed_invoice_date", "invoice_date"),
    ("reviewed_due_date", "reviewed_due_date", "due_date"),
    ("reviewed_total", "reviewed_total", "total"),
    ("reviewed_currency", "reviewed_currency", "currency"),
    ("property_id", "property_id", "property_id"),
    ("qb_vendor_id", "qb_vendor_id", "qb_vendor_id"),
    ("qb_account_id", "qb_account_id", "qb_account_id"),
    ("qb_class_id", "qb_class_id", "qb_class_id"),
]
ACCOUNTING_FIELDS = {"vendor_name", "invoice_number", "invoice_date", "due_date", "total", "currency",
                     "qb_vendor_id", "qb_account_id", "qb_class_id"}


class UpdateError(Exception):
    def __init__(self, status_code: int, message: str, field_errors: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.field_errors = field_errors or {}


async def update_document(
    db: AsyncSession, document_id: str, update: DocumentUpdateRequest, actor: str, source: str = "typed",
) -> Document:
    """Apply a versioned correction. Never posts or changes anything in QuickBooks."""
    doc = await load_document(db, document_id)
    if not doc:
        raise UpdateError(404, "Document not found")
    if doc.revision != update.revision:
        raise UpdateError(409, f"This bill was changed elsewhere (current revision {doc.revision}, "
                               f"yours {update.revision}). Reload to see the latest values.")
    if doc.status in POSTING_STATUSES:
        raise UpdateError(409, "A QuickBooks write for this bill is in progress or unconfirmed. "
                               "Resolve it before editing.")
    if doc.status in (DocumentStatus.RECEIVED, DocumentStatus.READING):
        raise UpdateError(409, "The bill is still being read. Try again in a moment.")

    errors: Dict[str, str] = {}
    if update.reviewed_currency is not None and not re.fullmatch(r"[A-Z]{3}", update.reviewed_currency):
        errors["reviewed_currency"] = "Use a 3-letter currency code such as USD"
    if update.reviewed_total is not None and update.reviewed_total < 0:
        errors["reviewed_total"] = "Amount cannot be negative"
    if update.property_id:
        if not (await db.execute(select(Property).where(Property.id == update.property_id))).scalar_one_or_none():
            errors["property_id"] = "Unknown property"
    if errors:
        raise UpdateError(400, "Some values are invalid", errors)

    changes: Dict[str, Dict[str, Any]] = {}
    for req_attr, doc_attr, name in FIELD_MAP:
        new = getattr(update, req_attr)
        if new is None:
            continue
        if isinstance(new, str):
            new = new.strip() or None
            if new is None:
                continue
        old = getattr(doc, doc_attr)
        if new != old:
            changes[name] = {"old": None if old is None else str(old), "new": str(new)}
            setattr(doc, doc_attr, new)

    if not changes:
        return doc

    doc.revision += 1
    for name, change in changes.items():
        details = {"field": name, "old_value": change["old"], "new_value": change["new"], "source": source}
        if name == "property_id":
            old_p = (await db.get(Property, change["old"])) if change["old"] else None
            new_p = await db.get(Property, change["new"])
            details["old_display"] = old_p.name if old_p else None
            details["new_display"] = new_p.name if new_p else None
        db.add(AuditEvent(document_id=doc.id, event_type=EventType.FIELD_CORRECTED, actor=actor, details=details))

    if doc.status in POSTED_STATUSES:
        affected = sorted(set(changes) & ACCOUNTING_FIELDS)
        if affected:
            note = (f"Corrected locally on {datetime.utcnow():%Y-%m-%d %H:%M} UTC after QuickBooks bill "
                    f"{doc.qb_bill_doc_number or doc.qb_bill_id} existed: {', '.join(affected)}. "
                    "QuickBooks was not changed.")
            doc.qb_discrepancy = note
            db.add(AuditEvent(document_id=doc.id, event_type=EventType.QB_DISCREPANCY_FLAGGED, actor="system",
                              details={"fields": affected, "qb_bill_id": doc.qb_bill_id}))
    else:
        # A correction changes the facts used by the QuickBooks duplicate check.
        if changes.keys() & {"vendor_name", "invoice_number", "total", "currency", "invoice_date", "qb_vendor_id"}:
            doc.last_accounting_check_ok = None
        await update_document_status(db, doc, actor)
    return doc


async def update_document_status(db: AsyncSession, doc: Document, actor: str) -> None:
    """Recompute review status. Never moves a document into or out of posting states."""
    if doc.status in POSTING_STATUSES or doc.status in POSTED_STATUSES:
        return
    old_status = doc.status
    if await _open_candidates(db, doc.id):
        new_status = DocumentStatus.DUPLICATE_CANDIDATE
    elif not missing_fields(doc):
        new_status = DocumentStatus.READY_FOR_APPROVAL
    else:
        new_status = DocumentStatus.NEEDS_REVIEW
    if new_status != old_status:
        doc.status = new_status
        if old_status == DocumentStatus.EXTRACTION_FAILED:
            doc.status_reason = None
        db.add(AuditEvent(document_id=doc.id, event_type=EventType.STATUS_CHANGED, actor=actor,
                          details={"old_status": old_status.value, "new_status": new_status.value}))


async def get_documents_needing_review(db: AsyncSession, limit: int = 50) -> List[DocumentListItem]:
    return await get_document_list(db, status_filter=[
        DocumentStatus.NEEDS_REVIEW, DocumentStatus.EXTRACTION_FAILED, DocumentStatus.DUPLICATE_CANDIDATE,
        DocumentStatus.ATTACHMENT_PENDING, DocumentStatus.REMOTE_OUTCOME_UNKNOWN, DocumentStatus.APPROVED,
        DocumentStatus.BILL_CREATED,
    ], limit=limit)


async def get_documents_ready_for_approval(db: AsyncSession, limit: int = 50) -> List[DocumentListItem]:
    return await get_document_list(db, status_filter=[DocumentStatus.READY_FOR_APPROVAL], limit=limit)


async def get_document_history(db: AsyncSession, document_id: str) -> List[AuditEvent]:
    return list((await db.execute(
        select(AuditEvent).where(AuditEvent.document_id == document_id)
        .order_by(AuditEvent.occurred_at.asc(), literal_column("audit_events.rowid"))
    )).scalars().all())
