"""add_document_classifications

Revision ID: 71b2d4e9f3a1
Revises: 50f3e31b1414
Create Date: 2026-11-14 12:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '71b2d4e9f3a1'
down_revision: Union[str, Sequence[str], None] = '50f3e31b1414'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    op.create_table(
        'document_classifications',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('run_number', sa.Integer(), nullable=False),
        sa.Column('predicted_type', sa.String(length=32), nullable=False),
        sa.Column('confidence_score', sa.Float(), nullable=False),
        sa.Column('probabilities', json_type, nullable=True),
        sa.Column('needs_human_review', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('review_reasons', json_type, nullable=True),
        sa.Column('reviewed_by', sa.String(length=128), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('actual_type', sa.String(length=32), nullable=True),
        sa.Column('status', sa.String(length=32), server_default='COMPLETED', nullable=False),
        sa.Column('model_name', sa.String(length=64), nullable=False),
        sa.Column('model_version', sa.String(length=32), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'run_number', name='uq_document_classifications_run')
    )
    op.create_index(op.f('ix_document_classifications_document_id'), 'document_classifications', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_classifications_needs_human_review'), 'document_classifications', ['needs_human_review'], unique=False)
    op.create_index(op.f('ix_document_classifications_status'), 'document_classifications', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_document_classifications_status'), table_name='document_classifications')
    op.drop_index(op.f('ix_document_classifications_needs_human_review'), table_name='document_classifications')
    op.drop_index(op.f('ix_document_classifications_document_id'), table_name='document_classifications')
    op.drop_table('document_classifications')
