from decimal import Decimal
from datetime import date, datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    PURCHASE_ORDER = "PO"
    DELIVERY_CHALLAN = "DELIVERY_CHALLAN"
    GOODS_RECEIVED_NOTE = "GRN"
    SUPPLIER_INVOICE = "INVOICE"
    UNKNOWN = "UNKNOWN"


class DeliveryScheduleStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"


class ScheduleAmendmentStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class QuantityAllocationType(str, Enum):
    DISPATCH = "DISPATCH"
    RECEIPT = "RECEIPT"


class DeliveryFindingType(str, Enum):
    MISSING_DELIVERY_SCHEDULE = "MISSING_DELIVERY_SCHEDULE"
    OVERDUE_DELIVERY = "OVERDUE_DELIVERY"
    LATE_DELIVERY = "LATE_DELIVERY"
    EARLY_DELIVERY = "EARLY_DELIVERY"
    DISPATCH_NOT_RECEIVED = "DISPATCH_NOT_RECEIVED"
    RECEIPT_WITHOUT_CHALLAN = "RECEIPT_WITHOUT_CHALLAN"
    OVER_DELIVERY = "OVER_DELIVERY"
    UNSCHEDULED_EVIDENCE = "UNSCHEDULED_EVIDENCE"


class ExceptionStatus(str, Enum):
    NEW = "NEW"
    UNDER_REVIEW = "UNDER_REVIEW"
    PENDING_EVIDENCE = "PENDING_EVIDENCE"
    RESOLVED_ACCEPTED = "RESOLVED - ACCEPTED"
    RESOLVED_CREDIT_NOTE = "RESOLVED - CREDIT NOTE"
    RESOLVED_RE_INVOICED = "RESOLVED - RE-INVOICED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


class ExceptionType(str, Enum):
    QUANTITY_SHORTAGE = "QUANTITY_SHORTAGE"
    QUANTITY_OVERBILL = "QUANTITY_OVERBILL"
    PRICE_VARIANCE = "PRICE_VARIANCE"
    MISSING_GRN = "MISSING_GRN"
    MISSING_PO = "MISSING_PO"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    CALCULATION_MISMATCH = "CALCULATION_MISMATCH"


class DepartmentRole(str, Enum):
    PROCUREMENT = "PROCUREMENT"
    STORES = "STORES"
    FINANCE = "FINANCE"
    MANAGEMENT = "MANAGEMENT"


class BoundingBox(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class LineItem(BaseModel):
    item_code: Optional[str] = None
    description: str
    quantity: Decimal
    unit: str = "pcs"
    unit_price: Decimal
    line_total: Decimal
    bbox: Optional[BoundingBox] = None


class PurchaseOrderItem(BaseModel):
    po_item_id: str
    po_reference: str
    line_item_id: str
    item_code: Optional[str] = None
    description: str
    ordered_quantity: Decimal = Field(gt=0)
    unit: str = "pcs"


class ScheduleAmendment(BaseModel):
    amendment_id: str
    schedule_id: str
    previous_quantity: Decimal = Field(gt=0)
    previous_expected_date: date
    proposed_quantity: Decimal = Field(gt=0)
    proposed_expected_date: date
    status: ScheduleAmendmentStatus = ScheduleAmendmentStatus.PENDING_APPROVAL
    requested_by: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


class DeliverySchedule(BaseModel):
    schedule_id: str
    po_item_id: str
    planned_quantity: Decimal = Field(gt=0)
    expected_delivery_date: date
    status: DeliveryScheduleStatus = DeliveryScheduleStatus.SCHEDULED
    amendment_history: List[ScheduleAmendment] = Field(default_factory=list)


class EvidenceDocumentLine(BaseModel):
    line_item_id: str
    quantity: Decimal = Field(gt=0)
    evidence_date: date
    po_item_ids: List[str] = Field(min_length=1)


class EvidenceDocument(BaseModel):
    document_id: str
    document_type: DocumentType
    document_number: str
    po_reference: str
    sha256: str
    lines: List[EvidenceDocumentLine] = Field(min_length=1)


class QuantityAllocation(BaseModel):
    allocation_id: str
    allocation_type: QuantityAllocationType
    source_document_type: DocumentType
    source_document_id: str
    source_line_item_id: str
    source_line_quantity: Decimal = Field(gt=0)
    evidence_date: date
    po_item_id: str
    schedule_id: Optional[str] = None
    quantity: Decimal = Field(gt=0)
    matched_dispatch_allocation_id: Optional[str] = None


class DeliveryReconciliationFinding(BaseModel):
    type: DeliveryFindingType
    po_item_id: str
    schedule_id: Optional[str] = None
    quantity: Decimal
    explanation: str


class DeliveryReconciliationSummary(BaseModel):
    po_item_id: str
    evaluation_date: date
    ordered_quantity: Decimal
    scheduled_quantity: Decimal
    quantity_due_by_evaluation_date: Decimal
    dispatched_quantity: Decimal
    accepted_quantity: Decimal
    outstanding_quantity: Decimal
    overdue_quantity: Decimal
    dispatched_not_received_quantity: Decimal
    unscheduled_dispatch_quantity: Decimal = Decimal("0")
    unscheduled_accepted_quantity: Decimal = Decimal("0")


class DeliveryValidationRun(BaseModel):
    run_id: str
    po_item_id: str
    evaluation_date: date
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    summary: DeliveryReconciliationSummary
    findings: List[DeliveryReconciliationFinding] = Field(default_factory=list)


class DocumentMetadata(BaseModel):
    document_id: str
    case_id: str
    document_type: DocumentType
    document_number: str
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    document_date: Optional[str] = None
    currency: str = "PKR"
    total_amount: Decimal
    line_items: List[LineItem] = Field(default_factory=list)
    sha256: str
    s3_key: str
    confidence_score: float = 1.0


class ReconciliationFinding(BaseModel):
    finding_id: str
    type: ExceptionType
    severity: str  # 'CRITICAL', 'WARNING', 'INFO'
    line_item_desc: Optional[str] = None
    expected_value: Decimal
    actual_value: Decimal
    delta_value: Decimal
    amount_at_risk_pkr: Decimal
    source_evidence: Dict[str, Any]
    explanation: str


class EvidenceChecklist(BaseModel):
    po_present: bool = False
    grn_present: bool = False
    invoice_present: bool = False


class ExceptionCase(BaseModel):
    id: str  # e.g. "EX-1048"
    po_reference: str
    supplier_name: str
    type: ExceptionType
    status: ExceptionStatus = ExceptionStatus.NEW
    owner_department: DepartmentRole
    assigned_user_id: Optional[str] = None
    amount_under_review_pkr: Decimal
    currency: str = "PKR"
    evidence_checklist: EvidenceChecklist
    findings: List[ReconciliationFinding] = Field(default_factory=list)
    resolution_state: Optional[str] = None
    resolution_note: Optional[str] = None
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditEvent(BaseModel):
    event_id: str
    case_id: Optional[str] = None
    actor_id: str
    actor_role: DepartmentRole
    action: str
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    hash_prev: Optional[str] = None
    hash_curr: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
