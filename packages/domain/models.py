from decimal import Decimal
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    PURCHASE_ORDER = "PO"
    GOODS_RECEIVED_NOTE = "GRN"
    SUPPLIER_INVOICE = "INVOICE"
    UNKNOWN = "UNKNOWN"


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
