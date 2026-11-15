"""durable_audit_trail_and_usd_currency

Revision ID: 6a3b2d1be23a
Revises: d6618fc8ca8d
Create Date: 2026-11-13 18:52:07.737399

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a3b2d1be23a'
down_revision: Union[str, Sequence[str], None] = 'd6618fc8ca8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to durable audit trail and USD default currency."""
    op.alter_column(
        'exception_history',
        'exception_id',
        existing_type=sa.VARCHAR(length=32),
        nullable=True,
    )
    op.drop_constraint(
        'exception_history_exception_id_fkey',
        'exception_history',
        type_='foreignkey',
    )
    op.create_foreign_key(
        'exception_history_exception_id_fkey',
        'exception_history',
        'exceptions',
        ['exception_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.alter_column(
        'purchase_orders',
        'currency',
        server_default='USD',
        existing_type=sa.String(length=8),
    )
    op.alter_column(
        'exceptions',
        'currency',
        server_default='USD',
        existing_type=sa.String(length=8),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'exceptions',
        'currency',
        server_default='PKR',
        existing_type=sa.String(length=8),
    )
    op.alter_column(
        'purchase_orders',
        'currency',
        server_default='PKR',
        existing_type=sa.String(length=8),
    )
    op.drop_constraint(
        'exception_history_exception_id_fkey',
        'exception_history',
        type_='foreignkey',
    )
    op.create_foreign_key(
        'exception_history_exception_id_fkey',
        'exception_history',
        'exceptions',
        ['exception_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.alter_column(
        'exception_history',
        'exception_id',
        existing_type=sa.VARCHAR(length=32),
        nullable=False,
    )
