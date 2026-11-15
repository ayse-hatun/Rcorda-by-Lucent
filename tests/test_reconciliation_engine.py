"""Targeted PostgreSQL integration tests for Command 12 — Reconciliation Engine.

Verifies:
1. Rejection of unapproved documents (strictly requires APPROVED verification).
2. Clean 4-way matching pass (PO + DC + GRN + INV).
3. Overbilled quantity detection (invoiced > accepted).
4. Rejected goods billed detection (goods rejected on GRN but billed on invoice).
5. Short/under-delivery detection (accepted < ordered).
6. Unit price variance detection (invoice unit price > PO unit price).
7. Missing GRN detection when supplier invoice is present.
8. Missing PO detection when root transaction chain lacks an approved PO.
9. Non-USD currency detection and rejection (strict USD policy).
10. Supplier mismatch detection across connected documents.
11. Unmatched line item detection (invoice contains item not on PO).
12. Database persistence, run history, and latest run endpoints.
"""

from decimal import Decimal
import io
import uuid

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.database import get_db, get_engine
from apps.api.main import app
from apps.api.models import (
    Document,
    DocumentExtraction,
    DocumentRelationship,
    DocumentVerification,
    ReconciliationDiscrepancy,
    ReconciliationRun,
)
from apps.api.routes.documents import get_extraction_service, get_ocr_service
from apps.api.routes.matching import get_matching_service
from apps.api.routes.reconciliation import get_reconciliation_service
from apps.api.routes.verifications import get_verification_service
from apps.api.services.extraction_service import DocumentExtractionService
from apps.api.services.matching_service import DocumentMatchingService
from apps.api.services.ocr_service import DocumentOcrService
from apps.api.services.reconciliation_service import ReconciliationService
from apps.api.services.verification_service import DocumentVerificationService
from apps.api.storage import FileSystemStorage, get_storage_service
from packages.ocr.deterministic_engine import DeterministicOcrEngine


@pytest.fixture
def isolated_storage(tmp_path):
    storage = FileSystemStorage(tmp_path / "storage")
    app.dependency_overrides[get_storage_service] = lambda: storage
    yield storage
    app.dependency_overrides.pop(get_storage_service, None)


@pytest.fixture
def client(isolated_storage):
    def _override_ocr_service(db: Session = Depends(get_db)):
        return DocumentOcrService(db=db, storage=isolated_storage, engine=DeterministicOcrEngine())

    def _override_extraction_service(db: Session = Depends(get_db)):
        return DocumentExtractionService(db=db)

    def _override_verification_service(db: Session = Depends(get_db)):
        return DocumentVerificationService(db=db)

    def _override_matching_service(db: Session = Depends(get_db)):
        return DocumentMatchingService(db=db)

    def _override_reconciliation_service(db: Session = Depends(get_db)):
        return ReconciliationService(db=db)

    app.dependency_overrides[get_ocr_service] = _override_ocr_service
    app.dependency_overrides[get_extraction_service] = _override_extraction_service
    app.dependency_overrides[get_verification_service] = _override_verification_service
    app.dependency_overrides[get_matching_service] = _override_matching_service
    app.dependency_overrides[get_reconciliation_service] = _override_reconciliation_service

    yield TestClient(app)

    app.dependency_overrides.pop(get_ocr_service, None)
    app.dependency_overrides.pop(get_extraction_service, None)
    app.dependency_overrides.pop(get_verification_service, None)
    app.dependency_overrides.pop(get_matching_service, None)
    app.dependency_overrides.pop(get_reconciliation_service, None)


def _upload_test_pdf(client, title: str, doc_type: str) -> dict:
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({
        "/Title": title,
        "/Producer": f"RecordaReconTest-{uuid.uuid4()}",
    })
    buf = io.BytesIO()
    writer.write(buf)

    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": doc_type},
        files={"file": (f"recon_{uuid.uuid4().hex[:8]}.pdf", buf.getvalue(), "application/pdf")},
    )
    assert resp.status_code == 201
    return resp.json()


