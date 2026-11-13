"""SQLAlchemy database models for Morning Assistant."""

import enum
import uuid
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    Column, String, Text, DateTime, Date, Boolean, Integer,
    ForeignKey, Enum, Numeric, Index, UniqueConstraint, JSON
)
from sqlalchemy.orm import relationship, DeclarativeBase, Mapped, mapped_column


def generate_uuid() -> str:
    """Generate a new UUID string."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Get current UTC datetime."""
    return datetime.utcnow()


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class DocumentStatus(str, enum.Enum):
    """Document workflow status."""
    RECEIVED = "received"  # Just arrived, not yet processed
    READING = "reading"  # Extraction in progress
    EXTRACTION_FAILED = "extraction_failed"  # Extraction failed
    NEEDS_REVIEW = "needs_review"  # Needs owner decision
    READY_FOR_APPROVAL = "ready_for_approval"  # All fields complete, ready to create bill
    DUPLICATE_CANDIDATE = "duplicate_candidate"  # Possible duplicate found
    APPROVED = "approved"  # Approved, bill creation pending
    BILL_CREATED = "bill_created"  # Bill created, attachment pending
    ATTACHMENT_PENDING = "attachment_pending"  # Bill exists, attachment failed
    RECORDED = "recorded"  # Complete: bill created and attached
    LINKED_EXISTING = "linked_existing"  # Linked to pre-existing QB bill
    REMOTE_OUTCOME_UNKNOWN = "remote_outcome_unknown"  # QuickBooks may have created the bill; reconcile first


class ArrivalMethod(str, enum.Enum):
    """How a document arrived."""
    EMAIL = "email"
    UPLOAD = "upload"


class EventType(str, enum.Enum):
    """Types of audit events."""
    DOCUMENT_CREATED = "document_created"
    ARRIVAL_RECORDED = "arrival_recorded"
    EXTRACTION_STARTED = "extraction_started"
    EXTRACTION_COMPLETED = "extraction_completed"
    EXTRACTION_FAILED = "extraction_failed"
    FIELD_CORRECTED = "field_corrected"
    STATUS_CHANGED = "status_changed"
    DUPLICATE_CHECK_COMPLETED = "duplicate_check_completed"
    DUPLICATE_DISMISSED = "duplicate_dismissed"
    LINKED_TO_EXISTING = "linked_to_existing"
    APPROVAL_GRANTED = "approval_granted"
    BILL_CREATION_STARTED = "bill_creation_started"
    BILL_CREATED = "bill_created"
    BILL_CREATION_FAILED = "bill_creation_failed"
    ATTACHMENT_STARTED = "attachment_started"
    ATTACHMENT_COMPLETED = "attachment_completed"
    ATTACHMENT_FAILED = "attachment_failed"
    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    TASK_CREATION_FAILED = "task_creation_failed"
    ACCOUNTING_CHECK_FAILED = "accounting_check_failed"
    BILL_OUTCOME_UNKNOWN = "bill_outcome_unknown"
    BILL_RECONCILED = "bill_reconciled"
    ATTACHMENT_VERIFIED = "attachment_verified"
    ATTACHMENT_OUTCOME_UNKNOWN = "attachment_outcome_unknown"
    OPERATION_INTERRUPTED = "operation_interrupted"
    QB_DISCREPANCY_FLAGGED = "qb_discrepancy_flagged"
    APPROVAL_RELEASED = "approval_released"


