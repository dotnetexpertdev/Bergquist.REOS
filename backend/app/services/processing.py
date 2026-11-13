"""Background processing shared by upload and Gmail intake."""

from typing import Iterable

from sqlalchemy import select

from ..database import get_db_context
from ..models import Document, DocumentStatus
from .extraction import extract_document
from .todoist import create_review_task


async def process_documents(document_ids: Iterable[str]) -> None:
    """Read each new document, then open a review task when one is needed (Todoist optional)."""
    for document_id in document_ids:
        async with get_db_context() as db:
            await extract_document(db, document_id)
            doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
            if doc and doc.status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.EXTRACTION_FAILED):
                if doc.status == DocumentStatus.EXTRACTION_FAILED:
                    reason = "Extraction failed"
                elif not doc.property_id:
                    reason = "Property not assigned"
                elif not doc.qb_vendor_id:
                    reason = "Vendor mapping needed"
                else:
                    reason = "Review needed"
                await db.commit()
                await create_review_task(db, document_id, reason)
