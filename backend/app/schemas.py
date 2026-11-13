"""Pydantic schemas for API request/response validation."""

from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, Dict, Any
from enum import Enum

from pydantic import BaseModel, Field, ConfigDict


# ============================================================================
# Auth Schemas
# ============================================================================

class LoginRequest(BaseModel):
    """Login request."""
    username: str
    password: str


class LoginResponse(BaseModel):
    """Login response."""
    success: bool
    username: Optional[str] = None
    message: Optional[str] = None
    csrf_token: Optional[str] = None


class SessionResponse(BaseModel):
    """Current session info."""
    authenticated: bool
    username: Optional[str] = None
    csrf_token: Optional[str] = None


# ============================================================================
# Document Schemas
# ============================================================================

class DocumentStatus(str, Enum):
    """Document workflow status."""
    RECEIVED = "received"
    READING = "reading"
    EXTRACTION_FAILED = "extraction_failed"
    NEEDS_REVIEW = "needs_review"
    READY_FOR_APPROVAL = "ready_for_approval"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    APPROVED = "approved"
    BILL_CREATED = "bill_created"
    ATTACHMENT_PENDING = "attachment_pending"
    RECORDED = "recorded"
    LINKED_EXISTING = "linked_existing"
    REMOTE_OUTCOME_UNKNOWN = "remote_outcome_unknown"
    FILED = "filed"
    POSSIBLE_COPY = "possible_copy"
    LINKED_COPY = "linked_copy"


class ArrivalMethod(str, Enum):
    """How a document arrived."""
    EMAIL = "email"
    UPLOAD = "upload"


class ArrivalResponse(BaseModel):
    """Information about how a document arrived."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    arrived_at: datetime
    method: ArrivalMethod
    original_filename: Optional[str] = None

    # Email specific
    email_from: Optional[str] = None
    email_subject: Optional[str] = None
    email_received_at: Optional[datetime] = None
    gmail_message_id: Optional[str] = None

    # Upload specific
    uploaded_by: Optional[str] = None


class DocumentListItem(BaseModel):
    """Document summary for list view."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: DocumentStatus
    status_reason: Optional[str] = None
    created_at: datetime
    revision: int

    # Display fields (effective values)
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    total: Optional[Decimal] = None
    currency: Optional[str] = None
    document_type: Optional[str] = None
    document_category: str = "bill"
    copy_of_document_id: Optional[str] = None

    # Property
    property_id: Optional[str] = None
    property_name: Optional[str] = None

    # QuickBooks
    qb_bill_id: Optional[str] = None

    # Arrival info
    arrival_count: int = 0
    first_arrival_method: Optional[ArrivalMethod] = None
    first_arrival_filename: Optional[str] = None

    # Outstanding question for review
    pending_question: Optional[str] = None
    missing_fields: List[str] = []