class OperationStatus(str, enum.Enum):
    """Status of a pending operation."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"  # Uncertain outcome, needs reconciliation


class OperationType(str, enum.Enum):
    """Types of external operations."""
    CREATE_BILL = "create_bill"
    ATTACH_FILE = "attach_file"
    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"


class Document(Base):
    """
    A document stored in the system.
    Identified by SHA-256 hash - identical bytes = same document.
    """
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    sha256_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)  # Relative to documents_path
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    # Current status and revision
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus), default=DocumentStatus.RECEIVED, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Extracted fields (AI suggestions)
    extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    extraction_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extraction_prompt_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    extracted_document_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extracted_vendor_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    extracted_invoice_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extracted_invoice_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    extracted_due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    extracted_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    extracted_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    extracted_service_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_raw: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)  # Full AI response

    # Reviewed/confirmed fields (owner decisions)
    reviewed_vendor_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    reviewed_invoice_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    reviewed_invoice_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reviewed_due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reviewed_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    reviewed_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)

    # Property and account mappings
    property_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("properties.id"), nullable=True)
    qb_vendor_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    qb_account_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    qb_class_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # QuickBooks bill reference
    qb_bill_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    qb_bill_doc_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    qb_realm_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    qb_attachment_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Todoist task reference
    todoist_task_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Approval tracking
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    approved_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approved_values: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)  # Snapshot of approved values

    # Local correction made after the QuickBooks record existed (US-08 follow-up)
    qb_discrepancy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Result of the most recent QuickBooks duplicate lookup
    last_accounting_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_accounting_check_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_accounting_check_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    arrivals: Mapped[List["Arrival"]] = relationship("Arrival", back_populates="document")
    events: Mapped[List["AuditEvent"]] = relationship("AuditEvent", back_populates="document")
    operations: Mapped[List["Operation"]] = relationship("Operation", back_populates="document")
    duplicate_candidates: Mapped[List["DuplicateCandidate"]] = relationship("DuplicateCandidate", back_populates="document")
    # The Python attribute is `assigned_property` (not `property`) so it cannot
    # shadow the builtin @property decorator. The database column remains
    # `property_id` and the API keeps exposing `property_id` / `property_name`.
    assigned_property: Mapped[Optional["Property"]] = relationship("Property", back_populates="documents")

    @property
    def effective_vendor_name(self) -> Optional[str]:
        """Get the reviewed vendor name or fall back to extracted."""
        return self.reviewed_vendor_name or self.extracted_vendor_name

    @property
    def effective_invoice_number(self) -> Optional[str]:
        return self.reviewed_invoice_number or self.extracted_invoice_number

    @property
    def effective_invoice_date(self) -> Optional[date]:
        return self.reviewed_invoice_date or self.extracted_invoice_date

    @property
    def effective_due_date(self) -> Optional[date]:
        return self.reviewed_due_date or self.extracted_due_date

    @property
    def effective_total(self) -> Optional[Decimal]:
        return self.reviewed_total if self.reviewed_total is not None else self.extracted_total

    @property
    def effective_currency(self) -> Optional[str]:
        return self.reviewed_currency or self.extracted_currency

    @property
    def property_name(self) -> Optional[str]:
        return self.assigned_property.name if self.assigned_property else None


class Arrival(Base):
    """
    Records how and when a document arrived.
    One document can have multiple arrivals (email + upload of same file).
    """
    __tablename__ = "arrivals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    arrived_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    method: Mapped[ArrivalMethod] = mapped_column(Enum(ArrivalMethod), nullable=False)
    original_filename: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # For email arrivals
    gmail_message_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    gmail_attachment_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    email_from: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    email_subject: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    email_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    email_source_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # Path to raw email

    # For upload arrivals
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    upload_attempt_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Relationship
    document: Mapped["Document"] = relationship("Document", back_populates="arrivals")

    __table_args__ = (
        # Prevent duplicate arrivals for same email attachment
        UniqueConstraint("gmail_message_id", "gmail_attachment_id", name="uq_gmail_arrival"),
        # Prevent duplicate upload attempts
        UniqueConstraint("upload_attempt_id", name="uq_upload_attempt"),
    )


class Property(Base):
    """A property that can receive bills."""
    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    # Relationships
    documents: Mapped[List["Document"]] = relationship("Document", back_populates="assigned_property")


class AuditEvent(Base):
    """Audit trail for all document events."""
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)  # "system" or owner username
    details: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)

    # Relationship
    document: Mapped["Document"] = relationship("Document", back_populates="events")


class DuplicateCandidate(Base):
    """Tracks potential duplicate bills found in QuickBooks."""
    __tablename__ = "duplicate_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    # What matched
    realm_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    qb_bill_id: Mapped[str] = mapped_column(String(50), nullable=False)
    qb_vendor_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    qb_currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    seen_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    match_details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    qb_doc_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    qb_vendor_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    qb_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    qb_txn_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Why it matched
    match_reason: Mapped[str] = mapped_column(Text, nullable=False)
    match_fields: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)

    # Resolution
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # "linked", "dismissed"
    resolution_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationship
    document: Mapped["Document"] = relationship("Document", back_populates="duplicate_candidates")

    __table_args__ = (
        # Re-running a check updates the existing row instead of adding another.
        UniqueConstraint("document_id", "qb_bill_id", name="uq_candidate_document_bill"),
    )


class Operation(Base):
    """
    Tracks pending and completed external operations.
    Used for recovery after failures.
    """
    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    operation_type: Mapped[OperationType] = mapped_column(Enum(OperationType), nullable=False)
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus), default=OperationStatus.PENDING, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Request details
    request_payload_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Idempotency key sent to the provider (QuickBooks `requestid`); reused on retries.
    request_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True)
    realm_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # e.g. bill ID for attachments
    accounting_identity: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    approved_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Result
    result_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # e.g., QB bill ID
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationship
    document: Mapped["Document"] = relationship("Document", back_populates="operations")


class ProviderConnection(Base):
    """OAuth tokens and connection status for external providers."""
    __tablename__ = "provider_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)  # "gmail", "quickbooks"

    # Encrypted tokens
    access_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Connection info
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    connected_account: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # QuickBooks specific
    realm_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Status
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class VendorMapping(Base):
    """Maps extracted vendor names to QuickBooks vendor IDs."""
    __tablename__ = "vendor_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    extracted_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    qb_vendor_id: Mapped[str] = mapped_column(String(50), nullable=False)
    qb_vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    realm_id: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("extracted_name", "realm_id", name="uq_vendor_mapping"),
    )


class AccountMapping(Base):
    """Maps expense categories to QuickBooks account IDs."""
    __tablename__ = "account_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # e.g., "utilities", "electricity"
    qb_account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    qb_account_name: Mapped[str] = mapped_column(String(200), nullable=False)
    realm_id: Mapped[str] = mapped_column(String(50), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("category", "realm_id", name="uq_account_mapping"),
    )


class AccountingClaim(Base):
    """
    Database-enforced claim on one vendor invoice in one QuickBooks company.

    Inserted (and committed) at approval time, before any remote write. Two
    different files describing the same vendor invoice cannot both claim it.
    """
    __tablename__ = "accounting_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    realm_id: Mapped[str] = mapped_column(String(50), nullable=False)
    qb_vendor_id: Mapped[str] = mapped_column(String(50), nullable=False)
    invoice_key: Mapped[str] = mapped_column(String(100), nullable=False)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("realm_id", "qb_vendor_id", "invoice_key", name="uq_accounting_claim"),
    )
