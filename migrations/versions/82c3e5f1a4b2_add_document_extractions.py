"""add_document_extractions

Revision ID: 82c3e5f1a4b2
Revises: 71b2d4e9f3a1
Create Date: 2026-11-14 13:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '82c3e5f1a4b2'
down_revision: Union[str, Sequence[str], None] = '71b2d4e9f3a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    op.create_table(
        'document_extractions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('ocr_run_id', sa.Uuid(), nullable=True),
        sa.Column('run_number', sa.Integer(), nullable=False),
        sa.Column('document_type', sa.String(length=32), nullable=False),
        sa.Column('extractor_version', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), server_default='COMPLETED', nullable=False),
        sa.Column('fields', json_type, nullable=True),
        sa.Column('line_items', json_type, nullable=True),
        sa.Column('field_provenance', json_type, nullable=True),
        sa.Column('validation_warnings', json_type, nullable=True),
        sa.Column('needs_human_review', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['ocr_run_id'], ['ocr_runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'run_number', name='uq_document_extractions_run')
    )
    op.create_index(op.f('ix_document_extractions_document_id'), 'document_extractions', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_extractions_ocr_run_id'), 'document_extractions', ['ocr_run_id'], unique=False)
    op.create_index(op.f('ix_document_extractions_status'), 'document_extractions', ['status'], unique=False)
    op.create_index(op.f('ix_document_extractions_needs_human_review'), 'document_extractions', ['needs_human_review'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_document_extractions_needs_human_review'), table_name='document_extractions')
    op.drop_index(op.f('ix_document_extractions_status'), table_name='document_extractions')
    op.drop_index(op.f('ix_document_extractions_ocr_run_id'), table_name='document_extractions')
    op.drop_index(op.f('ix_document_extractions_document_id'), table_name='document_extractions')
    op.drop_table('document_extractions')