def _create_verified_document(
    client,
    doc_type: str,
    doc_number: str,
    fields: dict,
    line_items: list,
    status: str = "APPROVED",
) -> str:
    """Helper to create and verify a document with custom fields and line items."""
    doc = _upload_test_pdf(client, f"{doc_type} {doc_number} {uuid.uuid4()}", doc_type=doc_type)
    doc_id = doc["id"]

    engine = get_engine()
    with Session(engine) as db:
        extraction = DocumentExtraction(
            document_id=uuid.UUID(doc_id),
            run_number=1,
            document_type=doc_type,
            extractor_version="1.0.0",
            status="COMPLETED",
            fields=fields,
            line_items=line_items,
        )
        db.add(extraction)
        db.flush()

        verification = DocumentVerification(
            document_id=uuid.UUID(doc_id),
            extraction_id=extraction.id,
            status=status,
            version=1,
            verified_fields=fields,
            verified_line_items=line_items,
            reviewed_by="auditor_recon_test",
        )
        db.add(verification)

        doc_row = db.get(Document, uuid.UUID(doc_id))
        doc_row.status = "VERIFIED" if status == "APPROVED" else "PENDING"
        doc_row.document_number = doc_number
        if fields.get("po_reference"):
            doc_row.po_reference = fields["po_reference"]

        db.commit()

    return doc_id


def _link_documents(source_id: str, target_id: str, rel_type: str):
    """Create a CONFIRMED link between two documents in DB."""
    engine = get_engine()
    with Session(engine) as db:
        rel = DocumentRelationship(
            source_document_id=uuid.UUID(source_id),
            target_document_id=uuid.UUID(target_id),
            relationship_type=rel_type,
            status="CONFIRMED",
            matching_method="AUTOMATIC_EXPLICIT_REFERENCE",
            confidence_score=1.0,
            created_by="TEST",
        )
        db.add(rel)
        db.commit()


# ==============================================================================
# TESTS
# ==============================================================================


def test_unapproved_document_rejected_for_reconciliation(client):
    """Test reconciliation rejects unapproved documents with 422."""
    doc_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-UNAPPROVED",
        fields={"po_number": "PO-UNAPPROVED", "currency": "USD"},
        line_items=[],
        status="PENDING_REVIEW",
    )

    resp = client.post(f"/v1/documents/{doc_id}/reconcile")
    assert resp.status_code == 422
    assert "does not have an APPROVED verification record" in resp.json()["detail"]


