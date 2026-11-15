"""add_delivery_schedule_tables

Revision ID: a1f3c8e92d47
Revises: 6a3b2d1be23a
Create Date: 2026-11-13 19:10:00.000000

Adds 7 new tables for PostgreSQL-backed delivery schedule persistence:
  schedule_po_items
  delivery_schedules
  schedule_amendments
  evidence_documents_schedule
  source_document_lines
  source_document_line_po_items
  quantity_allocations
  delivery_validation_runs

Business rules (previously enforced by SQLite triggers) are now enforced
at the application layer in PgDeliveryScheduleStore.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a1f3c8e92d47"
down_revision: Union[str, Sequence[str], None] = "6a3b2d1be23a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create delivery schedule tables."""

    # 1. schedule_po_items
    op.create_table(
        "schedule_po_items",
        sa.Column("po_item_id", sa.String(64), primary_key=True),
        sa.Column("po_reference", sa.String(128), nullable=False),
        sa.Column("line_item_id", sa.String(64), nullable=False),
        sa.Column("item_code", sa.String(64), nullable=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("ordered_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False, server_default="pcs"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_schedule_po_items_po_reference",
        "schedule_po_items",
        ["po_reference"],
    )

    # 2. delivery_schedules
    op.create_table(
        "delivery_schedules",
        sa.Column("schedule_id", sa.String(64), primary_key=True),
        sa.Column(
            "po_item_id",
            sa.String(64),
            sa.ForeignKey(
                "schedule_po_items.po_item_id",
                ondelete="CASCADE",
                name="fk_delivery_schedules_po_item_id",
            ),
            nullable=False,
        ),
        sa.Column("planned_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("expected_delivery_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="SCHEDULED"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_delivery_schedules_po_item_id",
        "delivery_schedules",
        ["po_item_id"],
    )

    # 3. schedule_amendments
    op.create_table(
        "schedule_amendments",
        sa.Column("amendment_id", sa.String(64), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.String(64),
            sa.ForeignKey(
                "delivery_schedules.schedule_id",
                ondelete="CASCADE",
                name="fk_schedule_amendments_schedule_id",
            ),
            nullable=False,
        ),
        sa.Column("previous_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("previous_expected_date", sa.Date, nullable=False),
        sa.Column("proposed_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("proposed_expected_date", sa.Date, nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="PENDING_APPROVAL",
        ),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_schedule_amendments_schedule_id",
        "schedule_amendments",
        ["schedule_id"],
    )

    # 4. evidence_documents_schedule
    op.create_table(
        "evidence_documents_schedule",
        sa.Column("document_type", sa.String(32), nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("document_number", sa.String(128), nullable=False),
        sa.Column("po_reference", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "document_type", "document_id", name="pk_evidence_docs"
        ),
        sa.UniqueConstraint("sha256", name="uq_evidence_docs_sha256"),
    )
    op.create_index(
        "ix_evidence_docs_po_reference",
        "evidence_documents_schedule",
        ["po_reference"],
    )

    # 5. source_document_lines
    op.create_table(
        "source_document_lines",
        sa.Column("document_type", sa.String(32), nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("line_item_id", sa.String(64), nullable=False),
        sa.Column("line_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("evidence_date", sa.Date, nullable=False),
        sa.PrimaryKeyConstraint(
            "document_type",
            "document_id",
            "line_item_id",
            name="pk_source_doc_lines",
        ),
        sa.ForeignKeyConstraint(
            ["document_type", "document_id"],
            [
                "evidence_documents_schedule.document_type",
                "evidence_documents_schedule.document_id",
            ],
            ondelete="CASCADE",
            name="fk_source_lines_evidence_doc",
        ),
    )

    # 6. source_document_line_po_items
    op.create_table(
        "source_document_line_po_items",
        sa.Column("document_type", sa.String(32), nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("line_item_id", sa.String(64), nullable=False),
        sa.Column(
            "po_item_id",
            sa.String(64),
            sa.ForeignKey(
                "schedule_po_items.po_item_id",
                ondelete="CASCADE",
                name="fk_line_po_items_schedule_po_item",
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "document_type",
            "document_id",
            "line_item_id",
            "po_item_id",
            name="pk_src_doc_line_po_items",
        ),
        sa.ForeignKeyConstraint(
            ["document_type", "document_id", "line_item_id"],
            [
                "source_document_lines.document_type",
                "source_document_lines.document_id",
                "source_document_lines.line_item_id",
            ],
            ondelete="CASCADE",
            name="fk_line_po_items_source_line",
        ),
    )
    op.create_index(
        "ix_src_doc_line_po_items_po_item_id",
        "source_document_line_po_items",
        ["po_item_id"],
    )

    # 7. quantity_allocations
    op.create_table(
        "quantity_allocations",
        sa.Column("allocation_id", sa.String(64), primary_key=True),
        sa.Column("allocation_type", sa.String(16), nullable=False),
        sa.Column("source_document_type", sa.String(32), nullable=False),
        sa.Column("source_document_id", sa.String(128), nullable=False),
        sa.Column("source_line_item_id", sa.String(64), nullable=False),
        sa.Column(
            "po_item_id",
            sa.String(64),
            sa.ForeignKey(
                "schedule_po_items.po_item_id",
                ondelete="CASCADE",
                name="fk_qty_alloc_po_item",
            ),
            nullable=False,
        ),
        sa.Column(
            "schedule_id",
            sa.String(64),
            sa.ForeignKey(
                "delivery_schedules.schedule_id",
                ondelete="SET NULL",
                name="fk_qty_alloc_schedule",
            ),
            nullable=True,
        ),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "matched_dispatch_allocation_id",
            sa.String(64),
            sa.ForeignKey(
                "quantity_allocations.allocation_id",
                ondelete="SET NULL",
                name="fk_qty_alloc_matched_dispatch",
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_quantity_allocations_po_item_id",
        "quantity_allocations",
        ["po_item_id"],
    )
    op.create_index(
        "ix_quantity_allocations_schedule_id",
        "quantity_allocations",
        ["schedule_id"],
    )
    op.create_index(
        "ix_quantity_allocations_allocation_type",
        "quantity_allocations",
        ["allocation_type"],
    )
    op.create_index(
        "ix_qty_alloc_target",
        "quantity_allocations",
        [
            "allocation_type",
            "source_document_type",
            "source_document_id",
            "source_line_item_id",
            "po_item_id",
            "schedule_id",
        ],
    )

    # 8. delivery_validation_runs
    op.create_table(
        "delivery_validation_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column(
            "po_item_id",
            sa.String(64),
            sa.ForeignKey(
                "schedule_po_items.po_item_id",
                ondelete="CASCADE",
                name="fk_validation_runs_po_item",
            ),
            nullable=False,
        ),
        sa.Column("evaluation_date", sa.Date, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("result_json", JSONB, nullable=False),
    )
    op.create_index(
        "ix_delivery_validation_runs_po_item_id",
        "delivery_validation_runs",
        ["po_item_id"],
    )


def downgrade() -> None:
    """Drop delivery schedule tables in reverse dependency order."""
    op.drop_table("delivery_validation_runs")
    op.drop_table("quantity_allocations")
    op.drop_table("source_document_line_po_items")
    op.drop_table("source_document_lines")
    op.drop_table("evidence_documents_schedule")
    op.drop_table("schedule_amendments")
    op.drop_table("delivery_schedules")
    op.drop_table("schedule_po_items")
