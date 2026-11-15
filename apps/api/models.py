import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.database import Base


# Cross-dialect JSONB helper: native JSONB on PostgreSQL, JSON on SQLite
JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class Supplier(Base):
    """Supplier master entity."""

    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    code: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    contact_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    purchase_orders: Mapped[List["PurchaseOrder"]] = relationship(
        "PurchaseOrder", back_populates="supplier"
    )
    documents: Mapped[List["Document"]] = relationship(
        "Document", back_populates="supplier"
    )


class PurchaseOrder(Base):
    """Purchase Order transaction entity."""

    __tablename__ = "purchase_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    po_number: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    supplier_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    order_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USD", server_default="USD"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ISSUED", index=True
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
    supplier: Mapped[Optional["Supplier"]] = relationship(
        "Supplier", back_populates="purchase_orders"
    )
    items: Mapped[List["PurchaseOrderItem"]] = relationship(
        "PurchaseOrderItem",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    documents: Mapped[List["Document"]] = relationship(
        "Document", back_populates="purchase_order"
    )
    exceptions: Mapped[List["Exception"]] = relationship(
        "Exception", back_populates="purchase_order"
    )


class PurchaseOrderItem(Base):
    """Line item belonging to a Purchase Order."""

    __tablename__ = "purchase_order_items"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    po_item_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    line_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_code: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    ordered_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="pcs")
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
    )
    total_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
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
    purchase_order: Mapped["PurchaseOrder"] = relationship(
        "PurchaseOrder", back_populates="items"
    )


