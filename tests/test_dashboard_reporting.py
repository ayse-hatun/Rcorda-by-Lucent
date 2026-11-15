"""Focused tests for dashboard metrics and reconciliation reporting."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.database import get_engine
from apps.api.main import app
from apps.api.models import (
    Document,
    DocumentExtraction,
    DocumentVerification,
    PurchaseOrder,
    ReconciliationDiscrepancy,
    ReconciliationException,
    ReconciliationRun,
    Supplier,
)
from apps.api.schedule_models import (
    PgDeliverySchedule,
    PgEvidenceDocument,
    PgQuantityAllocation,
    PgSourceDocumentLine,
    SchedulePoItem,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _seed_metrics_case() -> dict:
    unique = uuid.uuid4().hex
    supplier = Supplier(name=f"Metrics Supplier {unique[:8]}", code=f"MET-{unique[:16]}")
    purchase_order = PurchaseOrder(
        po_number=f"PO-MET-{unique[:12]}",
        supplier=supplier,
        total_amount=Decimal("1000.0000"),
        currency="USD",
    )
    document = Document(
        document_type="PURCHASE_ORDER",
        document_number=purchase_order.po_number,
        po_reference=purchase_order.po_number,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )
    review_document = Document(
        document_type="SUPPLIER_INVOICE",
        document_number=f"INV-MET-{unique[:12]}",
        po_reference=purchase_order.po_number,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="EXTRACTED",
    )
    extraction = DocumentExtraction(
        document=review_document,
        document_type="SUPPLIER_INVOICE",
        extractor_version="test-v1",
        run_number=1,
        fields={"grand_total": "1000.00"},
        needs_human_review=True,
    )
    verification = DocumentVerification(
        document=review_document,
        extraction=extraction,
        status="PENDING_REVIEW",
        verified_fields={"grand_total": "1000.00"},
    )

    run = ReconciliationRun(
        status="FAIL",
        summary={"discrepancy_count": 2},
        created_by="TEST",
    )
    financial_discrepancy = ReconciliationDiscrepancy(
        discrepancy_type="PRICE_VARIANCE",
        severity="CRITICAL",
        currency="USD",
        amount_at_risk=Decimal("200.0000"),
        explanation="Invoice price exceeds PO price.",
    )
    non_financial_discrepancy = ReconciliationDiscrepancy(
        discrepancy_type="UNMATCHED_LINE_ITEM",
        severity="WARNING",
        currency="USD",
        amount_at_risk=Decimal("0.0000"),
        explanation="Line item could not be matched.",
    )

    po_item = SchedulePoItem(
        po_item_id=f"ITEM-{unique[:12]}",
        po_reference=purchase_order.po_number,
        line_item_id="1",
        description="Test item",
        ordered_quantity=Decimal("10.0000"),
        unit="pcs",
    )
    expected_date = date(2025, 1, 1)
    evidence_document = PgEvidenceDocument(
        document_type="GRN",
        document_id=f"GRN-{unique[:12]}",
        document_number=f"GRN-{unique[:12]}",
        po_reference=purchase_order.po_number,
        sha256=uuid.uuid4().hex,
    )
    source_line = PgSourceDocumentLine(
        document_type="GRN",
        document_id=evidence_document.document_id,
        line_item_id="1",
        line_quantity=Decimal("4.0000"),
        evidence_date=expected_date + timedelta(days=2),
    )
    second_source_line = PgSourceDocumentLine(
        document_type="GRN",
        document_id=evidence_document.document_id,
        line_item_id="2",
        line_quantity=Decimal("3.0000"),
        evidence_date=expected_date + timedelta(days=3),
    )
    schedule = PgDeliverySchedule(
        schedule_id=f"SCH-{unique[:12]}",
        po_item=po_item,
        planned_quantity=Decimal("10.0000"),
        expected_delivery_date=expected_date,
        status="PARTIALLY_FULFILLED",
    )
    allocation = PgQuantityAllocation(
        allocation_id=f"ALLOC-{unique[:12]}",
        allocation_type="RECEIPT",
        source_document_type="GRN",
        source_document_id=evidence_document.document_id,
        source_line_item_id="1",
        po_item=po_item,
        schedule=schedule,
        quantity=Decimal("4.0000"),
    )
    second_allocation = PgQuantityAllocation(
        allocation_id=f"ALLOC2-{unique[:12]}",
        allocation_type="RECEIPT",
        source_document_type="GRN",
        source_document_id=evidence_document.document_id,
        source_line_item_id="2",
        po_item=po_item,
        schedule=schedule,
        quantity=Decimal("3.0000"),
    )

    engine = get_engine()
    with Session(engine) as db:
        db.add_all(
            [
                supplier,
                purchase_order,
                document,
                review_document,
                extraction,
                verification,
                po_item,
                evidence_document,
                source_line,
                second_source_line,
                schedule,
                allocation,
                second_allocation,
            ]
        )
        db.flush()

        run.root_document_id = document.id
        run.po_document_id = document.id
        run.reconciled_document_ids = [
            str(document.id),
            str(review_document.id),
        ]
        db.add(run)
        db.flush()

        financial_discrepancy.run_id = run.id
        non_financial_discrepancy.run_id = run.id
        db.add_all([financial_discrepancy, non_financial_discrepancy])
        db.flush()

        open_exception = ReconciliationException(
            discrepancy_id=financial_discrepancy.id,
            reconciliation_run_id=run.id,
            exception_type=financial_discrepancy.discrepancy_type,
            severity=financial_discrepancy.severity,
            description=financial_discrepancy.explanation,
            supplier_name=supplier.name,
            po_document_id=document.id,
            po_reference=purchase_order.po_number,
            related_document_ids=[str(document.id)],
            financial_impact=financial_discrepancy.amount_at_risk,
            currency="USD",
            status="OPEN",
            created_by="TEST",
        )
        resolved_exception = ReconciliationException(
            discrepancy_id=non_financial_discrepancy.id,
            reconciliation_run_id=run.id,
            exception_type=non_financial_discrepancy.discrepancy_type,
            severity=non_financial_discrepancy.severity,
            description=non_financial_discrepancy.explanation,
            supplier_name=supplier.name,
            po_document_id=document.id,
            po_reference=purchase_order.po_number,
            related_document_ids=[str(document.id)],
            financial_impact=Decimal("0.0000"),
            currency="USD",
            status="RESOLVED",
            created_by="TEST",
        )
        db.add_all([open_exception, resolved_exception])
        db.commit()

        return {
            "supplier_id": supplier.id,
            "purchase_order_id": purchase_order.id,
            "document_id": document.id,
            "review_document_id": review_document.id,
        }


def _seed_exception(
    *,
    exception_type: str,
    severity: str,
    status: str,
    supplier_name: str,
    po_reference: str,
    created_at: datetime,
) -> uuid.UUID:
    unique = uuid.uuid4().hex
    supplier = Supplier(name=supplier_name, code=f"REP-{unique[:16]}")
    document = Document(
        document_type="PURCHASE_ORDER",
        document_number=po_reference,
        po_reference=po_reference,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )
    run = ReconciliationRun(
        status="FAIL",
        summary={"discrepancy_count": 1},
        created_by="TEST",
    )
    discrepancy = ReconciliationDiscrepancy(
        discrepancy_type=exception_type,
        severity=severity,
        currency="USD",
        amount_at_risk=Decimal("50.0000"),
        explanation="Seeded reporting exception.",
    )
    exception = ReconciliationException(
        exception_type=exception_type,
        severity=severity,
        description="Seeded reporting exception.",
        supplier_name=supplier_name,
        po_reference=po_reference,
        financial_impact=Decimal("50.0000"),
        currency="USD",
        status=status,
        created_by="TEST",
        created_at=created_at,
    )

    engine = get_engine()
    with Session(engine) as db:
        db.add_all([supplier, document])
        db.flush()

        run.root_document_id = document.id
        run.po_document_id = document.id
        run.reconciled_document_ids = [str(document.id)]
        db.add(run)
        db.flush()

        discrepancy.run_id = run.id
        db.add(discrepancy)
        db.flush()

        exception.discrepancy_id = discrepancy.id
        exception.reconciliation_run_id = run.id
        exception.po_document_id = document.id
        db.add(exception)
        db.commit()
        return exception.id


def _seed_report_case() -> dict:
    unique = uuid.uuid4().hex
    supplier = Supplier(name=f"Report Supplier {unique[:8]}", code=f"REP-{unique[:16]}")
    po_reference = f"PO-REP-{unique[:12]}"

    purchase_order = Document(
        document_type="PURCHASE_ORDER",
        document_number=po_reference,
        po_reference=po_reference,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )
    delivery_challan = Document(
        document_type="DELIVERY_CHALLAN",
        document_number=f"DC-{unique[:12]}",
        po_reference=po_reference,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )
    grn = Document(
        document_type="GOODS_RECEIPT_NOTE",
        document_number=f"GRN-{unique[:12]}",
        po_reference=po_reference,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )
    invoice = Document(
        document_type="SUPPLIER_INVOICE",
        document_number=f"INV-{unique[:12]}",
        po_reference=po_reference,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )

    ai_extraction = DocumentExtraction(
        document=purchase_order,
        document_type="PURCHASE_ORDER",
        extractor_version="ai-v1",
        run_number=1,
        fields={"po_number": po_reference, "grand_total": "100.00"},
        line_items=[{"item_code": "AI-ITEM", "quantity": "10"}],
    )
    human_verification = DocumentVerification(
        document=purchase_order,
        extraction=ai_extraction,
        status="APPROVED",
        version=2,
        verified_fields={"po_number": po_reference, "grand_total": "95.00"},
        verified_line_items=[{"item_code": "HUMAN-ITEM", "quantity": "9"}],
        reviewed_by="REVIEWER",
        reviewed_at=datetime.now(timezone.utc),
    )

    run = ReconciliationRun(
        status="FAIL",
        summary={
            "discrepancy_count": 1,
            "critical_count": 1,
            "total_amount_at_risk": "125.0000",
        },
        created_by="TEST",
    )
    discrepancy = ReconciliationDiscrepancy(
        discrepancy_type="OVER_BILLED_QUANTITY",
        severity="CRITICAL",
        item_code="ITEM-1",
        line_item_description="Test item",
        expected_value=Decimal("10.0000"),
        actual_value=Decimal("12.5000"),
        delta_value=Decimal("2.5000"),
        currency="USD",
        amount_at_risk=Decimal("125.0000"),
        source_evidence=None,
        explanation="Invoice bills more than accepted.",
    )

    engine = get_engine()
    with Session(engine) as db:
        db.add_all(
            [
                supplier,
                purchase_order,
                delivery_challan,
                grn,
                invoice,
                ai_extraction,
                human_verification,
            ]
        )
        db.flush()

        run.root_document_id = invoice.id
        run.po_document_id = purchase_order.id
        run.reconciled_document_ids = [
            str(purchase_order.id),
            str(delivery_challan.id),
            str(grn.id),
            str(invoice.id),
        ]
        db.add(run)
        db.flush()

        discrepancy.run_id = run.id
        discrepancy.source_evidence = {"document_id": str(invoice.id)}
        db.add(discrepancy)
        db.commit()
        return {
            "run_id": str(run.id),
            "po_id": str(purchase_order.id),
            "po_reference": po_reference,
            "supplier_name": supplier.name,
        }


def test_dashboard_metrics_are_derived_from_persisted_records(client):
    before = client.get("/v1/dashboard/metrics")
    assert before.status_code == 200
    baseline = before.json()

    _seed_metrics_case()

    after = client.get("/v1/dashboard/metrics")
    assert after.status_code == 200
    metrics = after.json()

    assert metrics["total_suppliers"] - baseline["total_suppliers"] == 1
    assert metrics["total_purchase_orders"] - baseline["total_purchase_orders"] == 1
    assert metrics["total_documents"] - baseline["total_documents"] == 2
    assert (
        metrics["documents_awaiting_review"]
        - baseline["documents_awaiting_review"]
        == 1
    )
    assert metrics["open_exceptions"] - baseline["open_exceptions"] == 1
    assert metrics["resolved_exceptions"] - baseline["resolved_exceptions"] == 1
    assert metrics["late_deliveries"] - baseline["late_deliveries"] == 1
    assert metrics["partial_deliveries"] - baseline["partial_deliveries"] == 1
    assert (
        metrics["financial_discrepancies"]
        - baseline["financial_discrepancies"]
        == 1
    )


def test_exception_dashboard_filters_and_paginates(client):
    supplier_name = f"Filter Supplier {uuid.uuid4().hex[:10]}"
    po_reference = f"PO-FILTER-{uuid.uuid4().hex[:12]}"

    _seed_exception(
        exception_type="PRICE_VARIANCE",
        severity="CRITICAL",
        status="OPEN",
        supplier_name=supplier_name,
        po_reference=po_reference,
        created_at=datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc),
    )
    _seed_exception(
        exception_type="MISSING_GRN",
        severity="WARNING",
        status="RESOLVED",
        supplier_name=supplier_name,
        po_reference=po_reference,
        created_at=datetime(2025, 2, 10, 12, 0, tzinfo=timezone.utc),
    )

    all_items = client.get(
        "/v1/dashboard/exceptions",
        params={"supplier": supplier_name, "po_number": po_reference},
    )
    assert all_items.status_code == 200
    body = all_items.json()
    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    by_type = client.get(
        "/v1/dashboard/exceptions",
        params={"supplier": supplier_name, "type": "PRICE_VARIANCE"},
    ).json()
    assert by_type["total"] == 1
    assert by_type["items"][0]["exception_type"] == "PRICE_VARIANCE"

    by_status = client.get(
        "/v1/dashboard/exceptions",
        params={"supplier": supplier_name, "status": "RESOLVED"},
    ).json()
    assert by_status["total"] == 1
    assert by_status["items"][0]["status"] == "RESOLVED"

    by_date = client.get(
        "/v1/dashboard/exceptions",
        params={
            "supplier": supplier_name,
            "date_from": "2025-02-01",
            "date_to": "2025-02-28",
        },
    ).json()
    assert by_date["total"] == 1
    assert by_date["items"][0]["exception_type"] == "MISSING_GRN"

    paged = client.get(
        "/v1/dashboard/exceptions",
        params={"supplier": supplier_name, "limit": 1, "offset": 1},
    ).json()
    assert paged["total"] == 2
    assert len(paged["items"]) == 1


def test_exception_dashboard_date_boundaries_are_inclusive(client):
    supplier_name = f"Boundary Supplier {uuid.uuid4().hex[:10]}"
    po_reference = f"PO-BOUNDARY-{uuid.uuid4().hex[:12]}"

    for created_at in (
        datetime(2025, 3, 10, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 3, 10, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2025, 3, 11, 0, 0, 0, tzinfo=timezone.utc),
    ):
        _seed_exception(
            exception_type="PRICE_VARIANCE",
            severity="CRITICAL",
            status="OPEN",
            supplier_name=supplier_name,
            po_reference=po_reference,
            created_at=created_at,
        )

    body = client.get(
        "/v1/dashboard/exceptions",
        params={
            "supplier": supplier_name,
            "date_from": "2025-03-10",
            "date_to": "2025-03-10",
        },
    ).json()
    assert body["total"] == 2
    assert all(
        item["created_at"].startswith("2025-03-10") for item in body["items"]
    )


def test_reconciliation_report_distinguishes_ai_and_human_values(client):
    case = _seed_report_case()

    response = client.get(
        f"/v1/reports/reconciliation-runs/{case['run_id']}"
    )
    assert response.status_code == 200
    report = response.json()

    assert report["status"] == "FAIL"
    assert report["po_reference"] == case["po_reference"]
    assert report["supplier_name"] == case["supplier_name"]
    assert len(report["documents"]) == 4
    assert len(report["discrepancies"]) == 1
    assert Decimal(
        str(report["financial_impact_by_currency"]["USD"])
    ) == Decimal("125.0000")
    assert Decimal(str(report["total_amount_at_risk"])) == Decimal("125.0000")
    assert report["discrepancies"][0]["source_evidence"]["document_id"]

    po_document = next(
        doc for doc in report["documents"] if doc["document_id"] == case["po_id"]
    )
    assert po_document["is_purchase_order"] is True
    assert po_document["ai_extraction"]["fields"]["grand_total"] == "100.00"
    assert (
        po_document["human_verification"]["verified_fields"]["grand_total"]
        == "95.00"
    )
    assert po_document["ai_extraction"]["line_items"][0]["item_code"] == "AI-ITEM"
    assert (
        po_document["human_verification"]["verified_line_items"][0]["item_code"]
        == "HUMAN-ITEM"
    )

    alias = client.get(
        f"/v1/dashboard/reconciliation-runs/{case['run_id']}"
    )
    assert alias.status_code == 200
    assert alias.json()["run_id"] == case["run_id"]

    missing = client.get(f"/v1/reports/reconciliation-runs/{uuid.uuid4()}")
    assert missing.status_code == 404