def test_clean_4_way_reconciliation_passes(client):
    """Test full 4-way transaction chain with matching quantities and rates passes clean."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-RECON-100",
        fields={"po_number": "PO-RECON-100", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )
    dc_id = _create_verified_document(
        client,
        doc_type="DELIVERY_CHALLAN",
        doc_number="DC-RECON-100",
        fields={"dc_number": "DC-RECON-100", "po_reference": "PO-RECON-100", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "dispatched_quantity": "100"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-RECON-100",
        fields={"grn_number": "GRN-RECON-100", "po_reference": "PO-RECON-100", "dc_reference": "DC-RECON-100", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "received_quantity": "100", "accepted_quantity": "100", "rejected_quantity": "0"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-RECON-100",
        fields={"invoice_number": "INV-RECON-100", "po_reference": "PO-RECON-100", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )

    _link_documents(dc_id, po_id, "DELIVERY_CHALLAN_TO_PURCHASE_ORDER")
    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(grn_id, dc_id, "GOODS_RECEIPT_NOTE_TO_DELIVERY_CHALLAN")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "PASS"
    assert data["summary"]["discrepancy_count"] == 0
    assert Decimal(str(data["summary"]["total_amount_at_risk"])) == Decimal("0.0000")


def test_price_variance_not_masked_by_successive_invoice(client):
    """Test Case 4: a later lower-priced invoice must not mask a net-positive price variance."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-PRICE-MULTI-1700",
        fields={"po_number": "PO-PRICE-MULTI-1700", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "SENS-02", "item_description": "Pressure Sensor", "quantity": "10", "unit_price": "100.00", "line_total": "1000.00"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-PRICE-MULTI-1700",
        fields={"grn_number": "GRN-PRICE-MULTI-1700", "po_reference": "PO-PRICE-MULTI-1700", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "SENS-02", "item_description": "Pressure Sensor", "received_quantity": "10", "accepted_quantity": "10", "rejected_quantity": "0"}],
    )
    expensive_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-PRICE-MULTI-1700-A",
        fields={"invoice_number": "INV-PRICE-MULTI-1700-A", "po_reference": "PO-PRICE-MULTI-1700", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "750.00", "grand_total": "750.00"},
        line_items=[{"item_code": "SENS-02", "item_description": "Pressure Sensor", "quantity": "5", "unit_price": "150.00", "line_total": "750.00"}],
    )
    cheap_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-PRICE-MULTI-1700-B",
        fields={"invoice_number": "INV-PRICE-MULTI-1700-B", "po_reference": "PO-PRICE-MULTI-1700", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "500.00", "grand_total": "500.00"},
        line_items=[{"item_code": "SENS-02", "item_description": "Pressure Sensor", "quantity": "5", "unit_price": "100.00", "line_total": "500.00"}],
    )

    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(expensive_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")
    _link_documents(cheap_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    price_findings = [d for d in data["discrepancies"] if d["discrepancy_type"] == "PRICE_VARIANCE"]
    assert len(price_findings) == 1
    assert Decimal(str(price_findings[0]["delta_value"])) == Decimal("25.00")
    assert Decimal(str(price_findings[0]["amount_at_risk"])) == Decimal("250.0000")


def test_reconciliation_overbilled_quantity(client):
    """Test invoiced quantity exceeding accepted GRN quantity flags OVER_BILLED_QUANTITY."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-OVERBILL-200",
        fields={"po_number": "PO-OVERBILL-200", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-OVERBILL-200",
        fields={"grn_number": "GRN-OVERBILL-200", "po_reference": "PO-OVERBILL-200", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "received_quantity": "80", "accepted_quantity": "80", "rejected_quantity": "0"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-OVERBILL-200",
        fields={"invoice_number": "INV-OVERBILL-200", "po_reference": "PO-OVERBILL-200", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )

    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "OVER_BILLED_QUANTITY" in types

    overbill = next(d for d in data["discrepancies"] if d["discrepancy_type"] == "OVER_BILLED_QUANTITY")
    assert Decimal(str(overbill["expected_value"])) == Decimal("80")
    assert Decimal(str(overbill["actual_value"])) == Decimal("100")
    assert Decimal(str(overbill["delta_value"])) == Decimal("20")
    assert Decimal(str(overbill["amount_at_risk"])) == Decimal("200.0000")


def test_reconciliation_rejected_goods_billed(client):
    """Test billing for damaged/rejected goods on GRN triggers REJECTED_GOODS_BILLED."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-REJ-300",
        fields={"po_number": "PO-REJ-300", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-REJ-300",
        fields={"grn_number": "GRN-REJ-300", "po_reference": "PO-REJ-300", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "received_quantity": "100", "accepted_quantity": "90", "rejected_quantity": "10"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-REJ-300",
        fields={"invoice_number": "INV-REJ-300", "po_reference": "PO-REJ-300", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )

    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "REJECTED_GOODS_BILLED" in types
    rej = next(d for d in data["discrepancies"] if d["discrepancy_type"] == "REJECTED_GOODS_BILLED")
    assert Decimal(str(rej["delta_value"])) == Decimal("10")
    assert Decimal(str(rej["amount_at_risk"])) == Decimal("100.0000")


def test_reconciliation_under_delivered_warning(client):
    """Test short delivery where accepted < ordered triggers UNDER_DELIVERED."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-UNDER-400",
        fields={"po_number": "PO-UNDER-400", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-UNDER-400",
        fields={"grn_number": "GRN-UNDER-400", "po_reference": "PO-UNDER-400", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "received_quantity": "70", "accepted_quantity": "70", "rejected_quantity": "0"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-UNDER-400",
        fields={"invoice_number": "INV-UNDER-400", "po_reference": "PO-UNDER-400", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "700.00", "grand_total": "700.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "70", "unit_price": "10.00", "line_total": "700.00"}],
    )

    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "WARNING"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "UNDER_DELIVERED" in types
    assert "OVER_BILLED_QUANTITY" not in types


def test_reconciliation_unit_price_variance(client):
    """Test invoice unit rate exceeding PO rate triggers PRICE_VARIANCE."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-PRICE-500",
        fields={"po_number": "PO-PRICE-500", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "VALVE-02", "item_description": "Hydraulic Valve", "quantity": "50", "unit_price": "20.00", "line_total": "1000.00"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-PRICE-500",
        fields={"grn_number": "GRN-PRICE-500", "po_reference": "PO-PRICE-500", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "VALVE-02", "item_description": "Hydraulic Valve", "received_quantity": "50", "accepted_quantity": "50", "rejected_quantity": "0"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-PRICE-500",
        fields={"invoice_number": "INV-PRICE-500", "po_reference": "PO-PRICE-500", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1250.00", "grand_total": "1250.00"},
        line_items=[{"item_code": "VALVE-02", "item_description": "Hydraulic Valve", "quantity": "50", "unit_price": "25.00", "line_total": "1250.00"}],
    )

    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "PRICE_VARIANCE" in types

    pv = next(d for d in data["discrepancies"] if d["discrepancy_type"] == "PRICE_VARIANCE")
    assert Decimal(str(pv["expected_value"])) == Decimal("20.00")
    assert Decimal(str(pv["actual_value"])) == Decimal("25.00")
    assert Decimal(str(pv["delta_value"])) == Decimal("5.00")
    assert Decimal(str(pv["amount_at_risk"])) == Decimal("250.0000")


def test_reconciliation_missing_grn(client):
    """Test invoice received without corresponding GRN triggers MISSING_GRN."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-NOGRN-600",
        fields={"po_number": "PO-NOGRN-600", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "500.00", "grand_total": "500.00"},
        line_items=[{"item_code": "ITEM-X", "item_description": "Tool Set", "quantity": "10", "unit_price": "50.00", "line_total": "500.00"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-NOGRN-600",
        fields={"invoice_number": "INV-NOGRN-600", "po_reference": "PO-NOGRN-600", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "500.00", "grand_total": "500.00"},
        line_items=[{"item_code": "ITEM-X", "item_description": "Tool Set", "quantity": "10", "unit_price": "50.00", "line_total": "500.00"}],
    )

    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "MISSING_GRN" in types
    mgrn = next(d for d in data["discrepancies"] if d["discrepancy_type"] == "MISSING_GRN")
    assert Decimal(str(mgrn["amount_at_risk"])) == Decimal("500.0000")


def test_reconciliation_missing_po(client):
    """Test transaction chain without an approved PO produces INCOMPLETE status with MISSING_PO."""
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-NOPO-700",
        fields={"invoice_number": "INV-NOPO-700", "supplier_name": "Unknown Co", "currency": "USD", "grand_total": "300.00"},
        line_items=[],
    )

    resp = client.post(f"/v1/documents/{inv_id}/reconcile")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "INCOMPLETE"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "MISSING_PO" in types


def test_reconciliation_non_usd_currency_rejected(client):
    """Test documents with non-USD currency flag NON_USD_CURRENCY."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-EUR-800",
        fields={"po_number": "PO-EUR-800", "supplier_name": "Euro Tech", "currency": "EUR", "grand_total": "1000.00"},
        line_items=[{"item_code": "EUR-1", "item_description": "Euro Part", "quantity": "10", "unit_price": "100.00"}],
    )

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "NON_USD_CURRENCY" in types


def test_reconciliation_supplier_mismatch(client):
    """Test supplier inconsistency between PO and Invoice flags SUPPLIER_MISMATCH."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-SUPP-900",
        fields={"po_number": "PO-SUPP-900", "supplier_name": "Authorized Vendor LLC", "currency": "USD", "grand_total": "500.00"},
        line_items=[{"item_code": "ITEM-1", "item_description": "Part 1", "quantity": "5", "unit_price": "100.00"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-SUPP-900",
        fields={"invoice_number": "INV-SUPP-900", "po_reference": "PO-SUPP-900", "supplier_name": "Bogus Third Party Ltd", "currency": "USD", "grand_total": "500.00"},
        line_items=[{"item_code": "ITEM-1", "item_description": "Part 1", "quantity": "5", "unit_price": "100.00"}],
    )

    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "SUPPLIER_MISMATCH" in types


def test_reconciliation_unmatched_line_item(client):
    """Test invoice with unauthorized line items triggers UNMATCHED_LINE_ITEM."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-UNMATCH-1000",
        fields={"po_number": "PO-UNMATCH-1000", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "500.00", "grand_total": "500.00"},
        line_items=[{"item_code": "ITEM-A", "item_description": "Valid Item", "quantity": "50", "unit_price": "10.00", "line_total": "500.00"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-UNMATCH-1000",
        fields={"grn_number": "GRN-UNMATCH-1000", "po_reference": "PO-UNMATCH-1000", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "ITEM-A", "item_description": "Valid Item", "received_quantity": "50", "accepted_quantity": "50", "rejected_quantity": "0"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-UNMATCH-1000",
        fields={"invoice_number": "INV-UNMATCH-1000", "po_reference": "PO-UNMATCH-1000", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "800.00", "grand_total": "800.00"},
        line_items=[
            {"item_code": "ITEM-A", "item_description": "Valid Item", "quantity": "50", "unit_price": "10.00", "line_total": "500.00"},
            {"item_code": "GHOST-99", "item_description": "Unauthorized Service Fee", "quantity": "1", "unit_price": "300.00", "line_total": "300.00"},
        ],
    )

    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "UNMATCHED_LINE_ITEM" in types


def test_reconciliation_persistence_and_query_endpoints(client):
    """Test reconciliation run is persisted and queryable via list and latest endpoints."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-HIST-1100",
        fields={"po_number": "PO-HIST-1100", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "500.00", "grand_total": "500.00"},
        line_items=[{"item_code": "ITEM-1", "item_description": "Part 1", "quantity": "50", "unit_price": "10.00", "line_total": "500.00"}],
    )

    # Reconcile 1
    r1 = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert r1.status_code == 200
    run1_id = r1.json()["id"]

    # Reconcile 2 (repeated execution)
    r2 = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert r2.status_code == 200
    run2_id = r2.json()["id"]
    assert run1_id != run2_id

    # List reconciliations (both runs preserved)
    r_list = client.get(f"/v1/documents/{po_id}/reconciliations")
    assert r_list.status_code == 200
    runs = r_list.json()
    assert len(runs) >= 2
    run_ids = [r["id"] for r in runs]
    assert run1_id in run_ids
    assert run2_id in run_ids

    # Get latest returns the newer run
    r_latest = client.get(f"/v1/documents/{po_id}/reconciliations/latest")
    assert r_latest.status_code == 200
    assert r_latest.json()["id"] == run2_id


def test_duplicate_and_ambiguous_line_items_not_silently_matched(client):
    """Test Case 3: Duplicate or ambiguous line-item descriptions are flagged and not silently matched."""
    # PO has duplicate items with identical description and no distinct item codes
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-AMBIG-1200",
        fields={"po_number": "PO-AMBIG-1200", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "300.00", "grand_total": "300.00"},
        line_items=[
            {"item_description": "Generic Hydraulic Valve", "quantity": "10", "unit_price": "10.00", "line_total": "100.00"},
            {"item_description": "Generic Hydraulic Valve", "quantity": "20", "unit_price": "10.00", "line_total": "200.00"},
            {"item_code": "", "item_description": "", "quantity": "1", "unit_price": "0.00", "line_total": "0.00"},
        ],
    )

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "FAIL"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "AMBIGUOUS_LINE_ITEM" in types
    assert "UNMATCHED_LINE_ITEM" in types

    ambig = next(d for d in data["discrepancies"] if d["discrepancy_type"] == "AMBIGUOUS_LINE_ITEM")
    assert "multiple line items (2)" in ambig["explanation"]


def test_multiple_partial_deliveries_and_receipts_do_not_double_count(client):
    """Test Case 1: Partial deliveries and receipts across multiple documents do not double-count quantities."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-MULTI-1300",
        fields={"po_number": "PO-MULTI-1300", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )
    # Shipment 1: 40 units
    dc1_id = _create_verified_document(
        client,
        doc_type="DELIVERY_CHALLAN",
        doc_number="DC-MULTI-1300-1",
        fields={"dc_number": "DC-MULTI-1300-1", "po_reference": "PO-MULTI-1300", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "dispatched_quantity": "40"}],
    )
    grn1_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-MULTI-1300-1",
        fields={"grn_number": "GRN-MULTI-1300-1", "po_reference": "PO-MULTI-1300", "dc_reference": "DC-MULTI-1300-1", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "received_quantity": "40", "accepted_quantity": "40", "rejected_quantity": "0"}],
    )
    inv1_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-MULTI-1300-1",
        fields={"invoice_number": "INV-MULTI-1300-1", "po_reference": "PO-MULTI-1300", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "400.00", "grand_total": "400.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "40", "unit_price": "10.00", "line_total": "400.00"}],
    )

    # Shipment 2: 60 units
    dc2_id = _create_verified_document(
        client,
        doc_type="DELIVERY_CHALLAN",
        doc_number="DC-MULTI-1300-2",
        fields={"dc_number": "DC-MULTI-1300-2", "po_reference": "PO-MULTI-1300", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "dispatched_quantity": "60"}],
    )
    grn2_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-MULTI-1300-2",
        fields={"grn_number": "GRN-MULTI-1300-2", "po_reference": "PO-MULTI-1300", "dc_reference": "DC-MULTI-1300-2", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "received_quantity": "60", "accepted_quantity": "60", "rejected_quantity": "0"}],
    )
    inv2_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-MULTI-1300-2",
        fields={"invoice_number": "INV-MULTI-1300-2", "po_reference": "PO-MULTI-1300", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "600.00", "grand_total": "600.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "60", "unit_price": "10.00", "line_total": "600.00"}],
    )

    # Link all documents to PO
    _link_documents(dc1_id, po_id, "DELIVERY_CHALLAN_TO_PURCHASE_ORDER")
    _link_documents(grn1_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv1_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")
    _link_documents(dc2_id, po_id, "DELIVERY_CHALLAN_TO_PURCHASE_ORDER")
    _link_documents(grn2_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv2_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile")
    assert resp.status_code == 200
    data = resp.json()

    # Total accepted = 40 + 60 = 100, Total billed = 40 + 60 = 100, PO ordered = 100
    assert data["status"] == "PASS"
    assert data["summary"]["discrepancy_count"] == 0
    assert Decimal(str(data["summary"]["total_amount_at_risk"])) == Decimal("0.0000")


def test_ongoing_partial_delivery_is_not_treated_as_final_failure(client):
    """Test Case 2: Partial delivery on an ongoing transaction produces INFO/PASS, not FAIL."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-ONGOING-1400",
        fields={"po_number": "PO-ONGOING-1400", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "quantity": "100", "unit_price": "10.00", "line_total": "1000.00"}],
    )
    # Only partial DC received so far (no GRN or Invoice yet)
    dc_id = _create_verified_document(
        client,
        doc_type="DELIVERY_CHALLAN",
        doc_number="DC-ONGOING-1400",
        fields={"dc_number": "DC-ONGOING-1400", "po_reference": "PO-ONGOING-1400", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "BEAR-01", "item_description": "Precision Bearing", "dispatched_quantity": "50"}],
    )
    _link_documents(dc_id, po_id, "DELIVERY_CHALLAN_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile")
    assert resp.status_code == 200
    data = resp.json()

    # Status must be PASS with an INFO discrepancy for partial delivery (not FAIL)
    assert data["status"] == "PASS"
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "PARTIAL_DELIVERY" in types
    part_finding = next(d for d in data["discrepancies"] if d["discrepancy_type"] == "PARTIAL_DELIVERY")
    assert part_finding["severity"] == "INFO"


def test_price_tolerance_boundaries_and_negative_variance(client):
    """Test Case 4: Price tolerance boundaries and negative variances behave correctly."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-TOL-1500",
        fields={"po_number": "PO-TOL-1500", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1000.00", "grand_total": "1000.00"},
        line_items=[{"item_code": "TOL-01", "item_description": "Sensor Module", "quantity": "10", "unit_price": "100.00", "line_total": "1000.00"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-TOL-1500",
        fields={"grn_number": "GRN-TOL-1500", "po_reference": "PO-TOL-1500", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "TOL-01", "item_description": "Sensor Module", "received_quantity": "10", "accepted_quantity": "10", "rejected_quantity": "0"}],
    )
    # Invoice rate $102.00 (2% increase)
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-TOL-1500",
        fields={"invoice_number": "INV-TOL-1500", "po_reference": "PO-TOL-1500", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "1020.00", "grand_total": "1020.00"},
        line_items=[{"item_code": "TOL-01", "item_description": "Sensor Module", "quantity": "10", "unit_price": "102.00", "line_total": "1020.00"}],
    )
    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    # With tolerance 2% and $2.00: variance of exactly 2% ($2.00) is within tolerance
    resp = client.post(
        f"/v1/documents/{po_id}/reconcile",
        json={"allow_missing_dc": True, "tolerance_price_pct": 2.0, "tolerance_price_usd": 2.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    types = [d["discrepancy_type"] for d in data["discrepancies"]]
    assert "PRICE_VARIANCE" not in types

    # Negative price variance: supplier gave a discount ($90 vs $100 rate)
    inv_discount_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-DISC-1500",
        fields={"invoice_number": "INV-DISC-1500", "po_reference": "PO-TOL-1500", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "900.00", "grand_total": "900.00"},
        line_items=[{"item_code": "TOL-01", "item_description": "Sensor Module", "quantity": "10", "unit_price": "90.00", "line_total": "900.00"}],
    )
    _link_documents(inv_discount_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp2 = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp2.status_code == 200
    data2 = resp2.json()
    # Negative variance must not generate a CRITICAL price variance failure
    pv_critical = [d for d in data2["discrepancies"] if d["discrepancy_type"] == "PRICE_VARIANCE"]
    assert len(pv_critical) == 0


def test_decimal_precision_preserved_in_fractional_quantities_and_amounts(client):
    """Test Case 5: Exact Decimal arithmetic without float round-off errors on fractional values."""
    po_id = _create_verified_document(
        client,
        doc_type="PURCHASE_ORDER",
        doc_number="PO-DECIMAL-1600",
        fields={"po_number": "PO-DECIMAL-1600", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "838.1021", "grand_total": "838.1021"},
        line_items=[{"item_code": "CHEM-01", "item_description": "Industrial Solvent", "quantity": "12.3450", "unit_price": "67.8900", "line_total": "838.1021"}],
    )
    grn_id = _create_verified_document(
        client,
        doc_type="GOODS_RECEIPT_NOTE",
        doc_number="GRN-DECIMAL-1600",
        fields={"grn_number": "GRN-DECIMAL-1600", "po_reference": "PO-DECIMAL-1600", "supplier_name": "Apex Industrial", "currency": "USD"},
        line_items=[{"item_code": "CHEM-01", "item_description": "Industrial Solvent", "received_quantity": "12.3450", "accepted_quantity": "12.3450", "rejected_quantity": "0.0000"}],
    )
    inv_id = _create_verified_document(
        client,
        doc_type="SUPPLIER_INVOICE",
        doc_number="INV-DECIMAL-1600",
        fields={"invoice_number": "INV-DECIMAL-1600", "po_reference": "PO-DECIMAL-1600", "supplier_name": "Apex Industrial", "currency": "USD", "subtotal": "838.1021", "grand_total": "838.1021"},
        line_items=[{"item_code": "CHEM-01", "item_description": "Industrial Solvent", "quantity": "12.3450", "unit_price": "67.8900", "line_total": "838.1021"}],
    )

    _link_documents(grn_id, po_id, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER")
    _link_documents(inv_id, po_id, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")

    resp = client.post(f"/v1/documents/{po_id}/reconcile", json={"allow_missing_dc": True})
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "PASS"
    assert data["summary"]["discrepancy_count"] == 0
    assert Decimal(str(data["summary"]["total_amount_at_risk"])) == Decimal("0.0000")

