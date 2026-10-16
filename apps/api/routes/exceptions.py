from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import uuid

from packages.domain.models import (
    ExceptionCase,
    ExceptionStatus,
    ExceptionType,
    DepartmentRole,
    EvidenceChecklist,
    ReconciliationFinding,
    DocumentMetadata,
    DocumentType,
    LineItem,
)
from packages.reconciliation.engine import ReconciliationEngine

router = APIRouter()
engine = ReconciliationEngine()

# In-memory store for Day 1 prototyping
MOCK_EXCEPTIONS: List[ExceptionCase] = [
    ExceptionCase(
        id="EX-1048",
        po_reference="PO-44819",
        supplier_name="Apex Logistics",
        type=ExceptionType.QUANTITY_OVERBILL,
        status=ExceptionStatus.UNDER_REVIEW,
        owner_department=DepartmentRole.STORES,
        amount_under_review_pkr=Decimal("125000.00"),
        currency="PKR",
        evidence_checklist=EvidenceChecklist(po_present=True, grn_present=True, invoice_present=True),
        findings=[
            ReconciliationFinding(
                finding_id="f-1048-1",
                type=ExceptionType.QUANTITY_OVERBILL,
                severity="CRITICAL",
                line_item_desc="Industrial Gearbox (GB-99)",
                expected_value=Decimal("450"),
                actual_value=Decimal("500"),
                delta_value=Decimal("50"),
                amount_at_risk_pkr=Decimal("125000.00"),
                source_evidence={"po_qty": "500", "grn_qty": "450", "invoice_qty": "500"},
                explanation="50 units invoiced (INV-9901) but not verified on Goods Received Note (GRN-8812). Billed unreceived overpayment risk: Rs. 125,000.",
            )
        ],
    ),
    ExceptionCase(
        id="EX-1049",
        po_reference="PO-44820",
        supplier_name="Indus Paper Co",
        type=ExceptionType.PRICE_VARIANCE,
        status=ExceptionStatus.NEW,
        owner_department=DepartmentRole.FINANCE,
        amount_under_review_pkr=Decimal("38500.00"),
        currency="PKR",
        evidence_checklist=EvidenceChecklist(po_present=True, grn_present=True, invoice_present=True),
        findings=[
            ReconciliationFinding(
                finding_id="f-1049-1",
                type=ExceptionType.PRICE_VARIANCE,
                severity="WARNING",
                line_item_desc="Heavy Bleached Pulp (PULP-02)",
                expected_value=Decimal("350.00"),
                actual_value=Decimal("388.50"),
                delta_value=Decimal("38.50"),
                amount_at_risk_pkr=Decimal("38500.00"),
                source_evidence={"po_rate": "350.00", "inv_rate": "388.50"},
                explanation="Invoiced unit price exceeds contracted PO price by 11.0%. Overbill risk: Rs. 38,500.",
            )
        ],
    ),
]


class ResolveExceptionRequest(BaseModel):
    resolution_state: str  # 'RESOLVED - CREDIT NOTE', 'RESOLVED - ACCEPTED', 'RESOLVED - RE-INVOICED', 'REJECTED'
    resolution_note: str
    credit_note_ref: Optional[str] = None
    adjusted_amount_pkr: Optional[Decimal] = None


class AddCommentRequest(BaseModel):
    author_id: str
    author_role: DepartmentRole
    content: str
    attachment_filename: Optional[str] = None


@router.get("", response_model=List[ExceptionCase])
def list_exceptions(
    department: Optional[DepartmentRole] = Query(None, description="Filter by owner department"),
    status: Optional[ExceptionStatus] = Query(None, description="Filter by exception status"),
):
    results = MOCK_EXCEPTIONS
    if department:
        results = [e for e in results if e.owner_department == department]
    if status:
        results = [e for e in results if e.status == status]
    return results


@router.get("/{exception_id}", response_model=ExceptionCase)
def get_exception(exception_id: str):
    case = next((e for e in MOCK_EXCEPTIONS if e.id.upper() == exception_id.upper()), None)
    if not case:
        raise HTTPException(status_code=404, detail=f"Exception Case {exception_id} not found.")
    return case


@router.post("/{exception_id}/resolve", response_model=ExceptionCase)
def resolve_exception(exception_id: str, req: ResolveExceptionRequest):
    case = next((e for e in MOCK_EXCEPTIONS if e.id.upper() == exception_id.upper()), None)
    if not case:
        raise HTTPException(status_code=404, detail=f"Exception Case {exception_id} not found.")

    case.resolution_state = req.resolution_state
    case.resolution_note = req.resolution_note
    case.status = ExceptionStatus.CLOSED if "RESOLVED" in req.resolution_state else ExceptionStatus.REJECTED
    case.version += 1
    return case


@router.post("/reconcile-demo", response_model=Optional[ExceptionCase])
def reconcile_demo_case():
    """
    Executes the deterministic 3-way reconciliation engine on the classic 500/450/500 mismatch.
    """
    po = DocumentMetadata(
        document_id="doc-po-1",
        case_id="case-demo",
        document_type=DocumentType.PURCHASE_ORDER,
        document_number="PO-44819",
        po_reference="PO-44819",
        supplier_name="Apex Logistics",
        total_amount=Decimal("1250000.00"),
        sha256="hash-po-demo",
        s3_key="s3://po-demo.pdf",
        line_items=[
            LineItem(
                item_code="GB-99",
                description="Industrial Gearbox",
                quantity=Decimal("500"),
                unit_price=Decimal("2500.00"),
                line_total=Decimal("1250000.00"),
            )
        ],
    )

    grn = DocumentMetadata(
        document_id="doc-grn-1",
        case_id="case-demo",
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_number="GRN-8812",
        po_reference="PO-44819",
        supplier_name="Apex Logistics",
        total_amount=Decimal("1125000.00"),
        sha256="hash-grn-demo",
        s3_key="s3://grn-demo.pdf",
        line_items=[
            LineItem(
                item_code="GB-99",
                description="Industrial Gearbox",
                quantity=Decimal("450"),
                unit_price=Decimal("2500.00"),
                line_total=Decimal("1125000.00"),
            )
        ],
    )

    inv = DocumentMetadata(
        document_id="doc-inv-1",
        case_id="case-demo",
        document_type=DocumentType.SUPPLIER_INVOICE,
        document_number="INV-9901",
        po_reference="PO-44819",
        supplier_name="Apex Logistics",
        total_amount=Decimal("1250000.00"),
        sha256="hash-inv-demo",
        s3_key="s3://inv-demo.pdf",
        line_items=[
            LineItem(
                item_code="GB-99",
                description="Industrial Gearbox",
                quantity=Decimal("500"),
                unit_price=Decimal("2500.00"),
                line_total=Decimal("1250000.00"),
            )
        ],
    )

    return engine.reconcile_case(po=po, grn=grn, invoice=inv, case_id="EX-1048")
