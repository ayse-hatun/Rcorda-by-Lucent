from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SupplierCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=64)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_phone: Optional[str] = Field(None, max_length=64)
    address: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Supplier name cannot be empty or whitespace only")
        return s

    @field_validator("code")
    @classmethod
    def strip_code(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            s = v.strip()
            return s if s else None
        return None


class SupplierUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    code: Optional[str] = Field(None, max_length=64)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_phone: Optional[str] = Field(None, max_length=64)
    address: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            s = v.strip()
            if not s:
                raise ValueError("Supplier name cannot be empty or whitespace only")
            return s
        return v

    @field_validator("code")
    @classmethod
    def strip_code(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            s = v.strip()
            return s if s else None
        return None


class SupplierResponse(BaseModel):
    id: uuid.UUID
    name: str
    code: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    address: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)






class PurchaseOrderItemCreate(BaseModel):
    line_item_id: str = Field(..., min_length=1, max_length=64)
    item_code: Optional[str] = Field(None, max_length=64)
    description: str = Field(..., min_length=1)
    ordered_quantity: Decimal = Field(..., gt=0, description="Quantity must be positive")
    unit: str = Field(default="pcs", min_length=1, max_length=32)
    unit_price: Decimal = Field(default=Decimal("0.0000"), ge=0, description="Unit price must be non-negative")
    total_price: Optional[Decimal] = Field(None, ge=0, description="Total price must be non-negative if specified")
    po_item_id: Optional[str] = Field(None, max_length=64)

    @field_validator("line_item_id", "description")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Field cannot be empty or whitespace only")
        return s


class PurchaseOrderItemResponse(BaseModel):
    id: uuid.UUID
    purchase_order_id: uuid.UUID
    po_item_id: Optional[str] = None
    line_item_id: str
    item_code: Optional[str] = None
    description: str
    ordered_quantity: Decimal
    unit: str
    unit_price: Decimal
    total_price: Decimal
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)






SUPPORTED_CURRENCIES = {"USD"}
DEFAULT_CURRENCY = "USD"


class PurchaseOrderCreate(BaseModel):
    po_number: str = Field(..., min_length=1, max_length=128)
    supplier_id: Optional[uuid.UUID] = None
    order_date: Optional[date] = None
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=8)
    status: str = Field(default="ISSUED", max_length=32)
    items: List[PurchaseOrderItemCreate] = Field(
        ..., min_length=1, description="Purchase order must contain at least one line item"
    )

    @field_validator("po_number")
    @classmethod
    def validate_po_number(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("po_number cannot be empty or whitespace only")
        return s

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        s = v.strip().upper()
        if s not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Currency '{s}' is not supported in MVP. Supported currencies: {', '.join(sorted(SUPPORTED_CURRENCIES))}"
            )
        return s


class PurchaseOrderUpdate(BaseModel):
    po_number: Optional[str] = Field(None, min_length=1, max_length=128)
    supplier_id: Optional[uuid.UUID] = None
    order_date: Optional[date] = None
    currency: Optional[str] = Field(None, min_length=3, max_length=8)
    status: Optional[str] = Field(None, max_length=32)

    @field_validator("po_number")
    @classmethod
    def validate_po_number(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            s = v.strip()
            if not s:
                raise ValueError("po_number cannot be empty or whitespace only")
            return s
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            s = v.strip().upper()
            if s not in SUPPORTED_CURRENCIES:
                raise ValueError(
                    f"Currency '{s}' is not supported in MVP. Supported currencies: {', '.join(sorted(SUPPORTED_CURRENCIES))}"
                )
            return s
        return v


class PurchaseOrderResponse(BaseModel):
    id: uuid.UUID
    po_number: str
    supplier_id: Optional[uuid.UUID] = None
    order_date: Optional[date] = None
    total_amount: Decimal
    currency: str
    status: str
    created_at: datetime
    updated_at: datetime
    supplier: Optional[SupplierResponse] = None
    items: Optional[List[PurchaseOrderItemResponse]] = None

    model_config = ConfigDict(from_attributes=True)






SUPPORTED_DOCUMENT_TYPES = {
    "PURCHASE_ORDER",
    "DELIVERY_CHALLAN",
    "GOODS_RECEIPT_NOTE",
    "SUPPLIER_INVOICE",
}

DOCUMENT_TYPE_ALIASES = {
    "PO": "PURCHASE_ORDER",
    "GRN": "GOODS_RECEIPT_NOTE",
    "INVOICE": "SUPPLIER_INVOICE",
}


def normalize_document_type(doc_type: str) -> str:
    cleaned = doc_type.strip().upper()
    if cleaned in DOCUMENT_TYPE_ALIASES:
        return DOCUMENT_TYPE_ALIASES[cleaned]
    if cleaned in SUPPORTED_DOCUMENT_TYPES:
        return cleaned
    raise ValueError(
        f"Unsupported document type: '{doc_type}'. Supported types: {', '.join(sorted(SUPPORTED_DOCUMENT_TYPES))}"
    )


class DocumentResponse(BaseModel):
    id: uuid.UUID
    document_type: str
    document_number: Optional[str] = None
    po_reference: Optional[str] = None
    purchase_order_id: Optional[uuid.UUID] = None
    supplier_id: Optional[uuid.UUID] = None
    sha256: str
    s3_key: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)