class DocumentDetail(BaseModel):
    """Full document detail for review."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    sha256_hash: str
    content_type: str
    file_size: int
    status: DocumentStatus
    status_reason: Optional[str] = None
    revision: int
    created_at: datetime

    # Extracted values
    extracted_at: Optional[datetime] = None
    extraction_model: Optional[str] = None
    extracted_document_type: Optional[str] = None
    extracted_vendor_name: Optional[str] = None
    extracted_invoice_number: Optional[str] = None
    extracted_invoice_date: Optional[date] = None
    extracted_due_date: Optional[date] = None
    extracted_total: Optional[Decimal] = None
    extracted_currency: Optional[str] = None
    extracted_service_address: Optional[str] = None
    extraction_uncertain_fields: List[str] = []
    extraction_source_text: Optional[str] = None

    # Classification
    reviewed_document_type: Optional[str] = None
    document_category: str = "bill"
    document_categories: Dict[str, str] = {}

    # Same invoice in another stored file
    copy_of: Optional[Dict[str, Any]] = None

    # Reviewed values
    reviewed_vendor_name: Optional[str] = None
    reviewed_invoice_number: Optional[str] = None
    reviewed_invoice_date: Optional[date] = None
    reviewed_due_date: Optional[date] = None
    reviewed_total: Optional[Decimal] = None
    reviewed_currency: Optional[str] = None

    # Effective values (reviewed or extracted)
    effective_vendor_name: Optional[str] = None
    effective_invoice_number: Optional[str] = None
    effective_invoice_date: Optional[date] = None
    effective_due_date: Optional[date] = None
    effective_total: Optional[Decimal] = None
    effective_currency: Optional[str] = None

    # Property and mappings
    property_id: Optional[str] = None
    property_name: Optional[str] = None
    qb_vendor_id: Optional[str] = None
    qb_vendor_name: Optional[str] = None
    qb_account_id: Optional[str] = None
    qb_account_name: Optional[str] = None
    qb_class_id: Optional[str] = None

    # QuickBooks references
    qb_bill_id: Optional[str] = None
    qb_bill_doc_number: Optional[str] = None
    qb_realm_id: Optional[str] = None
    qb_attachment_id: Optional[str] = None
    qb_bill_url: Optional[str] = None
    qb_discrepancy: Optional[str] = None
    last_accounting_check_at: Optional[datetime] = None
    last_accounting_check_ok: Optional[bool] = None
    last_accounting_check_error: Optional[str] = None

    # Todoist
    todoist_task_id: Optional[str] = None

    # Approval info
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_revision: Optional[int] = None

    # Arrivals
    arrivals: List[ArrivalResponse] = []

    # Available actions
    available_actions: List[str] = []

    # Outstanding question and visibly unknown required values
    pending_question: Optional[str] = None
    missing_fields: List[str] = []

    # QuickBooks company the approval would target (verified or with the problem)
    company: Optional[Dict[str, Any]] = None


class DocumentUpdateRequest(BaseModel):
    """Request to update document fields."""
    revision: int  # Must match current revision

    # Reviewed values (any can be updated)
    reviewed_document_type: Optional[str] = None
    reviewed_vendor_name: Optional[str] = None
    reviewed_invoice_number: Optional[str] = None
    reviewed_invoice_date: Optional[date] = None
    reviewed_due_date: Optional[date] = None
    reviewed_total: Optional[Decimal] = None
    reviewed_currency: Optional[str] = None

    # Mappings
    property_id: Optional[str] = None
    qb_vendor_id: Optional[str] = None
    qb_account_id: Optional[str] = None
    qb_class_id: Optional[str] = None

    # "typed" or "voice" (recorded in the audit history)
    source: Optional[str] = Field(default="typed", pattern="^(typed|voice)$")


class ResolveCopyRequest(BaseModel):
    """Owner decision on a suspected copy of another stored document."""
    revision: int
    resolution: str = Field(pattern="^(same|different)$")


class DocumentUpdateResponse(BaseModel):
    """Response after updating document."""
    success: bool
    revision: int
    status: DocumentStatus
    available_actions: List[str] = []
    pending_question: Optional[str] = None
    missing_fields: List[str] = []
    errors: Optional[Dict[str, str]] = None


# ============================================================================
# Upload Schemas
# ============================================================================

class UploadResponse(BaseModel):
    """Response after file upload."""
    success: bool
    document_id: Optional[str] = None
    arrival_id: Optional[str] = None
    is_duplicate: bool = False
    retried_attempt: bool = False
    message: Optional[str] = None


# ============================================================================
# Gmail Import Schemas
# ============================================================================

class GmailImportRequest(BaseModel):
    """Request to import Gmail messages."""
    pass  # No parameters needed, uses configured label


class GmailImportResult(BaseModel):
    """Result of importing a single attachment."""
    message_id: str
    attachment_filename: str
    success: bool
    document_id: Optional[str] = None
    is_duplicate: bool = False
    error: Optional[str] = None
    skipped_reason: Optional[str] = None


class GmailImportResponse(BaseModel):
    """Response after importing Gmail messages."""
    success: bool
    messages_found: int
    attachments_processed: int
    imported: int
    already_processed: int
    skipped: int
    failed: int
    results: List[GmailImportResult] = []
    error: Optional[str] = None


# ============================================================================
# QuickBooks Schemas
# ============================================================================

class QBConnectionStatus(BaseModel):
    """QuickBooks connection status."""
    configured: bool = False
    verified: bool = False
    redirect_uri: Optional[str] = None
    connected: bool
    company_name: Optional[str] = None
    realm_id: Optional[str] = None
    environment: str
    expected_realm_id: Optional[str] = None
    realm_mismatch: bool = False
    last_error: Optional[str] = None
    token_expires_at: Optional[datetime] = None


class QBVendor(BaseModel):
    """QuickBooks vendor."""
    id: str
    name: str
    display_name: str


class QBAccount(BaseModel):
    """QuickBooks expense account."""
    id: str
    name: str
    account_type: str
    fully_qualified_name: str


class QBClass(BaseModel):
    """QuickBooks class."""
    id: str
    name: str


class QBReferenceData(BaseModel):
    """Reference data from QuickBooks."""
    realm_id: Optional[str] = None
    vendors: List[QBVendor] = []
    accounts: List[QBAccount] = []
    classes: List[QBClass] = []


# ============================================================================
# Duplicate Detection Schemas
# ============================================================================

class DuplicateCandidateResponse(BaseModel):
    """A potential duplicate bill."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    realm_id: Optional[str] = None
    qb_bill_id: str
    qb_doc_number: Optional[str] = None
    qb_vendor_name: Optional[str] = None
    qb_total: Optional[Decimal] = None
    qb_currency: Optional[str] = None
    qb_txn_date: Optional[date] = None
    match_reason: str
    match_fields: Optional[List[str]] = None
    match_details: Optional[Dict[str, Any]] = None
    found_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    seen_count: int = 1
    resolved: bool
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolution_reason: Optional[str] = None


