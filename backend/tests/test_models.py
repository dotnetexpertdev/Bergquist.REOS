"""Tests for database models."""

import pytest
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Document, Arrival, Property, AuditEvent, DuplicateCandidate, Operation,
    DocumentStatus, ArrivalMethod, EventType, OperationType, OperationStatus
)


@pytest.mark.asyncio
async def test_create_document(db_session):
    """Test creating a document."""
    doc = Document(
        sha256_hash="a" * 64,
        content_type="application/pdf",
        file_size=1024,
        storage_path="test/path.pdf",
    )
    db_session.add(doc)
    await db_session.commit()

    result = await db_session.execute(select(Document))
    saved_doc = result.scalar_one()

    assert saved_doc.id is not None
    assert saved_doc.sha256_hash == "a" * 64
    assert saved_doc.status == DocumentStatus.RECEIVED
    assert saved_doc.revision == 1


@pytest.mark.asyncio
async def test_document_effective_values(db_session):
    """Test document effective value properties."""
    doc = Document(
        sha256_hash="b" * 64,
        content_type="application/pdf",
        file_size=1024,
        storage_path="test/path.pdf",
        extracted_vendor_name="Electric Co",
        extracted_total=Decimal("100.00"),
    )
    db_session.add(doc)
    await db_session.commit()

    # Effective values should use extracted when no reviewed values
    assert doc.effective_vendor_name == "Electric Co"
    assert doc.effective_total == Decimal("100.00")

    # Update with reviewed values
    doc.reviewed_vendor_name = "Electric Company"
    doc.reviewed_total = Decimal("105.00")
    await db_session.commit()

    # Effective values should now use reviewed
    assert doc.effective_vendor_name == "Electric Company"
    assert doc.effective_total == Decimal("105.00")


@pytest.mark.asyncio
async def test_create_arrival(db_session):
    """Test creating arrivals for a document."""
    doc = Document(
        sha256_hash="c" * 64,
        content_type="application/pdf",
        file_size=1024,
        storage_path="test/path.pdf",
    )
    db_session.add(doc)
    await db_session.flush()

    # Email arrival
    arrival1 = Arrival(
        document_id=doc.id,
        method=ArrivalMethod.EMAIL,
        original_filename="bill.pdf",
        gmail_message_id="msg123",
        gmail_attachment_id="att456",
        email_from="vendor@example.com",
    )
    db_session.add(arrival1)

    # Upload arrival
    arrival2 = Arrival(
        document_id=doc.id,
        method=ArrivalMethod.UPLOAD,
        original_filename="bill_copy.pdf",
        uploaded_by="owner",
        upload_attempt_id="attempt123",
    )
    db_session.add(arrival2)
    await db_session.commit()

    # Refresh to load relationships (async sessions cannot lazy-load)
    await db_session.refresh(doc, attribute_names=["arrivals"])

    assert len(doc.arrivals) == 2
    assert any(a.method == ArrivalMethod.EMAIL for a in doc.arrivals)
    assert any(a.method == ArrivalMethod.UPLOAD for a in doc.arrivals)


@pytest.mark.asyncio
async def test_create_audit_event(db_session):
    """Test creating audit events."""
    doc = Document(
        sha256_hash="d" * 64,
        content_type="application/pdf",
        file_size=1024,
        storage_path="test/path.pdf",
    )
    db_session.add(doc)
    await db_session.flush()

    event = AuditEvent(
        document_id=doc.id,
        event_type=EventType.DOCUMENT_CREATED,
        actor="system",
        details={"sha256_hash": doc.sha256_hash},
    )
    db_session.add(event)
    await db_session.commit()

    result = await db_session.execute(
        select(AuditEvent).where(AuditEvent.document_id == doc.id)
    )
    saved_event = result.scalar_one()

    assert saved_event.event_type == EventType.DOCUMENT_CREATED
    assert saved_event.actor == "system"
    assert saved_event.details["sha256_hash"] == doc.sha256_hash


@pytest.mark.asyncio
async def test_property_relationship(db_session):
    """Test property relationship with documents."""
    prop = Property(
        name="Demo Property",
        address="123 Main St",
    )
    db_session.add(prop)
    await db_session.flush()

    doc = Document(
        sha256_hash="e" * 64,
        content_type="application/pdf",
        file_size=1024,
        storage_path="test/path.pdf",
        property_id=prop.id,
    )
    db_session.add(doc)
    await db_session.commit()

    await db_session.refresh(doc, attribute_names=["assigned_property"])
    assert doc.assigned_property is not None
    assert doc.assigned_property.name == "Demo Property"
    # The API-facing name is unchanged.
    assert doc.property_name == "Demo Property"


def test_property_decorator_not_shadowed():
    """The relationship must not be named 'property' (it would shadow @property)."""
    import builtins
    for name in ("effective_vendor_name", "effective_total", "effective_currency", "property_name"):
        assert isinstance(Document.__dict__[name], builtins.property), name
    assert "property" not in Document.__mapper__.relationships
    assert "assigned_property" in Document.__mapper__.relationships


@pytest.mark.asyncio
async def test_duplicate_candidate(db_session):
    """Test duplicate candidate tracking."""
    doc = Document(
        sha256_hash="f" * 64,
        content_type="application/pdf",
        file_size=1024,
        storage_path="test/path.pdf",
    )
    db_session.add(doc)
    await db_session.flush()

    candidate = DuplicateCandidate(
        document_id=doc.id,
        qb_bill_id="12345",
        qb_doc_number="INV-001",
        qb_vendor_name="Electric Co",
        qb_total=Decimal("100.00"),
        match_reason="Matching invoice_number",
        match_fields=["invoice_number"],
    )
    db_session.add(candidate)
    await db_session.commit()

    assert candidate.resolved == False
    assert candidate.qb_bill_id == "12345"


@pytest.mark.asyncio
async def test_operation_tracking(db_session):
    """Test operation tracking for recovery."""
    doc = Document(
        sha256_hash="g" * 64,
        content_type="application/pdf",
        file_size=1024,
        storage_path="test/path.pdf",
    )
    db_session.add(doc)
    await db_session.flush()

    operation = Operation(
        document_id=doc.id,
        operation_type=OperationType.CREATE_BILL,
        status=OperationStatus.PENDING,
        request_id="req123",
    )
    db_session.add(operation)
    await db_session.commit()

    assert operation.status == OperationStatus.PENDING
    assert operation.retry_count == 0

    # Simulate completion
    operation.status = OperationStatus.COMPLETED
    operation.result_id = "bill456"
    operation.completed_at = datetime.utcnow()
    await db_session.commit()

    assert operation.result_id == "bill456"
