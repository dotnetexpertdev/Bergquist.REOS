"""Document extraction service.

The original is already stored before this runs. A failed or unreadable
extraction never touches the original; the document moves to
``extraction_failed`` with a reason and offers retry or manual review.
"""

import re
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    AccountMapping, Arrival, AuditEvent, Document, DocumentStatus, EventType, Property,
    ProviderConnection, VendorMapping,
)
from ..providers.registry import ai
from .documents import update_document_status


def _normalize_address(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


async def suggest_property(db: AsyncSession, service_address: Optional[str]) -> Optional[str]:
    """Property ID only when exactly one property's address matches the service address."""
    target = _normalize_address(service_address)
    if not target:
        return None
    props = (await db.execute(select(Property).where(Property.address.is_not(None)))).scalars().all()
    matches: List[Property] = []
    for prop in props:
        addr = _normalize_address(prop.address)
        if not addr:
            continue
        # Compare the full address, or its street line (text before the first comma/newline).
        street = _normalize_address(re.split(r"[,\n]", prop.address)[0])
        if addr in target or target in addr or (len(street) >= 6 and street in target):
            matches.append(prop)
    return matches[0].id if len(matches) == 1 else None


async def connected_realm(db: AsyncSession) -> Optional[str]:
    conn = (await db.execute(
        select(ProviderConnection).where(ProviderConnection.provider == "quickbooks")
    )).scalar_one_or_none()
    return conn.realm_id if conn and conn.is_valid else None


async def suggest_accounting(db: AsyncSession, doc: Document) -> None:
    """Fill vendor/account from the company-scoped mappings when unambiguous."""
    realm = await connected_realm(db)
    if not realm:
        return
    name = (doc.effective_vendor_name or "").strip()
    if name and not doc.qb_vendor_id:
        rows = (await db.execute(select(VendorMapping).where(VendorMapping.realm_id == realm))).scalars().all()
        hits = {m.qb_vendor_id for m in rows if m.extracted_name.strip().lower() == name.lower()}
        if len(hits) == 1:
            doc.qb_vendor_id = hits.pop()
    if not doc.qb_account_id:
        haystack = f"{name} {doc.extracted_document_type or ''}".lower()
        rows = (await db.execute(select(AccountMapping).where(AccountMapping.realm_id == realm))).scalars().all()
        hits = {m.qb_account_id for m in rows if m.category and m.category.lower() in haystack}
        if len(hits) == 1:  # conflicting mappings are left for the reviewer
            doc.qb_account_id = hits.pop()


async def extract_document(db: AsyncSession, document_id: str) -> Tuple[bool, Optional[str]]:
    """Run extraction for one document. Returns (success, error)."""
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        return False, "Document not found"
    if doc.status not in (DocumentStatus.RECEIVED, DocumentStatus.EXTRACTION_FAILED, DocumentStatus.READING):
        return False, f"Document not in extractable status: {doc.status.value}"

    file_path = settings.documents_path / doc.storage_path
    first_arrival = (await db.execute(
        select(Arrival).where(Arrival.document_id == doc.id).order_by(Arrival.arrived_at)
    )).scalars().first()
    filename = (first_arrival.original_filename if first_arrival else None) or file_path.name

    # Durable "reading" state before the remote call (and release the write lock).
    doc.status = DocumentStatus.READING
    doc.status_reason = None
    doc.revision += 1
    db.add(AuditEvent(document_id=doc.id, event_type=EventType.EXTRACTION_STARTED, actor="system",
                      details={"model": settings.openai_extraction_model}))
    await db.commit()

    if not file_path.exists():
        extracted, error = None, "Stored original file is missing"
    else:
        extracted, error = await ai().extract_document_data(file_path, doc.content_type, filename)

    if error:
        doc.status = DocumentStatus.EXTRACTION_FAILED
        doc.status_reason = error
        doc.revision += 1
        db.add(AuditEvent(document_id=doc.id, event_type=EventType.EXTRACTION_FAILED, actor="system",
                          details={"error": error}))
        await db.commit()
        return False, error

    doc.extracted_at = datetime.utcnow()
    doc.extraction_model = settings.openai_extraction_model
    doc.extraction_prompt_version = _prompt_version()
    doc.extracted_document_type = extracted.get("document_type")
    doc.extracted_vendor_name = extracted.get("vendor_name")
    doc.extracted_invoice_number = extracted.get("invoice_number")
    doc.extracted_invoice_date = extracted.get("invoice_date")
    doc.extracted_due_date = extracted.get("due_date")
    doc.extracted_total = extracted.get("total")
    doc.extracted_currency = extracted.get("currency")  # stays None when not established
    doc.extracted_service_address = extracted.get("service_address")
    doc.extracted_raw = {
        "response": extracted.get("raw_response"),
        "uncertain_fields": extracted.get("uncertain_fields") or [],
        "source_text": extracted.get("source_text"),
        "source_page": extracted.get("source_page"),
        "confidence": extracted.get("confidence"),
    }

    if not doc.property_id:
        doc.property_id = await suggest_property(db, doc.extracted_service_address)
    await suggest_accounting(db, doc)
    doc.revision += 1

    db.add(AuditEvent(
        document_id=doc.id, event_type=EventType.EXTRACTION_COMPLETED, actor="system",
        details={
            "vendor": doc.extracted_vendor_name,
            "invoice_number": doc.extracted_invoice_number,
            "total": str(doc.extracted_total) if doc.extracted_total is not None else None,
            "currency": doc.extracted_currency,
            "service_address": doc.extracted_service_address,
            "property_suggested": bool(doc.property_id),
            "uncertain_fields": extracted.get("uncertain_fields") or [],
        },
    ))
    await update_document_status(db, doc, "system")
    await db.commit()
    return True, None


def _prompt_version() -> str:
    provider = ai()
    return getattr(provider, "EXTRACTION_PROMPT_VERSION", "test")


async def process_pending_extractions(db: AsyncSession) -> int:
    """Extract documents left in received/reading (e.g. after a restart)."""
    ids = (await db.execute(
        select(Document.id).where(Document.status.in_([DocumentStatus.RECEIVED, DocumentStatus.READING]))
    )).scalars().all()
    await db.commit()
    processed = 0
    for document_id in ids:
        ok, _ = await extract_document(db, document_id)
        processed += int(ok)
    return processed
