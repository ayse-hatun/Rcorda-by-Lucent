"""add_reconciliation_runs_and_discrepancies

Revision ID: f5a6b7c8d9e2
Revises: e4f5a7c8b9d1
Create Date: 2026-11-14 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d9e2'
down_revision: Union[str, Sequence[str], None] = 'e4f5a7c8b9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    # Create reconciliation_runs table
    op.create_table(
        'reconciliation_runs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('root_document_id', sa.Uuid(), nullable=False),
        sa.Column('po_document_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=32), server_default='PASS', nullable=False),
        sa.Column('reconciled_document_ids', json_type, nullable=True),
        sa.Column('summary', json_type, nullable=True),
        sa.Column('created_by', sa.String(length=128), server_default='SYSTEM', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['root_document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['po_document_id'], ['documents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reconciliation_runs_root_document_id'), 'reconciliation_runs', ['root_document_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_runs_po_document_id'), 'reconciliation_runs', ['po_document_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_runs_status'), 'reconciliation_runs', ['status'], unique=False)

    # Create reconciliation_discrepancies table
    op.create_table(
        'reconciliation_discrepancies',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('discrepancy_type', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=32), server_default='CRITICAL', nullable=False),
        sa.Column('item_code', sa.String(length=64), nullable=True),
        sa.Column('line_item_description', sa.String(length=255), nullable=True),
        sa.Column('expected_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('actual_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('delta_value', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('currency', sa.String(length=8), server_default='USD', nullable=False),
        sa.Column('amount_at_risk', sa.Numeric(precision=18, scale=4), server_default='0.0000', nullable=False),
        sa.Column('source_evidence', json_type, nullable=True),
        sa.Column('explanation', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reconciliation_discrepancies_run_id'), 'reconciliation_discrepancies', ['run_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_discrepancies_discrepancy_type'), 'reconciliation_discrepancies', ['discrepancy_type'], unique=False)
    op.create_index(op.f('ix_reconciliation_discrepancies_severity'), 'reconciliation_discrepancies', ['severity'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reconciliation_discrepancies_severity'), table_name='reconciliation_discrepancies')
    op.drop_index(op.f('ix_reconciliation_discrepancies_discrepancy_type'), table_name='reconciliation_discrepancies')
    op.drop_index(op.f('ix_reconciliation_discrepancies_run_id'), table_name='reconciliation_discrepancies')
    op.drop_table('reconciliation_discrepancies')

    op.drop_index(op.f('ix_reconciliation_runs_status'), table_name='reconciliation_runs')
    op.drop_index(op.f('ix_reconciliation_runs_po_document_id'), table_name='reconciliation_runs')
    op.drop_index(op.f('ix_reconciliation_runs_root_document_id'), table_name='reconciliation_runs')
    op.drop_table('reconciliation_runs')
