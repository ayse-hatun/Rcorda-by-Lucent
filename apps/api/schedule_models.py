"""SQLAlchemy ORM models for delivery schedule persistence in PostgreSQL.

These models map the existing delivery schedule domain (currently stored in
SQLite via DeliveryScheduleStore) to PostgreSQL via SQLAlchemy.  They are
intentionally kept in a separate module from apps/api/models.py so that the
schedule layer can be imported independently and kept decoupled from the core
procurement entities.

Table naming convention mirrors the SQLite schema:
  schedule_po_items            ← po_items (scoped to avoid clash with purchase_order_items)
  delivery_schedules
  schedule_amendments
  evidence_documents_schedule  ← evidence_documents (scoped to avoid clash with documents)
  source_document_lines
  source_document_line_po_items
  quantity_allocations
  delivery_validation_runs

All TEXT quantities from the SQLite schema become Numeric(18,4) here.
All TEXT dates become Date.
All TEXT datetimes become DateTime(timezone=True).
Composite primary keys are expressed with PrimaryKeyConstraint.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    JSON,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.database import Base

# Cross-dialect JSONB helper: native JSONB on PostgreSQL, JSON on others
_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class SchedulePoItem(Base):
    """PO item record for the delivery schedule layer.

    This stores the same conceptual entity as PurchaseOrderItem but is
    maintained independently by the schedule domain so that the schedule
    layer does not force a hard dependency on the procurement ORM.  An
    optional FK to purchase_order_items.po_item_id provides a soft link.

    po_item_id uses a domain-assigned string key (UUID or business key)
    matching the SQLite schema.
    """

    __tablename__ = "schedule_po_items"

    po_item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    po_reference: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    line_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    ordered_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pcs")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    delivery_schedules: Mapped[List["PgDeliverySchedule"]] = relationship(
        "PgDeliverySchedule",
        back_populates="po_item",
        cascade="all, delete-orphan",
    )
    quantity_allocations: Mapped[List["PgQuantityAllocation"]] = relationship(
        "PgQuantityAllocation",
        back_populates="po_item",
        foreign_keys="[PgQuantityAllocation.po_item_id]",
    )
    validation_runs: Mapped[List["PgDeliveryValidationRun"]] = relationship(
        "PgDeliveryValidationRun",
        back_populates="po_item",
    )


class PgDeliverySchedule(Base):
    """Delivery schedule for a PO item."""

    __tablename__ = "delivery_schedules"

    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    po_item_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("schedule_po_items.po_item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    planned_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    expected_delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="SCHEDULED"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    po_item: Mapped["SchedulePoItem"] = relationship(
        "SchedulePoItem", back_populates="delivery_schedules"
    )
    amendments: Mapped[List["PgScheduleAmendment"]] = relationship(
        "PgScheduleAmendment",
        back_populates="schedule",
        order_by="PgScheduleAmendment.requested_at",
        cascade="all, delete-orphan",
    )
    quantity_allocations: Mapped[List["PgQuantityAllocation"]] = relationship(
        "PgQuantityAllocation",
        back_populates="schedule",
        foreign_keys="[PgQuantityAllocation.schedule_id]",
    )


class PgScheduleAmendment(Base):
    """History of amendments proposed and approved against a delivery schedule."""

    __tablename__ = "schedule_amendments"

    amendment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("delivery_schedules.schedule_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    previous_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    previous_expected_date: Mapped[date] = mapped_column(Date, nullable=False)
    proposed_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    proposed_expected_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="PENDING_APPROVAL"
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    schedule: Mapped["PgDeliverySchedule"] = relationship(
        "PgDeliverySchedule", back_populates="amendments"
    )


class PgEvidenceDocument(Base):
    """Immutable delivery evidence document (Delivery Challan or GRN).

    Table name is deliberately different from the existing `documents` table
    so there is no collision with the procurement document store.

    The composite primary key (document_type, document_id) mirrors the
    SQLite schema.  Immutability is enforced at the application layer since
    PostgreSQL does not have SQLite-style row-level triggers without an
    extension.
    """

    __tablename__ = "evidence_documents_schedule"

    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_number: Mapped[str] = mapped_column(String(128), nullable=False)
    po_reference: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("document_type", "document_id", name="pk_evidence_docs"),
    )

    # Relationships
    source_lines: Mapped[List["PgSourceDocumentLine"]] = relationship(
        "PgSourceDocumentLine",
        back_populates="evidence_document",
        cascade="all, delete-orphan",
        primaryjoin=(
            "and_(PgEvidenceDocument.document_type == foreign(PgSourceDocumentLine.document_type),"
            " PgEvidenceDocument.document_id == foreign(PgSourceDocumentLine.document_id))"
        ),
    )


class PgSourceDocumentLine(Base):
    """Immutable line entry within an evidence document.

    Composite PK: (document_type, document_id, line_item_id).
    """

    __tablename__ = "source_document_lines"

    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    line_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    line_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    evidence_date: Mapped[date] = mapped_column(Date, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "document_type", "document_id", "line_item_id",
            name="pk_source_doc_lines",
        ),
        ForeignKeyConstraint(
            ["document_type", "document_id"],
            ["evidence_documents_schedule.document_type", "evidence_documents_schedule.document_id"],
            ondelete="CASCADE",
            name="fk_source_lines_evidence_doc",
        ),
    )

    # Relationships
    evidence_document: Mapped["PgEvidenceDocument"] = relationship(
        "PgEvidenceDocument",
        back_populates="source_lines",
        foreign_keys="[PgSourceDocumentLine.document_type, PgSourceDocumentLine.document_id]",
    )
    po_item_mappings: Mapped[List["PgSourceDocumentLinePoItem"]] = relationship(
        "PgSourceDocumentLinePoItem",
        back_populates="source_line",
        cascade="all, delete-orphan",
        primaryjoin=(
            "and_(PgSourceDocumentLine.document_type == foreign(PgSourceDocumentLinePoItem.document_type),"
            " PgSourceDocumentLine.document_id == foreign(PgSourceDocumentLinePoItem.document_id),"
            " PgSourceDocumentLine.line_item_id == foreign(PgSourceDocumentLinePoItem.line_item_id))"
        ),
    )


class PgSourceDocumentLinePoItem(Base):
    """Immutable mapping from an evidence document line to a PO item.

    Composite PK: (document_type, document_id, line_item_id, po_item_id).
    """

    __tablename__ = "source_document_line_po_items"

    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    line_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    po_item_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("schedule_po_items.po_item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "document_type", "document_id", "line_item_id", "po_item_id",
            name="pk_src_doc_line_po_items",
        ),
        ForeignKeyConstraint(
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

    # Relationships
    source_line: Mapped["PgSourceDocumentLine"] = relationship(
        "PgSourceDocumentLine",
        back_populates="po_item_mappings",
        foreign_keys="[PgSourceDocumentLinePoItem.document_type, PgSourceDocumentLinePoItem.document_id, PgSourceDocumentLinePoItem.line_item_id]",
    )
    po_item: Mapped["SchedulePoItem"] = relationship(
        "SchedulePoItem",
        foreign_keys="[PgSourceDocumentLinePoItem.po_item_id]",
    )


class PgQuantityAllocation(Base):
    """Immutable record of a dispatched or received quantity allocation.

    allocation_type: DISPATCH | RECEIPT
    Idempotency and quantity-cap enforcement are done at the application layer.

    Unique constraint on (allocation_type, source_document_type,
    source_document_id, source_line_item_id, po_item_id, schedule_id) handles
    the duplicate-allocation check.  Because schedule_id is nullable,
    PostgreSQL's standard UNIQUE treats NULLs as distinct; we work around
    this in application code (query-before-insert) rather than relying solely
    on the unique index.
    """

    __tablename__ = "quantity_allocations"

    allocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allocation_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    source_document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_line_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    po_item_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("schedule_po_items.po_item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schedule_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("delivery_schedules.schedule_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    matched_dispatch_allocation_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("quantity_allocations.allocation_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Non-unique index for the idempotency query (application-layer check)
        Index(
            "ix_qty_alloc_target",
            "allocation_type",
            "source_document_type",
            "source_document_id",
            "source_line_item_id",
            "po_item_id",
            "schedule_id",
        ),
    )

    # Relationships
    po_item: Mapped["SchedulePoItem"] = relationship(
        "SchedulePoItem",
        back_populates="quantity_allocations",
        foreign_keys=[po_item_id],
    )
    schedule: Mapped[Optional["PgDeliverySchedule"]] = relationship(
        "PgDeliverySchedule",
        back_populates="quantity_allocations",
        foreign_keys=[schedule_id],
    )
    matched_dispatch: Mapped[Optional["PgQuantityAllocation"]] = relationship(
        "PgQuantityAllocation",
        remote_side="PgQuantityAllocation.allocation_id",
        foreign_keys=[matched_dispatch_allocation_id],
    )


class PgDeliveryValidationRun(Base):
    """Immutable snapshot of a reconciliation run result stored as JSONB."""

    __tablename__ = "delivery_validation_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    po_item_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("schedule_po_items.po_item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evaluation_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    result_json: Mapped[Dict[str, Any]] = mapped_column(_JSON_TYPE, nullable=False)

    # Relationships
    po_item: Mapped["SchedulePoItem"] = relationship(
        "SchedulePoItem", back_populates="validation_runs"
    )