class OcrPageResponse(BaseModel):
    id: uuid.UUID
    page_number: int
    raw_text: str
    text_blocks: Optional[List[Dict[str, Any]]] = None
    confidence_score: Optional[float] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)






class OcrRunResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    run_number: int
    status: str
    ocr_engine: str
    ocr_version: Optional[str] = None
    total_pages: int
    full_text: Optional[str] = None
    confidence_score: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    pages: Optional[List[OcrPageResponse]] = None

    model_config = ConfigDict(from_attributes=True)





OcrPageResponse.model_rebuild()
OcrRunResponse.model_rebuild()


class ClassificationResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    run_number: int
    predicted_type: str
    confidence_score: float
    probabilities: Optional[Dict[str, float]] = None
    needs_human_review: bool
    review_reasons: Optional[List[str]] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    actual_type: Optional[str] = None
    status: str
    model_name: str
    model_version: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)





class ClassifyDocumentRequest(BaseModel):
    raw_text: Optional[str] = None
    actor: Optional[str] = None


class HumanReviewRequest(BaseModel):
    actual_type: str = Field(..., min_length=1, max_length=32)
    reviewer_id: str = Field(..., min_length=1, max_length=128)
    notes: Optional[str] = None


class ExtractDocumentRequest(BaseModel):
    ocr_run_id: Optional[uuid.UUID] = None
    document_type: Optional[str] = None


class ExtractionResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    ocr_run_id: Optional[uuid.UUID] = None
    run_number: int
    document_type: str
    extractor_version: str
    status: str
    fields: Optional[Dict[str, Any]] = None
    line_items: Optional[List[Dict[str, Any]]] = None
    field_provenance: Optional[Dict[str, Any]] = None
    validation_warnings: Optional[List[Dict[str, Any]]] = None
    needs_human_review: bool
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class VerificationResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    extraction_id: uuid.UUID
    status: str
    version: int
    verified_fields: Optional[Dict[str, Any]] = None
    verified_line_items: Optional[List[Dict[str, Any]]] = None
    assigned_reviewer_id: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CorrectionResponse(BaseModel):
    id: uuid.UUID
    verification_id: uuid.UUID
    document_id: uuid.UUID
    correction_type: str
    field_name: Optional[str] = None
    line_item_index: Optional[int] = None
    original_value: Optional[Any] = None
    corrected_value: Optional[Any] = None
    reason: Optional[str] = None
    corrected_by: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ClaimReviewRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)


class ReleaseReviewRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)


class CorrectFieldRequest(BaseModel):
    field_name: str = Field(..., min_length=1, max_length=128)
    corrected_value: Any
    corrected_by: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)
    reason: Optional[str] = None


class CorrectLineItemRequest(BaseModel):
    line_item_index: int = Field(..., ge=0)
    item_updates: Dict[str, Any] = Field(..., min_length=1)
    corrected_by: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)
    reason: Optional[str] = None


class AddLineItemRequest(BaseModel):
    line_item: Dict[str, Any] = Field(..., min_length=1)
    corrected_by: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)
    reason: Optional[str] = None


class DeleteLineItemRequest(BaseModel):
    line_item_index: int = Field(..., ge=0)
    corrected_by: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)
    reason: Optional[str] = None


class ApproveVerificationRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)
    notes: Optional[str] = None


class RejectVerificationRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)
    notes: Optional[str] = None


class ResubmitVerificationRequest(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    expected_version: int = Field(..., ge=1)
    reason: Optional[str] = None


class DocumentRelationshipResponse(BaseModel):
    id: uuid.UUID
    source_document_id: uuid.UUID
    target_document_id: uuid.UUID
    relationship_type: str
    status: str
    matching_method: str
    matched_references: Optional[Dict[str, Any]] = None
    evidence: Optional[Dict[str, Any]] = None
    confidence_score: float
    created_by: str
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentMatchAuditResponse(BaseModel):
    id: uuid.UUID
    relationship_id: Optional[uuid.UUID] = None
    source_document_id: uuid.UUID
    target_document_id: Optional[uuid.UUID] = None
    action: str
    previous_status: Optional[str] = None
    new_status: Optional[str] = None
    actor: str
    reason: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MatchSummaryResponse(BaseModel):
    document_id: uuid.UUID
    document_type: str
    match_status: str
    relationships: List[DocumentRelationshipResponse]
    audits: List[DocumentMatchAuditResponse]
    transaction_chain: Optional[Dict[str, Any]] = None


class DocumentRelationshipsChainResponse(BaseModel):
    document_id: uuid.UUID
    outgoing_relationships: List[DocumentRelationshipResponse]
    incoming_relationships: List[DocumentRelationshipResponse]
    transaction_chain: Dict[str, Any]


class ManualLinkRequest(BaseModel):
    target_document_id: uuid.UUID
    relationship_type: str = Field(..., min_length=1, max_length=64)
    actor: str = Field(..., min_length=1, max_length=128)
    reason: Optional[str] = None


class ResolveRelationshipRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=32)
    actor: str = Field(..., min_length=1, max_length=128)
    reason: Optional[str] = None