class Document(Base):
    """Registered raw or ingested document metadata."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # PO, GRN, INVOICE, DELIVERY_CHALLAN
    document_number: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    po_reference: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    purchase_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    supplier_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    s3_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    file_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INGESTED", index=True
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
    purchase_order: Mapped[Optional["PurchaseOrder"]] = relationship(
        "PurchaseOrder", back_populates="documents"
    )
    supplier: Mapped[Optional["Supplier"]] = relationship(
        "Supplier", back_populates="documents"
    )
    extracted_data: Mapped[Optional["ExtractedDocumentData"]] = relationship(
        "ExtractedDocumentData",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
    )
    ocr_runs: Mapped[List["OcrRun"]] = relationship(
        "OcrRun",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="OcrRun.run_number",
    )
    classifications: Mapped[List["DocumentClassification"]] = relationship(
        "DocumentClassification",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentClassification.run_number",
    )
    extractions: Mapped[List["DocumentExtraction"]] = relationship(
        "DocumentExtraction",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentExtraction.run_number",
    )
    verifications: Mapped[List["DocumentVerification"]] = relationship(
        "DocumentVerification",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentVerification.created_at",
    )
    outgoing_relationships: Mapped[List["DocumentRelationship"]] = relationship(
        "DocumentRelationship",
        foreign_keys="DocumentRelationship.source_document_id",
        back_populates="source_document",
        cascade="all, delete-orphan",
    )
    incoming_relationships: Mapped[List["DocumentRelationship"]] = relationship(
        "DocumentRelationship",
        foreign_keys="DocumentRelationship.target_document_id",
        back_populates="target_document",
        cascade="all, delete-orphan",
    )
    match_audits: Mapped[List["DocumentMatchAudit"]] = relationship(
        "DocumentMatchAudit",
        foreign_keys="DocumentMatchAudit.source_document_id",
        back_populates="source_document",
        cascade="all, delete-orphan",
        order_by="DocumentMatchAudit.created_at",
    )
    reconciliation_runs: Mapped[List["ReconciliationRun"]] = relationship(
        "ReconciliationRun",
        foreign_keys="ReconciliationRun.root_document_id",
        back_populates="root_document",
        cascade="all, delete-orphan",
        order_by="ReconciliationRun.created_at.desc()",
    )


class ExtractedDocumentData(Base):
    """Structured fields and extracted line items from OCR/parsers."""

    __tablename__ = "extracted_document_data"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fields: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    line_items: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
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
    document: Mapped["Document"] = relationship(
        "Document", back_populates="extracted_data"
    )


class Exception(Base):
    """Core Exception case tracking discrepancies across PO, GRN, and Invoice."""

    __tablename__ = "exceptions"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True
    )  # e.g. "EX-1048"
    purchase_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    po_reference: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    supplier_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # QUANTITY_SHORTAGE, PRICE_VARIANCE, etc.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NEW", index=True
    )  # NEW, UNDER_REVIEW, etc.
    owner_department: Mapped[str] = mapped_column(
        String(32), nullable=False, default="FINANCE", index=True
    )  # STORES, FINANCE, PROCUREMENT, MANAGEMENT
    assigned_user_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    amount_under_review: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USD", server_default="USD"
    )
    po_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    grn_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invoice_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    resolution_state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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
    purchase_order: Mapped[Optional["PurchaseOrder"]] = relationship(
        "PurchaseOrder", back_populates="exceptions"
    )
    history: Mapped[List["ExceptionHistory"]] = relationship(
        "ExceptionHistory",
        back_populates="exception",
        order_by="ExceptionHistory.created_at",
    )


# Alias for callers who prefer ExceptionRecord to avoid shadowing built-in Exception
ExceptionRecord = Exception


class ExceptionHistory(Base):
    """Audit and resolution history log for an exception case."""

    __tablename__ = "exception_history"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    exception_id: Mapped[Optional[str]] = mapped_column(
        String(32),
        ForeignKey("exceptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    exception: Mapped["Exception"] = relationship(
        "Exception", back_populates="history"
    )


class OcrRun(Base):
    """Historical and versioned OCR extraction run for a document."""

    __tablename__ = "ocr_runs"

    __table_args__ = (
        UniqueConstraint("document_id", "run_number", name="uq_ocr_runs_doc_run_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", index=True
    )  # PENDING, PROCESSING, COMPLETED, FAILED
    ocr_engine: Mapped[str] = mapped_column(String(64), nullable=False, default="PaddleOCR")
    ocr_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    full_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="ocr_runs")
    pages: Mapped[List["OcrPageResult"]] = relationship(
        "OcrPageResult",
        back_populates="ocr_run",
        cascade="all, delete-orphan",
        order_by="OcrPageResult.page_number",
    )


class OcrPageResult(Base):
    """Extracted text and metadata for a single page of an OCR run."""

    __tablename__ = "ocr_page_results"

    __table_args__ = (
        UniqueConstraint("ocr_run_id", "page_number", name="uq_ocr_page_results_run_page"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ocr_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("ocr_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text_blocks: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    ocr_run: Mapped["OcrRun"] = relationship("OcrRun", back_populates="pages")


class DocumentClassification(Base):
    """Historical classification result and human-review record for a document."""

    __tablename__ = "document_classifications"

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "run_number",
            name="uq_document_classifications_run",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    predicted_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    probabilities: Mapped[Optional[Dict[str, float]]] = mapped_column(JSON_TYPE, nullable=True)
    needs_human_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    review_reasons: Mapped[Optional[List[str]]] = mapped_column(JSON_TYPE, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="COMPLETED", index=True
    )  # COMPLETED, FAILED, CONFIRMED, REVISED
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    document: Mapped["Document"] = relationship(
        "Document", back_populates="classifications"
    )


class DocumentExtraction(Base):
    """Historical and versioned structured data extraction for a document."""

    __tablename__ = "document_extractions"

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "run_number",
            name="uq_document_extractions_run",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ocr_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("ocr_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="COMPLETED", index=True
    )  # COMPLETED, FAILED, REVIEW_REQUIRED
    fields: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    line_items: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    field_provenance: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    validation_warnings: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    needs_human_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    document: Mapped["Document"] = relationship(
        "Document", back_populates="extractions"
    )
    ocr_run: Mapped[Optional["OcrRun"]] = relationship("OcrRun")


class DocumentVerification(Base):
    """Human verification and review lifecycle session for a document extraction."""

    __tablename__ = "document_verifications"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_extractions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING_REVIEW", index=True
    )  # PENDING_REVIEW, IN_REVIEW, APPROVED, REJECTED, RESUBMITTED
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )  # Optimistic concurrency lock
    verified_fields: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    verified_line_items: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    assigned_reviewer_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    document: Mapped["Document"] = relationship(
        "Document", back_populates="verifications"
    )
    extraction: Mapped["DocumentExtraction"] = relationship("DocumentExtraction")
    corrections: Mapped[List["VerificationCorrection"]] = relationship(
        "VerificationCorrection",
        back_populates="verification",
        cascade="all, delete-orphan",
        order_by="VerificationCorrection.created_at",
    )


class VerificationCorrection(Base):
    """Immutable field-level or line-item audit record of a human correction."""

    __tablename__ = "verification_corrections"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    verification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_verifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    correction_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # FIELD, LINE_ITEM_UPDATE, LINE_ITEM_ADD, LINE_ITEM_DELETE
    field_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    line_item_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    original_value: Mapped[Optional[Any]] = mapped_column(JSON_TYPE, nullable=True)
    corrected_value: Mapped[Optional[Any]] = mapped_column(JSON_TYPE, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    corrected_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    verification: Mapped["DocumentVerification"] = relationship(
        "DocumentVerification", back_populates="corrections"
    )
    document: Mapped["Document"] = relationship("Document")


class DocumentRelationship(Base):
    """Explicit, verifiable relationship between two procurement documents."""

    __tablename__ = "document_relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relationship_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # DELIVERY_CHALLAN_TO_PURCHASE_ORDER, GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER, GOODS_RECEIPT_NOTE_TO_DELIVERY_CHALLAN, SUPPLIER_INVOICE_TO_PURCHASE_ORDER
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="CONFIRMED", index=True
    )  # CONFIRMED, FLAGGED_AMBIGUOUS, REJECTED, PROPOSED
    matching_method: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # AUTOMATIC_EXPLICIT_REFERENCE, MANUAL_OVERRIDE, MANUAL_RESOLUTION
    matched_references: Mapped[Optional[Any]] = mapped_column(
        JSON_TYPE, nullable=True
    )  # e.g. {"po_reference": "PO-1001"}
    evidence: Mapped[Optional[Any]] = mapped_column(
        JSON_TYPE, nullable=True
    )  # explainability metadata & candidate counts
    confidence_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )
    created_by: Mapped[str] = mapped_column(
        String(128), nullable=False, default="SYSTEM"
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "source_document_id",
            "target_document_id",
            "relationship_type",
            name="uq_document_relationship",
        ),
        CheckConstraint(
            "source_document_id != target_document_id",
            name="chk_no_self_relationship",
        ),
    )

    # Relationships
    source_document: Mapped["Document"] = relationship(
        "Document",
        foreign_keys="DocumentRelationship.source_document_id",
        back_populates="outgoing_relationships",
    )
    target_document: Mapped["Document"] = relationship(
        "Document",
        foreign_keys="DocumentRelationship.target_document_id",
        back_populates="incoming_relationships",
    )
    audit_logs: Mapped[List["DocumentMatchAudit"]] = relationship(
        "DocumentMatchAudit",
        back_populates="document_relationship",
        cascade="all, delete-orphan",
        order_by="DocumentMatchAudit.created_at",
    )


class DocumentMatchAudit(Base):
    """Audit trail for automatic match evaluations, ambiguous flags, and manual decisions."""

    __tablename__ = "document_match_audits"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    relationship_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_relationships.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # MATCH_CONFIRMED, MATCH_AMBIGUOUS_FLAGGED, MATCH_REJECTED, MANUAL_LINK_CREATED, MANUAL_STATUS_UPDATED, MANUAL_LINK_REMOVED, NO_REFERENCE_FOUND, TARGET_NOT_FOUND, SUPPLIER_MISMATCH
    previous_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    new_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    actor: Mapped[str] = mapped_column(
        String(128), nullable=False, default="SYSTEM"
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence: Mapped[Optional[Any]] = mapped_column(JSON_TYPE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    document_relationship: Mapped[Optional["DocumentRelationship"]] = relationship(
        "DocumentRelationship", back_populates="audit_logs"
    )
    source_document: Mapped["Document"] = relationship(
        "Document", foreign_keys="DocumentMatchAudit.source_document_id", back_populates="match_audits"
    )
    target_document: Mapped[Optional["Document"]] = relationship(
        "Document", foreign_keys="DocumentMatchAudit.target_document_id"
    )


class ReconciliationRun(Base):
    """Persistence for a deterministic 2-way, 3-way, or 4-way reconciliation execution."""

    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    root_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    po_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PASS", index=True
    )  # PASS, WARNING, FAIL, INCOMPLETE, NEEDS_REVIEW
    reconciled_document_ids: Mapped[Optional[List[str]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    created_by: Mapped[str] = mapped_column(
        String(128), nullable=False, default="SYSTEM"
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
    root_document: Mapped["Document"] = relationship(
        "Document",
        foreign_keys="ReconciliationRun.root_document_id",
        back_populates="reconciliation_runs",
    )
    po_document: Mapped[Optional["Document"]] = relationship(
        "Document",
        foreign_keys="ReconciliationRun.po_document_id",
    )
    discrepancies: Mapped[List["ReconciliationDiscrepancy"]] = relationship(
        "ReconciliationDiscrepancy",
        back_populates="reconciliation_run",
        cascade="all, delete-orphan",
        order_by="ReconciliationDiscrepancy.created_at.asc()",
    )


class ReconciliationDiscrepancy(Base):
    """Detailed finding or discrepancy identified during document reconciliation."""

    __tablename__ = "reconciliation_discrepancies"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    discrepancy_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # OVER_BILLED_QUANTITY, UNDER_DELIVERED, REJECTED_GOODS_BILLED, OVER_DELIVERED, PARTIAL_DELIVERY, PRICE_VARIANCE, CALCULATION_DISCREPANCY, TAX_DISCREPANCY, TOTAL_MISMATCH, MISSING_PO, MISSING_GRN, MISSING_DC, UNMATCHED_LINE_ITEM, SUPPLIER_MISMATCH, NON_USD_CURRENCY
    severity: Mapped[str] = mapped_column(
        String(32), nullable=False, default="CRITICAL", index=True
    )  # CRITICAL, WARNING, INFO
    item_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    line_item_description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    expected_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    actual_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    delta_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    amount_at_risk: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
    )
    source_evidence: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    reconciliation_run: Mapped["ReconciliationRun"] = relationship(
        "ReconciliationRun", back_populates="discrepancies"
    )


class ReconciliationException(Base):
    """Persistent exception created from a reconciliation discrepancy."""

    __tablename__ = "reconciliation_exceptions"
    __table_args__ = (
        UniqueConstraint(
            "discrepancy_id",
            name="uq_reconciliation_exceptions_discrepancy_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    discrepancy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reconciliation_discrepancies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exception_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    supplier_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    po_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    po_reference: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    related_document_ids: Mapped[Optional[List[str]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    financial_impact: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USD", server_default="USD"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="OPEN", server_default="OPEN", index=True
    )
    item_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    line_item_description: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    source_evidence: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolution_supporting_information: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str] = mapped_column(
        String(128), nullable=False, default="SYSTEM", server_default="SYSTEM"
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
    discrepancy: Mapped["ReconciliationDiscrepancy"] = relationship(
        "ReconciliationDiscrepancy"
    )
    reconciliation_run: Mapped["ReconciliationRun"] = relationship(
        "ReconciliationRun"
    )
    history: Mapped[List["ReconciliationExceptionHistory"]] = relationship(
        "ReconciliationExceptionHistory",
        back_populates="exception",
        cascade="all, delete-orphan",
        order_by="ReconciliationExceptionHistory.created_at",
    )


class ReconciliationExceptionHistory(Base):
    """Immutable audit trail for reconciliation exception lifecycle changes."""

    __tablename__ = "reconciliation_exception_history"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    exception_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reconciliation_exceptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default="SYSTEM", server_default="SYSTEM"
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    exception: Mapped["ReconciliationException"] = relationship(
        "ReconciliationException", back_populates="history"
    )