class ResolveDuplicateRequest(BaseModel):
    """Request to resolve a duplicate candidate."""
    candidate_id: str
    resolution: str  # "linked" or "dismissed"
    reason: Optional[str] = None


# ============================================================================
# Approval Schemas
# ============================================================================

class ApprovalRequest(BaseModel):
    """Request to approve a document for bill creation."""
    revision: int  # Must match current revision


class ApprovalResponse(BaseModel):
    """Response after approval/bill creation."""
    success: bool
    document_id: str
    status: DocumentStatus
    revision: Optional[int] = None
    qb_bill_url: Optional[str] = None
    qb_bill_id: Optional[str] = None
    qb_bill_doc_number: Optional[str] = None
    qb_attachment_id: Optional[str] = None
    error: Optional[str] = None
    requires_retry: bool = False
    retry_action: Optional[str] = None  # "retry_attachment"


# ============================================================================
# Search Schemas
# ============================================================================

class SearchRequest(BaseModel):
    """Typed or reviewed-voice question."""
    query: str = ""
    context_document_id: Optional[str] = None  # bill selected in the previous turn
    select_document_id: Optional[str] = None  # the owner picked this bill after a clarification


class SearchResultDocument(BaseModel):
    """A document in search results, with stable links."""
    id: str
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    total: Optional[Decimal] = None
    currency: Optional[str] = None
    status: DocumentStatus
    property_name: Optional[str] = None
    qb_bill_id: Optional[str] = None
    qb_bill_doc_number: Optional[str] = None
    invoice_date: Optional[date] = None
    arrival_count: int = 0
    document_category: Optional[str] = None
    links: Dict[str, Optional[str]] = {}


class HistoryEntry(BaseModel):
    """One history event, described for people, with source links."""
    id: str
    event_type: str
    occurred_at: datetime
    actor: str
    actor_kind: str
    summary: str
    links: List[Dict[str, str]] = []
    details: Optional[Dict[str, Any]] = None


class ProposedEdit(BaseModel):
    """A correction proposed from a voice/typed instruction; applied only after confirmation."""
    document_id: str
    revision: int
    field: str
    field_label: str
    update_key: str
    current_value: Optional[str] = None
    proposed_value: str
    proposed_raw: Any = None
    # Every field the confirmation will send (e.g. vendor name plus its QuickBooks vendor)
    updates: Dict[str, Any] = {}


class SearchResponse(BaseModel):
    """Grounded answer for a question."""
    success: bool
    answer: str
    intent: Optional[str] = None
    interpreted_by: Optional[str] = None
    documents: List[SearchResultDocument] = []
    selected_document_id: Optional[str] = None
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    pending_query: Optional[str] = None
    history: List[HistoryEntry] = []
    proposed_edit: Optional[ProposedEdit] = None
    error: Optional[str] = None


# ============================================================================
# Transcription Schemas
# ============================================================================

class TranscriptionResponse(BaseModel):
    """Response from audio transcription."""
    success: bool
    transcript: Optional[str] = None
    error: Optional[str] = None


# ============================================================================
# Audit Event Schemas
# ============================================================================

class AuditEventResponse(BaseModel):
    """An audit event."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: str
    occurred_at: datetime
    actor: str
    details: Optional[Dict[str, Any]] = None


# ============================================================================
# Property Schemas
# ============================================================================

class PropertyResponse(BaseModel):
    """A property."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    address: Optional[str] = None


class PropertyCreateRequest(BaseModel):
    """Request to create a property."""
    name: str
    address: Optional[str] = None


# ============================================================================
# Connection Status Schemas
# ============================================================================

class IntegrationStatus(BaseModel):
    """Status of an integration."""
    configured: bool
    connected: bool
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class AllIntegrationsStatus(BaseModel):
    """Status of all integrations."""
    gmail: IntegrationStatus
    quickbooks: IntegrationStatus
    openai: IntegrationStatus
    todoist: IntegrationStatus


# ============================================================================
# Mapping Schemas
# ============================================================================

class VendorMappingResponse(BaseModel):
    """A vendor mapping between extracted name and QB vendor."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    extracted_name: str
    qb_vendor_id: str
    qb_vendor_name: str
    created_at: datetime


class VendorMappingCreateRequest(BaseModel):
    """Request to create a vendor mapping."""
    extracted_name: str
    qb_vendor_id: str
    qb_vendor_name: str


class AccountMappingResponse(BaseModel):
    """Maps a vendor-name or bill-type keyword (e.g. "electric") to a QB expense account."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    category: str
    qb_account_id: str
    qb_account_name: str
    created_at: datetime


class AccountMappingCreateRequest(BaseModel):
    """Request to create an account mapping."""
    category: str
    qb_account_id: str
    qb_account_name: str