class ReconcileTransactionRequest(BaseModel):
    tolerance_price_pct: Optional[Decimal] = Decimal("0.0")
    tolerance_price_usd: Optional[Decimal] = Decimal("0.0")
    tolerance_qty_pct: Optional[Decimal] = Decimal("0.0")
    allow_missing_dc: bool = False
    actor: str = Field(default="SYSTEM", max_length=128)


class ReconciliationDiscrepancyResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    discrepancy_type: str
    severity: str
    item_code: Optional[str] = None
    line_item_description: Optional[str] = None
    expected_value: Optional[Decimal] = None
    actual_value: Optional[Decimal] = None
    delta_value: Optional[Decimal] = None
    currency: str
    amount_at_risk: Decimal
    source_evidence: Optional[Dict[str, Any]] = None
    explanation: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReconciliationRunResponse(BaseModel):
    id: uuid.UUID
    root_document_id: uuid.UUID
    po_document_id: Optional[uuid.UUID] = None
    status: str
    reconciled_document_ids: Optional[List[str]] = None
    summary: Optional[Dict[str, Any]] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    discrepancies: List[ReconciliationDiscrepancyResponse] = []

    model_config = ConfigDict(from_attributes=True)




class ReconciliationExceptionHistoryResponse(BaseModel):
    id: uuid.UUID
    exception_id: uuid.UUID
    action: str
    actor_id: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    note: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReconciliationExceptionResponse(BaseModel):
    id: uuid.UUID
    discrepancy_id: uuid.UUID
    reconciliation_run_id: uuid.UUID
    exception_type: str
    severity: str
    description: str
    supplier_name: Optional[str] = None
    po_document_id: Optional[uuid.UUID] = None
    po_reference: Optional[str] = None
    related_document_ids: Optional[List[str]] = None
    financial_impact: Decimal
    currency: str
    status: str
    item_code: Optional[str] = None
    line_item_description: Optional[str] = None
    source_evidence: Optional[Dict[str, Any]] = None
    resolution_notes: Optional[str] = None
    resolution_supporting_information: Optional[Dict[str, Any]] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    history: List[ReconciliationExceptionHistoryResponse] = []

    model_config = ConfigDict(from_attributes=True)


class CreateReconciliationExceptionRequest(BaseModel):
    discrepancy_id: uuid.UUID
    actor: str = Field(default="SYSTEM", max_length=128)


class CreateReconciliationExceptionsFromRunRequest(BaseModel):
    actor: str = Field(default="SYSTEM", max_length=128)


class UpdateReconciliationExceptionStatusRequest(BaseModel):
    status: str = Field(..., min_length=1, max_length=32)
    actor: str = Field(default="SYSTEM", max_length=128)
    note: Optional[str] = None
    supporting_information: Optional[Dict[str, Any]] = None


class ResolveReconciliationExceptionRequest(BaseModel):
    resolution_notes: str = Field(..., min_length=1)
    supporting_information: Optional[Dict[str, Any]] = None
    actor: str = Field(default="SYSTEM", max_length=128)


class DashboardMetricsResponse(BaseModel):
    total_suppliers: int
    total_purchase_orders: int
    total_documents: int
    documents_awaiting_review: int
    open_exceptions: int
    resolved_exceptions: int
    late_deliveries: int
    partial_deliveries: int
    financial_discrepancies: int


class ReconciliationExceptionListResponse(BaseModel):
    items: List[ReconciliationExceptionResponse]
    total: int
    limit: int
    offset: int


class AIExtractionSnapshot(BaseModel):
    extraction_id: uuid.UUID
    run_number: int
    status: str
    fields: Optional[Dict[str, Any]] = None
    line_items: Optional[List[Dict[str, Any]]] = None
    field_provenance: Optional[Dict[str, Any]] = None
    validation_warnings: Optional[List[Dict[str, Any]]] = None


class HumanVerificationSnapshot(BaseModel):
    verification_id: uuid.UUID
    status: str
    version: int
    verified_fields: Optional[Dict[str, Any]] = None
    verified_line_items: Optional[List[Dict[str, Any]]] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class ReconciliationReportDocument(BaseModel):
    document_id: uuid.UUID
    document_type: str
    document_number: Optional[str] = None
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    status: str
    is_root: bool
    is_purchase_order: bool
    ai_extraction: Optional[AIExtractionSnapshot] = None
    human_verification: Optional[HumanVerificationSnapshot] = None


class ReconciliationReportResponse(BaseModel):
    run_id: uuid.UUID
    root_document_id: uuid.UUID
    po_document_id: Optional[uuid.UUID] = None
    status: str
    summary: Optional[Dict[str, Any]] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    documents: List[ReconciliationReportDocument] = []
    discrepancies: List[ReconciliationDiscrepancyResponse] = []
    financial_impact_by_currency: Dict[str, Decimal] = {}
    total_amount_at_risk: Decimal
