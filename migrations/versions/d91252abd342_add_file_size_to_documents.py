"""add_file_size_to_documents

Revision ID: d91252abd342
Revises: a1f3c8e92d47
Create Date: 2026-11-14 10:58:15.378577

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd91252abd342'
down_revision: Union[str, Sequence[str], None] = 'a1f3c8e92d47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('file_size', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'file_size')
