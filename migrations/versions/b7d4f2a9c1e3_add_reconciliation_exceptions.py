"""add_reconciliation_exceptions

Revision ID: b7d4f2a9c1e3
Revises: f5a6b7c8d9e2
Create Date: 2026-11-14 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7d4f2a9c1e3'
down_revision: Union[str, Sequence[str], None] = 'f5a6b7c8d9e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    op.create_table(
        'reconciliation_exceptions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('discrepancy_id', sa.Uuid(), nullable=False),
        sa.Column('reconciliation_run_id', sa.Uuid(), nullable=False),
        sa.Column('exception_type', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=32), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('supplier_name', sa.String(length=255), nullable=True),
        sa.Column('po_document_id', sa.Uuid(), nullable=True),
        sa.Column('po_reference', sa.String(length=128), nullable=True),
        sa.Column('related_document_ids', json_type, nullable=True),
        sa.Column('financial_impact', sa.Numeric(precision=18, scale=4), server_default='0.0000', nullable=False),
        sa.Column('currency', sa.String(length=8), server_default='USD', nullable=False),
        sa.Column('status', sa.String(length=32), server_default='OPEN', nullable=False),
        sa.Column('item_code', sa.String(length=64), nullable=True),
        sa.Column('line_item_description', sa.String(length=255), nullable=True),
        sa.Column('source_evidence', json_type, nullable=True),
        sa.Column('resolution_notes', sa.Text(), nullable=True),
        sa.Column('resolution_supporting_information', json_type, nullable=True),
        sa.Column('resolved_by', sa.String(length=128), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(length=128), server_default='SYSTEM', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['discrepancy_id'], ['reconciliation_discrepancies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['reconciliation_run_id'], ['reconciliation_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['po_document_id'], ['documents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('discrepancy_id', name='uq_reconciliation_exceptions_discrepancy_id'),
    )
    op.create_index(op.f('ix_reconciliation_exceptions_discrepancy_id'), 'reconciliation_exceptions', ['discrepancy_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_exceptions_reconciliation_run_id'), 'reconciliation_exceptions', ['reconciliation_run_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_exceptions_exception_type'), 'reconciliation_exceptions', ['exception_type'], unique=False)
    op.create_index(op.f('ix_reconciliation_exceptions_severity'), 'reconciliation_exceptions', ['severity'], unique=False)
    op.create_index(op.f('ix_reconciliation_exceptions_po_document_id'), 'reconciliation_exceptions', ['po_document_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_exceptions_po_reference'), 'reconciliation_exceptions', ['po_reference'], unique=False)
    op.create_index(op.f('ix_reconciliation_exceptions_status'), 'reconciliation_exceptions', ['status'], unique=False)

    op.create_table(
        'reconciliation_exception_history',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('exception_id', sa.Uuid(), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('actor_id', sa.String(length=128), server_default='SYSTEM', nullable=False),
        sa.Column('from_status', sa.String(length=32), nullable=True),
        sa.Column('to_status', sa.String(length=32), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('details', json_type, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['exception_id'], ['reconciliation_exceptions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_reconciliation_exception_history_exception_id'), 'reconciliation_exception_history', ['exception_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reconciliation_exception_history_exception_id'), table_name='reconciliation_exception_history')
    op.drop_table('reconciliation_exception_history')

    op.drop_index(op.f('ix_reconciliation_exceptions_status'), table_name='reconciliation_exceptions')
    op.drop_index(op.f('ix_reconciliation_exceptions_po_reference'), table_name='reconciliation_exceptions')
    op.drop_index(op.f('ix_reconciliation_exceptions_po_document_id'), table_name='reconciliation_exceptions')
    op.drop_index(op.f('ix_reconciliation_exceptions_severity'), table_name='reconciliation_exceptions')
    op.drop_index(op.f('ix_reconciliation_exceptions_exception_type'), table_name='reconciliation_exceptions')
    op.drop_index(op.f('ix_reconciliation_exceptions_reconciliation_run_id'), table_name='reconciliation_exceptions')
    op.drop_index(op.f('ix_reconciliation_exceptions_discrepancy_id'), table_name='reconciliation_exceptions')
    op.drop_table('reconciliation_exceptions')
