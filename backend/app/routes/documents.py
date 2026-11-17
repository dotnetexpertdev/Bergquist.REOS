"""Document routes: list, detail, original download, history, corrections, upload."""

import tempfile
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_auth, require_csrf
from ..config import settings
from ..database import get_session
from ..models import Arrival, ArrivalMethod, AuditEvent, Document, DocumentStatus as DBDocumentStatus, EventType
from ..schemas import (
    DocumentDetail, DocumentListItem, DocumentUpdateRequest, DocumentUpdateResponse, HistoryEntry, ResolveCopyRequest,
    UploadResponse,
)
from ..services.accounting import auto_duplicate_check
from ..services.documents import (
    UpdateError, get_available_actions, get_document_detail, get_document_history, get_document_list,
    get_documents_needing_review, get_documents_ready_for_approval, get_pending_question, missing_fields,
    resolve_copy, update_document,
)
from ..services.history import describe
from ..services.intake import ALLOWED_EXTENSIONS, cleanup_temp_file, ingest_document, validate_upload
from ..services.processing import process_documents

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("", response_model=List[DocumentListItem])
async def list_documents(
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    status_filter: Optional[str] = Query(None, description="Comma-separated status values"),
    property_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    statuses = None
    if status_filter:
        try:
            statuses = [DBDocumentStatus(s.strip()) for s in status_filter.split(",")]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid status: {e}")
    return await get_document_list(db, status_filter=statuses, property_id=property_id, limit=limit, offset=offset)


@router.get("/needs-review", response_model=List[DocumentListItem])
async def list_needs_review(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                            limit: int = Query(50, ge=1, le=200)):
    return await get_documents_needing_review(db, limit=limit)


@router.get("/ready-for-approval", response_model=List[DocumentListItem])
async def list_ready_for_approval(username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                                  limit: int = Query(50, ge=1, le=200)):
    return await get_documents_ready_for_approval(db, limit=limit)


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(document_id: str, username: str = Depends(require_auth),
                       db: AsyncSession = Depends(get_session)):
    doc = await get_document_detail(db, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/{document_id}/file")
async def download_document_file(document_id: str, username: str = Depends(require_auth),
                                 db: AsyncSession = Depends(get_session)):
    """The untouched original bytes (authenticated owners only)."""
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    file_path = settings.documents_path / doc.storage_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Original file missing from storage")
    first = (await db.execute(select(Arrival).where(Arrival.document_id == document_id)
                              .order_by(Arrival.arrived_at))).scalars().first()
    filename = (first.original_filename if first and first.original_filename else f"document_{document_id}")
    return FileResponse(
        path=file_path, media_type=doc.content_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
            "X-Content-SHA256": doc.sha256_hash,
        },
    )


@router.get("/{document_id}/history", response_model=List[HistoryEntry])
async def get_document_history_route(document_id: str, username: str = Depends(require_auth),
                                     db: AsyncSession = Depends(get_session)):
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return [describe(e, doc) for e in await get_document_history(db, document_id)]


@router.post("/{document_id}/update", response_model=DocumentUpdateResponse)
async def update_document_route(document_id: str, update: DocumentUpdateRequest,
                                username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                                _csrf: None = Depends(require_csrf)):
    """Save a correction. This never creates or changes anything in QuickBooks."""
    try:
        doc = await update_document(db, document_id, update, username, source=update.source or "typed")
    except UpdateError as e:
        await db.rollback()
        raise HTTPException(status_code=e.status_code,
                            detail={"message": e.message, "field_errors": e.field_errors} if e.field_errors else e.message)
    await db.commit()
    if doc.last_accounting_check_ok is None:
        # The facts compared with QuickBooks are new or have changed: look for an existing bill now
        # (read-only) so the status returned below is already the checked one.
        await auto_duplicate_check(db, document_id, username)
        await db.refresh(doc)
    return DocumentUpdateResponse(
        success=True, revision=doc.revision, status=doc.status.value,
        available_actions=get_available_actions(doc), pending_question=get_pending_question(doc),
        missing_fields=missing_fields(doc),
    )


@router.post("/{document_id}/resolve-copy", response_model=DocumentUpdateResponse)
async def resolve_copy_route(document_id: str, request: ResolveCopyRequest,
                             username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                             _csrf: None = Depends(require_csrf)):
    """Answer "is this the same invoice as the stored one?". Never posts to QuickBooks."""
    try:
        doc = await resolve_copy(db, document_id, request.revision, request.resolution, username)
    except UpdateError as e:
        await db.rollback()
        raise HTTPException(status_code=e.status_code, detail=e.message)
    await db.commit()
    return DocumentUpdateResponse(
        success=True, revision=doc.revision, status=doc.status.value,
        available_actions=get_available_actions(doc), pending_question=get_pending_question(doc),
        missing_fields=missing_fields(doc),
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    attempt_id: Optional[str] = Form(None),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _csrf: None = Depends(require_csrf),
):
    """Upload a PDF/JPEG/PNG. A retried request with the same attempt_id counts once."""
    try:
        attempt_id = str(uuid.UUID(attempt_id)) if attempt_id else str(uuid.uuid4())
    except ValueError:
        raise HTTPException(status_code=400, detail="attempt_id must be a UUID")

    previous = (await db.execute(select(Arrival).where(Arrival.upload_attempt_id == attempt_id))).scalar_one_or_none()
    if previous:
        return UploadResponse(success=True, document_id=previous.document_id, arrival_id=previous.id,
                              is_duplicate=False, retried_attempt=True,
                              message="This upload was already received.")
    await db.commit()

    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type. Upload a PDF, JPEG or PNG file.")

    settings.temp_path.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(suffix=ext, dir=str(settings.temp_path))
    temp_path = Path(temp_name)
    try:
        total = 0
        with open(fd, "wb") as f:
            while chunk := await file.read(1024 * 256):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise HTTPException(status_code=413,
                                        detail=f"File is larger than the {settings.max_upload_size_mb} MB limit.")
                f.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="The file is empty. Choose the bill again.")
        is_valid, content_type, error = await validate_upload(temp_path, filename, total)
        if not is_valid:
            raise HTTPException(status_code=400, detail=error)

        document, arrival, is_new = await ingest_document(
            db, temp_path, content_type, ArrivalMethod.UPLOAD, original_filename=filename,
            uploaded_by=username, upload_attempt_id=attempt_id,
        )
        await db.commit()  # success is reported only after the original and arrival are durable
        if is_new:
            background_tasks.add_task(process_documents, [document.id])
        return UploadResponse(
            success=True, document_id=document.id, arrival_id=arrival.id, is_duplicate=not is_new,
            message=("Document uploaded." if is_new else
                     "This exact file is already stored; a new arrival was added to the same document."),
        )
    finally:
        cleanup_temp_file(temp_path)


@router.post("/{document_id}/retry-extraction")
async def retry_extraction(document_id: str, background_tasks: BackgroundTasks,
                           username: str = Depends(require_auth), db: AsyncSession = Depends(get_session),
                           _csrf: None = Depends(require_csrf)):
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != DBDocumentStatus.EXTRACTION_FAILED:
        raise HTTPException(status_code=409, detail="Only a bill that failed to read can be retried")
    doc.status = DBDocumentStatus.RECEIVED
    doc.status_reason = None
    doc.revision += 1
    db.add(AuditEvent(document_id=doc.id, event_type=EventType.STATUS_CHANGED, actor=username,
                      details={"action": "retry_extraction"}))
    await db.commit()
    background_tasks.add_task(process_documents, [document_id])
    return {"success": True, "message": "Reading the bill again"}
