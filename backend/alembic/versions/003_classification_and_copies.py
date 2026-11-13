"""Document classification and same-invoice copy detection

Revision ID: 003
Revises: 002
Create Date: 2026-11-13 12:00:00.000000

Additive only; existing rows are preserved. New status values ("filed",
"possible_copy", "linked_copy") need no schema change: SQLite stores the
status as text without a CHECK constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('documents') as batch:
        batch.add_column(sa.Column('reviewed_document_type', sa.String(30), nullable=True))
        batch.add_column(sa.Column('copy_of_document_id', sa.String(36), nullable=True))
        batch.add_column(sa.Column('copy_dismissed_ids', sa.JSON(), nullable=True))
        batch.create_foreign_key('fk_documents_copy_of', 'documents', ['copy_of_document_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('documents') as batch:
        batch.drop_constraint('fk_documents_copy_of', type_='foreignkey')
        batch.drop_column('copy_dismissed_ids')
        batch.drop_column('copy_of_document_id')
        batch.drop_column('reviewed_document_type')
