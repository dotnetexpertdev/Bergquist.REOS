"""Todoist task management service."""

import uuid
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Document, AuditEvent, Operation, OperationType, OperationStatus, EventType
from ..providers.todoist_provider import (
    is_todoist_enabled, create_task, update_task, close_task,
    build_task_content, build_task_description
)


async def create_review_task(
    db: AsyncSession,
    document_id: str,
    reason: str,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Create a Todoist task for document review.
    Returns (success, task_id, error)
    """
    if not is_todoist_enabled():
        return True, None, None  # Not an error if disabled

    # Get document
    result = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()

    if not doc:
        return False, None, "Document not found"

    # Don't create duplicate tasks
    if doc.todoist_task_id:
        return True, doc.todoist_task_id, None

    # Build task content
    vendor_name = doc.effective_vendor_name or "Unknown vendor"
    content = build_task_content(vendor_name, reason)

    document_url = f"{settings.public_origin}/documents/{document_id}"
    description = build_task_description(
        document_url,
        vendor_name=doc.effective_vendor_name,
        invoice_number=doc.effective_invoice_number,
        total=f"{doc.effective_currency or 'USD'} {doc.effective_total}" if doc.effective_total else None,
        due_date=doc.effective_due_date,
    )

    # Create operation record
    operation_id = str(uuid.uuid4())
    operation = Operation(
        id=operation_id,
        document_id=document_id,
        operation_type=OperationType.CREATE_TASK,
        status=OperationStatus.IN_PROGRESS,
        started_at=datetime.utcnow(),
        request_id=operation_id,
    )
    db.add(operation)
    await db.commit()  # durable before the remote write; releases the DB write lock

    # Determine priority and due date
    priority = 3  # Medium priority
    due_date = doc.effective_due_date

    # Create task
    task, error = await create_task(
        content=content,
        description=description,
        due_date=due_date,
        priority=priority,
        labels=["bill-review"],
        request_id=operation_id,
    )

    if error:
        operation.status = OperationStatus.FAILED
        operation.error_message = error
        operation.completed_at = datetime.utcnow()

        event = AuditEvent(
            document_id=document_id,
            event_type=EventType.TASK_CREATION_FAILED,
            actor="system",
            details={"error": error},
        )
        db.add(event)
        await db.commit()

        return False, None, error

    # Success
    task_id = task["id"]
    doc.todoist_task_id = task_id

    operation.status = OperationStatus.COMPLETED
    operation.result_id = task_id
    operation.completed_at = datetime.utcnow()

    event = AuditEvent(
        document_id=document_id,
        event_type=EventType.TASK_CREATED,
        actor="system",
        details={
            "task_id": task_id,
            "reason": reason,
        },
    )
    db.add(event)
    await db.commit()

    return True, task_id, None


async def update_review_task(
    db: AsyncSession,
    document_id: str,
) -> Tuple[bool, Optional[str]]:
    """
    Update an existing Todoist task with document changes.
    Returns (success, error)
    """
    if not is_todoist_enabled():
        return True, None

    # Get document
    result = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()

    if not doc or not doc.todoist_task_id:
        return True, None  # Nothing to update

    # Build updated description
    vendor_name = doc.effective_vendor_name or "Unknown vendor"
    document_url = f"{settings.public_origin}/documents/{document_id}"
    description = build_task_description(
        document_url,
        vendor_name=doc.effective_vendor_name,
        invoice_number=doc.effective_invoice_number,
        total=f"{doc.effective_currency or 'USD'} {doc.effective_total}" if doc.effective_total else None,
        due_date=doc.effective_due_date,
        qb_bill_id=doc.qb_bill_id,
    )

    # Update task
    operation_id = str(uuid.uuid4())
    task, error = await update_task(
        doc.todoist_task_id,
        description=description,
        request_id=operation_id,
    )

    if error:
        event = AuditEvent(
            document_id=document_id,
            event_type=EventType.TASK_UPDATED,
            actor="system",
            details={"error": error, "task_id": doc.todoist_task_id},
        )
        db.add(event)
        return False, error

    event = AuditEvent(
        document_id=document_id,
        event_type=EventType.TASK_UPDATED,
        actor="system",
        details={"task_id": doc.todoist_task_id},
    )
    db.add(event)

    return True, None


async def complete_review_task(
    db: AsyncSession,
    document_id: str,
) -> Tuple[bool, Optional[str]]:
    """
    Mark a Todoist task as complete.
    Returns (success, error)
    """
    if not is_todoist_enabled():
        return True, None

    # Get document
    result = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()

    if not doc or not doc.todoist_task_id:
        return True, None

    success, error = await close_task(doc.todoist_task_id)

    if not success:
        return False, error

    event = AuditEvent(
        document_id=document_id,
        event_type=EventType.TASK_UPDATED,
        actor="system",
        details={"task_id": doc.todoist_task_id, "closed": True},
    )
    db.add(event)

    return True, None
