"""Document intake service - one ingestion path for Gmail attachments and uploads."""

import hashlib
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..models import Arrival, ArrivalMethod, AuditEvent, Document, DocumentStatus, EventType

# Supported content types
SUPPORTED_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}

# Allowed extensions (for upload validation)
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
JPEG_SIGNATURE = bytes([0xFF, 0xD8, 0xFF])


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def sniff_content_type(file_path: Path) -> Optional[str]:
    """Detect PDF/JPEG/PNG from the file signature (bytes, not the name)."""
    with open(file_path, "rb") as f:
        head = f.read(1024)
    if head.startswith(PNG_SIGNATURE):
        return "image/png"
    if head.startswith(JPEG_SIGNATURE):
        return "image/jpeg"
    if b"%PDF-" in head:
        return "application/pdf"
    return None


def get_content_type(file_path: Path, original_filename: Optional[str] = None) -> Optional[str]:
    """Content type detected from the bytes; the filename is never trusted on its own."""
    return sniff_content_type(file_path)


def generate_storage_path(sha256_hash: str, content_type: str) -> str:
    """Server-controlled storage path derived from the content hash."""
    ext = SUPPORTED_CONTENT_TYPES.get(content_type, "")
    return f"{sha256_hash[:4]}/{sha256_hash}{ext}"


async def validate_upload(
    file_path: Path,
    original_filename: str,
    file_size: int,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Validate an uploaded file. Returns (is_valid, content_type, error_message)."""
    if file_size == 0:
        return False, None, "File is empty"
    if file_size > settings.max_upload_bytes:
        return False, None, f"File exceeds maximum size of {settings.max_upload_size_mb}MB"
    ext = Path(original_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, None, "Unsupported file type. Allowed: PDF, JPEG, PNG"
    content_type = get_content_type(file_path, original_filename)
    if not content_type:
        return False, None, "The file's contents are not a PDF, JPEG or PNG image."
    return True, content_type, None


async def store_document(temp_file_path: Path, content_type: str) -> Tuple[str, str, int]:
    """Store a document file permanently. Returns (sha256_hash, storage_path, file_size)."""
    sha256_hash = compute_sha256(temp_file_path)
    file_size = temp_file_path.stat().st_size
    storage_path = generate_storage_path(sha256_hash, content_type)
    full_path = settings.documents_path / storage_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    # Write once, atomically: copy to a sibling temp name, then rename into place.
    # An existing blob with this hash already holds identical bytes and is never overwritten.
    if not full_path.exists():
        partial = full_path.with_name(f".{full_path.name}.{uuid.uuid4().hex}.partial")
        shutil.copyfile(temp_file_path, partial)
        try:
            os.replace(partial, full_path)
        finally:
            if partial.exists():
                partial.unlink()
    return sha256_hash, storage_path, file_size


async def ingest_document(
    db: AsyncSession,
    temp_file_path: Path,
    content_type: str,
    arrival_method: ArrivalMethod,
    original_filename: Optional[str] = None,
    # Email fields
    gmail_message_id: Optional[str] = None,
    gmail_attachment_id: Optional[str] = None,
    email_from: Optional[str] = None,
    email_subject: Optional[str] = None,
    email_received_at: Optional[datetime] = None,
    email_source_path: Optional[str] = None,
    # Upload fields
    uploaded_by: Optional[str] = None,
    upload_attempt_id: Optional[str] = None,
) -> Tuple[Document, Arrival, bool]:
    """
    Store the original, then record the document (found by SHA-256) and the arrival.

    The caller commits. Identical bytes always resolve to one document; each
    genuine submission adds an arrival. Returns (document, arrival, is_new_document).
    """
    sha256_hash, storage_path, file_size = await store_document(temp_file_path, content_type)

    document = (await db.execute(select(Document).where(Document.sha256_hash == sha256_hash))).scalar_one_or_none()
    is_new_document = document is None
    if is_new_document:
        document = Document(
            sha256_hash=sha256_hash, content_type=content_type, file_size=file_size,
            storage_path=storage_path, status=DocumentStatus.RECEIVED,
        )
        db.add(document)
        try:
            await db.flush()
        except IntegrityError:
            # A concurrent submission of the same bytes won; use its document.
            await db.rollback()
            document = (await db.execute(select(Document).where(Document.sha256_hash == sha256_hash))).scalar_one()
            is_new_document = False
        else:
            db.add(AuditEvent(document_id=document.id, event_type=EventType.DOCUMENT_CREATED, actor="system",
                              details={"sha256_hash": sha256_hash, "content_type": content_type,
                                       "file_size": file_size}))

    arrival = Arrival(
        document_id=document.id, method=arrival_method, original_filename=original_filename,
        gmail_message_id=gmail_message_id, gmail_attachment_id=gmail_attachment_id, email_from=email_from,
        email_subject=email_subject, email_received_at=email_received_at, email_source_path=email_source_path,
        uploaded_by=uploaded_by, upload_attempt_id=upload_attempt_id,
    )
    db.add(arrival)
    await db.flush()
    db.add(AuditEvent(
        document_id=document.id, event_type=EventType.ARRIVAL_RECORDED, actor=uploaded_by or "system",
        details={
            "arrival_id": arrival.id, "method": arrival_method.value, "original_filename": original_filename,
            "email_from": email_from, "email_subject": email_subject, "gmail_message_id": gmail_message_id,
            "repeat_of_existing_document": not is_new_document,
        },
    ))
    return document, arrival, is_new_document


async def get_document_file_path(document: Document) -> Path:
    """Get the full file path for a document."""
    return settings.documents_path / document.storage_path


def cleanup_temp_file(file_path: Path) -> None:
    """Clean up a temporary file."""
    try:
        if file_path.exists():
            file_path.unlink()
    except Exception:
        pass
