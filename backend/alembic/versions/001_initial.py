"""Initial database schema

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Properties table
    op.create_table('properties',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # Documents table
    op.create_table('documents',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('sha256_hash', sa.String(64), nullable=False),
        sa.Column('content_type', sa.String(100), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.Column('storage_path', sa.String(500), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.Enum('received', 'reading', 'extraction_failed', 'needs_review',
                                    'ready_for_approval', 'duplicate_candidate', 'approved',
                                    'bill_created', 'attachment_pending', 'recorded', 'linked_existing',
                                    name='documentstatus'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('status_reason', sa.Text(), nullable=True),

        # Extraction fields
        sa.Column('extracted_at', sa.DateTime(), nullable=True),
        sa.Column('extraction_model', sa.String(100), nullable=True),
        sa.Column('extraction_prompt_version', sa.String(50), nullable=True),
        sa.Column('extracted_document_type', sa.String(100), nullable=True),
        sa.Column('extracted_vendor_name', sa.String(200), nullable=True),
        sa.Column('extracted_invoice_number', sa.String(100), nullable=True),
        sa.Column('extracted_invoice_date', sa.Date(), nullable=True),
        sa.Column('extracted_due_date', sa.Date(), nullable=True),
        sa.Column('extracted_total', sa.Numeric(12, 2), nullable=True),
        sa.Column('extracted_currency', sa.String(3), nullable=True),
        sa.Column('extracted_service_address', sa.Text(), nullable=True),
        sa.Column('extracted_raw', sa.JSON(), nullable=True),

        # Reviewed fields
        sa.Column('reviewed_vendor_name', sa.String(200), nullable=True),
        sa.Column('reviewed_invoice_number', sa.String(100), nullable=True),
        sa.Column('reviewed_invoice_date', sa.Date(), nullable=True),
        sa.Column('reviewed_due_date', sa.Date(), nullable=True),
        sa.Column('reviewed_total', sa.Numeric(12, 2), nullable=True),
        sa.Column('reviewed_currency', sa.String(3), nullable=True),

        # Mappings
        sa.Column('property_id', sa.String(36), sa.ForeignKey('properties.id'), nullable=True),
        sa.Column('qb_vendor_id', sa.String(50), nullable=True),
        sa.Column('qb_account_id', sa.String(50), nullable=True),
        sa.Column('qb_class_id', sa.String(50), nullable=True),

        # QuickBooks references
        sa.Column('qb_bill_id', sa.String(50), nullable=True),
        sa.Column('qb_bill_doc_number', sa.String(100), nullable=True),
        sa.Column('qb_realm_id', sa.String(50), nullable=True),
        sa.Column('qb_attachment_id', sa.String(50), nullable=True),

        # Todoist
        sa.Column('todoist_task_id', sa.String(50), nullable=True),

        # Approval
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('approved_by', sa.String(100), nullable=True),
        sa.Column('approved_revision', sa.Integer(), nullable=True),
        sa.Column('approved_values', sa.JSON(), nullable=True),

        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_documents_sha256_hash', 'documents', ['sha256_hash'], unique=True)
    op.create_index('ix_documents_qb_bill_id', 'documents', ['qb_bill_id'])

    # Arrivals table
    op.create_table('arrivals',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('document_id', sa.String(36), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('arrived_at', sa.DateTime(), nullable=False),
        sa.Column('method', sa.Enum('email', 'upload', name='arrivalmethod'), nullable=False),
        sa.Column('original_filename', sa.String(500), nullable=True),

        # Email fields
        sa.Column('gmail_message_id', sa.String(100), nullable=True),
        sa.Column('gmail_attachment_id', sa.String(200), nullable=True),
        sa.Column('email_from', sa.String(500), nullable=True),
        sa.Column('email_subject', sa.String(500), nullable=True),
        sa.Column('email_received_at', sa.DateTime(), nullable=True),
        sa.Column('email_source_path', sa.String(500), nullable=True),

        # Upload fields
        sa.Column('uploaded_by', sa.String(100), nullable=True),
        sa.Column('upload_attempt_id', sa.String(36), nullable=True),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('gmail_message_id', 'gmail_attachment_id', name='uq_gmail_arrival'),
        sa.UniqueConstraint('upload_attempt_id', name='uq_upload_attempt')
    )
    op.create_index('ix_arrivals_document_id', 'arrivals', ['document_id'])

    # Audit events table
    op.create_table('audit_events',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('document_id', sa.String(36), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('event_type', sa.Enum('document_created', 'arrival_recorded', 'extraction_started',
                                        'extraction_completed', 'extraction_failed', 'field_corrected',
                                        'status_changed', 'duplicate_check_completed', 'duplicate_dismissed',
                                        'linked_to_existing', 'approval_granted', 'bill_creation_started',
                                        'bill_created', 'bill_creation_failed', 'attachment_started',
                                        'attachment_completed', 'attachment_failed', 'task_created',
                                        'task_updated', 'task_creation_failed',
                                        name='eventtype'), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('actor', sa.String(100), nullable=False),
        sa.Column('details', sa.JSON(), nullable=True),

        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_events_document_id', 'audit_events', ['document_id'])
    op.create_index('ix_audit_events_occurred_at', 'audit_events', ['occurred_at'])

    # Duplicate candidates table
    op.create_table('duplicate_candidates',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('document_id', sa.String(36), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('found_at', sa.DateTime(), nullable=False),
        sa.Column('qb_bill_id', sa.String(50), nullable=False),
        sa.Column('qb_doc_number', sa.String(100), nullable=True),
        sa.Column('qb_vendor_name', sa.String(200), nullable=True),
        sa.Column('qb_total', sa.Numeric(12, 2), nullable=True),
        sa.Column('qb_txn_date', sa.Date(), nullable=True),
        sa.Column('match_reason', sa.Text(), nullable=False),
        sa.Column('match_fields', sa.JSON(), nullable=True),
        sa.Column('resolved', sa.Boolean(), nullable=False, default=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_by', sa.String(100), nullable=True),
        sa.Column('resolution', sa.String(50), nullable=True),
        sa.Column('resolution_reason', sa.Text(), nullable=True),

        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_duplicate_candidates_document_id', 'duplicate_candidates', ['document_id'])

    # Operations table
    op.create_table('operations',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('document_id', sa.String(36), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('operation_type', sa.Enum('create_bill', 'attach_file', 'create_task', 'update_task',
                                            name='operationtype'), nullable=False),
        sa.Column('status', sa.Enum('pending', 'in_progress', 'completed', 'failed', 'unknown',
                                    name='operationstatus'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('request_payload_hash', sa.String(64), nullable=True),
        sa.Column('request_id', sa.String(100), nullable=True),
        sa.Column('result_id', sa.String(100), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False, default=0),

        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_operations_document_id', 'operations', ['document_id'])

    # Provider connections table
    op.create_table('provider_connections',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('access_token_encrypted', sa.Text(), nullable=True),
        sa.Column('refresh_token_encrypted', sa.Text(), nullable=True),
        sa.Column('token_expiry', sa.DateTime(), nullable=True),
        sa.Column('connected_at', sa.DateTime(), nullable=True),
        sa.Column('connected_account', sa.String(200), nullable=True),
        sa.Column('realm_id', sa.String(50), nullable=True),
        sa.Column('company_name', sa.String(200), nullable=True),
        sa.Column('is_valid', sa.Boolean(), nullable=False, default=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_refreshed_at', sa.DateTime(), nullable=True),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider')
    )

    # Vendor mappings table
    op.create_table('vendor_mappings',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('extracted_name', sa.String(200), nullable=False),
        sa.Column('qb_vendor_id', sa.String(50), nullable=False),
        sa.Column('qb_vendor_name', sa.String(200), nullable=False),
        sa.Column('realm_id', sa.String(50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('extracted_name', 'realm_id', name='uq_vendor_mapping')
    )
    op.create_index('ix_vendor_mappings_extracted_name', 'vendor_mappings', ['extracted_name'])

    # Account mappings table
    op.create_table('account_mappings',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('category', sa.String(100), nullable=False),
        sa.Column('qb_account_id', sa.String(50), nullable=False),
        sa.Column('qb_account_name', sa.String(200), nullable=False),
        sa.Column('realm_id', sa.String(50), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False, default=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category', 'realm_id', name='uq_account_mapping')
    )
    op.create_index('ix_account_mappings_category', 'account_mappings', ['category'])


def downgrade() -> None:
    op.drop_table('account_mappings')
    op.drop_table('vendor_mappings')
    op.drop_table('provider_connections')
    op.drop_table('operations')
    op.drop_table('duplicate_candidates')
    op.drop_table('audit_events')
    op.drop_table('arrivals')
    op.drop_table('documents')
    op.drop_table('properties')
