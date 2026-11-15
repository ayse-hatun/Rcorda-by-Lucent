"""add_document_relationships_and_match_audits

Revision ID: e4f5a7c8b9d1
Revises: 93d4a6b2c5e3
Create Date: 2026-11-14 14:18:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e4f5a7c8b9d1'
down_revision: Union[str, Sequence[str], None] = '93d4a6b2c5e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    # Create document_relationships
    op.create_table(
        'document_relationships',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('source_document_id', sa.Uuid(), nullable=False),
        sa.Column('target_document_id', sa.Uuid(), nullable=False),
        sa.Column('relationship_type', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), server_default='CONFIRMED', nullable=False),
        sa.Column('matching_method', sa.String(length=64), nullable=False),
        sa.Column('matched_references', json_type, nullable=True),
        sa.Column('evidence', json_type, nullable=True),
        sa.Column('confidence_score', sa.Float(), server_default='1.0', nullable=False),
        sa.Column('created_by', sa.String(length=128), server_default='SYSTEM', nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['source_document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_document_id', 'target_document_id', 'relationship_type', name='uq_document_relationship'),
        sa.CheckConstraint('source_document_id != target_document_id', name='chk_no_self_relationship')
    )
    op.create_index(op.f('ix_document_relationships_source_document_id'), 'document_relationships', ['source_document_id'], unique=False)
    op.create_index(op.f('ix_document_relationships_target_document_id'), 'document_relationships', ['target_document_id'], unique=False)
    op.create_index(op.f('ix_document_relationships_relationship_type'), 'document_relationships', ['relationship_type'], unique=False)
    op.create_index(op.f('ix_document_relationships_status'), 'document_relationships', ['status'], unique=False)

    # Create document_match_audits
    op.create_table(
        'document_match_audits',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('relationship_id', sa.Uuid(), nullable=True),
        sa.Column('source_document_id', sa.Uuid(), nullable=False),
        sa.Column('target_document_id', sa.Uuid(), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('previous_status', sa.String(length=32), nullable=True),
        sa.Column('new_status', sa.String(length=32), nullable=True),
        sa.Column('actor', sa.String(length=128), server_default='SYSTEM', nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('evidence', json_type, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['relationship_id'], ['document_relationships.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['source_document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_document_id'], ['documents.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_match_audits_relationship_id'), 'document_match_audits', ['relationship_id'], unique=False)
    op.create_index(op.f('ix_document_match_audits_source_document_id'), 'document_match_audits', ['source_document_id'], unique=False)
    op.create_index(op.f('ix_document_match_audits_target_document_id'), 'document_match_audits', ['target_document_id'], unique=False)
    op.create_index(op.f('ix_document_match_audits_action'), 'document_match_audits', ['action'], unique=False)
    op.create_index(op.f('ix_document_match_audits_created_at'), 'document_match_audits', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_document_match_audits_created_at'), table_name='document_match_audits')
    op.drop_index(op.f('ix_document_match_audits_action'), table_name='document_match_audits')
    op.drop_index(op.f('ix_document_match_audits_target_document_id'), table_name='document_match_audits')
    op.drop_index(op.f('ix_document_match_audits_source_document_id'), table_name='document_match_audits')
    op.drop_index(op.f('ix_document_match_audits_relationship_id'), table_name='document_match_audits')
    op.drop_table('document_match_audits')

    op.drop_index(op.f('ix_document_relationships_status'), table_name='document_relationships')
    op.drop_index(op.f('ix_document_relationships_relationship_type'), table_name='document_relationships')
    op.drop_index(op.f('ix_document_relationships_target_document_id'), table_name='document_relationships')
    op.drop_index(op.f('ix_document_relationships_source_document_id'), table_name='document_relationships')
    op.drop_table('document_relationships')
