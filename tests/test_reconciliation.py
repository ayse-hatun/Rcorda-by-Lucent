from decimal import Decimal
import pytest
import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from packages.domain.models import (
    DocumentMetadata,
    DocumentType,
    LineItem,
    ExceptionType,
    DepartmentRole,
    ExceptionStatus,
)
from packages.reconciliation.engine import ReconciliationEngine, ToleranceConfig


def test_deck_example_500_450_500():
    """
    Test the fundamental deck scenario:
    PO = 500 boxes @ Rs. 100
    GRN = 450 boxes received
    Invoice = 500 boxes billed @ Rs. 100
    Result: 50 units overbilled (Rs. 5,000 at risk) -> Assigned to Finance/Stores
    """
    engine = ReconciliationEngine()

    po = DocumentMetadata(
        document_id="doc-po-1",
        case_id="case-1",
        document_type=DocumentType.PURCHASE_ORDER,
        document_number="PO-44819",
        po_reference="PO-44819",
        supplier_name="Apex Logistics",
        total_amount=Decimal("50000.00"),
        sha256="hash-po",
        s3_key="s3://po.pdf",
        line_items=[
            LineItem(
                item_code="BOX-10",
                description="Heavy Duty Boxes",
                quantity=Decimal("500"),
                unit="boxes",
                unit_price=Decimal("100.00"),
                line_total=Decimal("50000.00"),
            )
        ],
    )

    grn = DocumentMetadata(
        document_id="doc-grn-1",
        case_id="case-1",
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_number="GRN-8812",
        po_reference="PO-44819",
        supplier_name="Apex Logistics",
        total_amount=Decimal("45000.00"),
        sha256="hash-grn",
        s3_key="s3://grn.pdf",
        line_items=[
            LineItem(
                item_code="BOX-10",
                description="Heavy Duty Boxes",
                quantity=Decimal("450"),
                unit="boxes",
                unit_price=Decimal("100.00"),
                line_total=Decimal("45000.00"),
            )
        ],
    )

    invoice = DocumentMetadata(
        document_id="doc-inv-1",
        case_id="case-1",
        document_type=DocumentType.SUPPLIER_INVOICE,
        document_number="INV-9901",
        po_reference="PO-44819",
        supplier_name="Apex Logistics",
        total_amount=Decimal("50000.00"),
        sha256="hash-inv",
        s3_key="s3://inv.pdf",
        line_items=[
            LineItem(
                item_code="BOX-10",
                description="Heavy Duty Boxes",
                quantity=Decimal("500"),
                unit="boxes",
                unit_price=Decimal("100.00"),
                line_total=Decimal("50000.00"),
            )
        ],
    )

    case = engine.reconcile_case(po=po, grn=grn, invoice=invoice, case_id="EX-1048")

    assert case is not None
    assert case.id == "EX-1048"
    assert case.status == ExceptionStatus.NEW
    assert case.evidence_checklist.po_present is True
    assert case.evidence_checklist.grn_present is True
    assert case.evidence_checklist.invoice_present is True

    # Check findings
    findings = case.findings
    assert len(findings) == 2  # 1 short delivery (50 units) + 1 overbill unreceived (50 units)

    overbill = next(f for f in findings if f.type == ExceptionType.QUANTITY_OVERBILL)
    assert overbill.delta_value == Decimal("50")
    assert overbill.amount_at_risk_pkr == Decimal("5000.00")
    assert "Rs. 5,000.00" in overbill.explanation

    shortage = next(f for f in findings if f.type == ExceptionType.QUANTITY_SHORTAGE)
    assert shortage.delta_value == Decimal("50")
    assert shortage.amount_at_risk_pkr == Decimal("5000.00")


def test_clean_transaction_returns_none():
    """
    When PO, GRN, and Invoice all match 100%, no exception case should be created.
    """
    engine = ReconciliationEngine()

    line = LineItem(
        item_code="BOLT-01",
        description="Steel Bolts",
        quantity=Decimal("100"),
        unit_price=Decimal("10.00"),
        line_total=Decimal("1000.00"),
    )

    po = DocumentMetadata(
        document_id="p1", case_id="c1", document_type=DocumentType.PURCHASE_ORDER,
        document_number="PO-1", po_reference="PO-1", supplier_name="Pak Steel",
        total_amount=Decimal("1000.00"), sha256="h1", s3_key="s1",
        line_items=[line],
    )
    grn = DocumentMetadata(
        document_id="g1", case_id="c1", document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_number="GRN-1", po_reference="PO-1", supplier_name="Pak Steel",
        total_amount=Decimal("1000.00"), sha256="h2", s3_key="s2",
        line_items=[line],
    )
    invoice = DocumentMetadata(
        document_id="i1", case_id="c1", document_type=DocumentType.SUPPLIER_INVOICE,
        document_number="INV-1", po_reference="PO-1", supplier_name="Pak Steel",
        total_amount=Decimal("1000.00"), sha256="h3", s3_key="s3",
        line_items=[line],
    )

    case = engine.reconcile_case(po=po, grn=grn, invoice=invoice)
    assert case is None


