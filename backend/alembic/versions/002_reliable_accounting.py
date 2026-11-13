"""Reliable accounting writes: claims, durable operations, candidate de-duplication

Revision ID: 002
Revises: 001
Create Date: 2026-11-13 00:00:00.000000

Additive only; existing rows are preserved.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('documents') as batch:
        batch.add_column(sa.Column('qb_discrepancy', sa.Text(), nullable=True))
        batch.add_column(sa.Column('last_accounting_check_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('last_accounting_check_ok', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('last_accounting_check_error', sa.Text(), nullable=True))

    # Collapse any duplicate candidate rows created by repeated checks before adding the constraint.
    op.execute(
        """
        DELETE FROM duplicate_candidates
        WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM duplicate_candidates GROUP BY document_id, qb_bill_id
        )
        """
    )
    with op.batch_alter_table('duplicate_candidates') as batch:
        batch.add_column(sa.Column('realm_id', sa.String(50), nullable=True))
        batch.add_column(sa.Column('qb_vendor_id', sa.String(50), nullable=True))
        batch.add_column(sa.Column('qb_currency', sa.String(3), nullable=True))
        batch.add_column(sa.Column('last_seen_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('seen_count', sa.Integer(), nullable=False, server_default='1'))
        batch.add_column(sa.Column('match_details', sa.JSON(), nullable=True))
        batch.create_unique_constraint('uq_candidate_document_bill', ['document_id', 'qb_bill_id'])

    with op.batch_alter_table('operations') as batch:
        batch.add_column(sa.Column('realm_id', sa.String(50), nullable=True))
        batch.add_column(sa.Column('target_id', sa.String(100), nullable=True))
        batch.add_column(sa.Column('accounting_identity', sa.String(300), nullable=True))
        batch.add_column(sa.Column('approved_revision', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('payload', sa.JSON(), nullable=True))
        batch.create_unique_constraint('uq_operation_request_id', ['request_id'])

    op.create_table(
        'accounting_claims',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('realm_id', sa.String(50), nullable=False),
        sa.Column('qb_vendor_id', sa.String(50), nullable=False),
        sa.Column('invoice_key', sa.String(100), nullable=False),
        sa.Column('document_id', sa.String(36), sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('realm_id', 'qb_vendor_id', 'invoice_key', name='uq_accounting_claim'),
    )
    op.create_index('ix_accounting_claims_document_id', 'accounting_claims', ['document_id'])


def downgrade() -> None:
    op.drop_index('ix_accounting_claims_document_id', table_name='accounting_claims')
    op.drop_table('accounting_claims')
    with op.batch_alter_table('operations') as batch:
        batch.drop_constraint('uq_operation_request_id', type_='unique')
        for col in ('payload', 'approved_revision', 'accounting_identity', 'target_id', 'realm_id'):
            batch.drop_column(col)
    with op.batch_alter_table('duplicate_candidates') as batch:
        batch.drop_constraint('uq_candidate_document_bill', type_='unique')
        for col in ('match_details', 'seen_count', 'last_seen_at', 'qb_currency', 'qb_vendor_id', 'realm_id'):
            batch.drop_column(col)
    with op.batch_alter_table('documents') as batch:
        for col in ('last_accounting_check_error', 'last_accounting_check_ok',
                    'last_accounting_check_at', 'qb_discrepancy'):
            batch.drop_column(col)
