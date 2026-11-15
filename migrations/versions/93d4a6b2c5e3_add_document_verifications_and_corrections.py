"""add_document_verifications_and_corrections

Revision ID: 93d4a6b2c5e3
Revises: 82c3e5f1a4b2
Create Date: 2026-11-14 13:33:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '93d4a6b2c5e3'
down_revision: Union[str, Sequence[str], None] = '82c3e5f1a4b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    # Create document_verifications
    op.create_table(
        'document_verifications',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('extraction_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=32), server_default='PENDING_REVIEW', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('verified_fields', json_type, nullable=True),
        sa.Column('verified_line_items', json_type, nullable=True),
        sa.Column('assigned_reviewer_id', sa.String(length=128), nullable=True),
        sa.Column('reviewed_by', sa.String(length=128), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['extraction_id'], ['document_extractions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_verifications_document_id'), 'document_verifications', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_verifications_extraction_id'), 'document_verifications', ['extraction_id'], unique=False)
    op.create_index(op.f('ix_document_verifications_status'), 'document_verifications', ['status'], unique=False)
    op.create_index(op.f('ix_document_verifications_assigned_reviewer_id'), 'document_verifications', ['assigned_reviewer_id'], unique=False)

    # Create verification_corrections
    op.create_table(
        'verification_corrections',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('verification_id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('correction_type', sa.String(length=32), nullable=False),
        sa.Column('field_name', sa.String(length=64), nullable=True),
        sa.Column('line_item_index', sa.Integer(), nullable=True),
        sa.Column('original_value', json_type, nullable=True),
        sa.Column('corrected_value', json_type, nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('corrected_by', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['verification_id'], ['document_verifications.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_verification_corrections_verification_id'), 'verification_corrections', ['verification_id'], unique=False)
    op.create_index(op.f('ix_verification_corrections_document_id'), 'verification_corrections', ['document_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_verification_corrections_document_id'), table_name='verification_corrections')
    op.drop_index(op.f('ix_verification_corrections_verification_id'), table_name='verification_corrections')
    op.drop_table('verification_corrections')

    op.drop_index(op.f('ix_document_verifications_assigned_reviewer_id'), table_name='document_verifications')
    op.drop_index(op.f('ix_document_verifications_status'), table_name='document_verifications')
    op.drop_index(op.f('ix_document_verifications_extraction_id'), table_name='document_verifications')
    op.drop_index(op.f('ix_document_verifications_document_id'), table_name='document_verifications')
    op.drop_table('document_verifications')