def test_price_variance_outside_tolerance():
    """
    When invoice unit price exceeds PO rate beyond tolerance, flag price variance.
    """
    # 2% tolerance
    engine = ReconciliationEngine(tolerance=ToleranceConfig(price_tolerance_pct=Decimal("2.0")))

    po = DocumentMetadata(
        document_id="p1", case_id="c1", document_type=DocumentType.PURCHASE_ORDER,
        document_number="PO-1", po_reference="PO-1", supplier_name="Fasten Corp",
        total_amount=Decimal("10000.00"), sha256="h1", s3_key="s1",
        line_items=[
            LineItem(
                item_code="SKU-99", description="Component X",
                quantity=Decimal("100"), unit_price=Decimal("100.00"), line_total=Decimal("10000.00")
            )
        ],
    )
    grn = DocumentMetadata(
        document_id="g1", case_id="c1", document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_number="GRN-1", po_reference="PO-1", supplier_name="Fasten Corp",
        total_amount=Decimal("10000.00"), sha256="h2", s3_key="s2",
        line_items=[
            LineItem(
                item_code="SKU-99", description="Component X",
                quantity=Decimal("100"), unit_price=Decimal("100.00"), line_total=Decimal("10000.00")
            )
        ],
    )
    # Invoiced at Rs. 110 (+10% over PO)
    invoice = DocumentMetadata(
        document_id="i1", case_id="c1", document_type=DocumentType.SUPPLIER_INVOICE,
        document_number="INV-1", po_reference="PO-1", supplier_name="Fasten Corp",
        total_amount=Decimal("11000.00"), sha256="h3", s3_key="s3",
        line_items=[
            LineItem(
                item_code="SKU-99", description="Component X",
                quantity=Decimal("100"), unit_price=Decimal("110.00"), line_total=Decimal("11000.00")
            )
        ],
    )

    case = engine.reconcile_case(po=po, grn=grn, invoice=invoice)
    assert case is not None
    assert case.type == ExceptionType.PRICE_VARIANCE
    assert case.owner_department == DepartmentRole.FINANCE
    # Difference = Rs. 10 * 100 units = Rs. 1,000 at risk
    assert case.amount_under_review_pkr == Decimal("1000.00")


def test_missing_goods_received_note():
    """
    Invoice received with PO, but no Goods Received Note in Stores.
    Should be flagged as MISSING_GRN and routed to Stores.
    """
    engine = ReconciliationEngine()

    po = DocumentMetadata(
        document_id="p1", case_id="c1", document_type=DocumentType.PURCHASE_ORDER,
        document_number="PO-200", po_reference="PO-200", supplier_name="Paper Mills",
        total_amount=Decimal("75000.00"), sha256="h1", s3_key="s1",
        line_items=[
            LineItem(
                item_code="PAPER-A4", description="A4 Reams",
                quantity=Decimal("50"), unit_price=Decimal("1500.00"), line_total=Decimal("75000.00")
            )
        ],
    )
    invoice = DocumentMetadata(
        document_id="i1", case_id="c1", document_type=DocumentType.SUPPLIER_INVOICE,
        document_number="INV-500", po_reference="PO-200", supplier_name="Paper Mills",
        total_amount=Decimal("75000.00"), sha256="h3", s3_key="s3",
        line_items=[
            LineItem(
                item_code="PAPER-A4", description="A4 Reams",
                quantity=Decimal("50"), unit_price=Decimal("1500.00"), line_total=Decimal("75000.00")
            )
        ],
    )

    case = engine.reconcile_case(po=po, grn=None, invoice=invoice)
    assert case is not None
    assert case.type == ExceptionType.MISSING_GRN
    assert case.owner_department == DepartmentRole.STORES
    assert case.amount_under_review_pkr == Decimal("75000.00")
    assert case.evidence_checklist.grn_present is False
