"""Background processing shared by upload and Gmail intake."""

from typing import Iterable

from sqlalchemy import select

from ..database import get_db_context
from ..models import Document, DocumentStatus
from .accounting import auto_duplicate_check
from .extraction import extract_document
from .todoist import create_review_task


async def process_documents(document_ids: Iterable[str]) -> None:
    """Read each new document, check QuickBooks for an existing bill, then open a review task
    when one is needed (Todoist optional)."""
    for document_id in document_ids:
        async with get_db_context() as db:
            await extract_document(db, document_id)
            await auto_duplicate_check(db, document_id)
            doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
            if doc and doc.status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.EXTRACTION_FAILED,
                                      DocumentStatus.POSSIBLE_COPY, DocumentStatus.DUPLICATE_CANDIDATE):
                if doc.status == DocumentStatus.EXTRACTION_FAILED:
                    reason = "Extraction failed"
                elif doc.status == DocumentStatus.POSSIBLE_COPY:
                    reason = "Possible copy of a stored bill"
                elif doc.status == DocumentStatus.DUPLICATE_CANDIDATE:
                    reason = "Possible match already in QuickBooks"
                elif not doc.property_id:
                    reason = "Property not assigned"
                elif not doc.qb_vendor_id:
                    reason = "Vendor mapping needed"
                else:
                    reason = "Review needed"
                await db.commit()
                await create_review_task(db, document_id, reason)
