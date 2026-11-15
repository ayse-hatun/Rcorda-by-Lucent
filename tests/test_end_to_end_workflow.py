"""End-to-end PostgreSQL integration tests for the Recorda backend workflow."""

from decimal import Decimal
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.database import get_engine
from apps.api.main import app
from apps.api.models import (
    Document,
    DocumentExtraction,
    DocumentRelationship,
    DocumentVerification,
    ReconciliationRun,
    Supplier,
)
from apps.api.storage import FileSystemStorage, get_storage_service


@pytest.fixture
def client(tmp_path):
    storage = FileSystemStorage(tmp_path / "storage")
    app.dependency_overrides[get_storage_service] = lambda: storage
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_storage_service, None)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _pdf_bytes(title: str) -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            "/Title": f"{title} {uuid.uuid4()}",
            "/Producer": f"RecordaE2E-{uuid.uuid4()}",
        }
    )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _upload_document(
    client: TestClient,
    doc_type: str,
    document_number: str,
    supplier_id: str,
    po_reference: str | None = None,
    purchase_order_id: str | None = None,
) -> str:
    data = {
        "document_type": doc_type,
        "document_number": document_number,
        "supplier_id": supplier_id,
    }
    if po_reference:
        data["po_reference"] = po_reference
    if purchase_order_id:
        data["purchase_order_id"] = purchase_order_id

    response = client.post(
        "/v1/documents/upload",
        data=data,
        files={
            "file": (
                f"e2e_{uuid.uuid4().hex[:8]}.pdf",
                _pdf_bytes(document_number),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _add_extraction(
    document_id: str,
    doc_type: str,
    fields: dict,
    line_items: list,
) -> str:
    with Session(get_engine()) as db:
        extraction = DocumentExtraction(
            document_id=uuid.UUID(document_id),
            run_number=1,
            document_type=doc_type,
            extractor_version="e2e-test-v1",
            status="COMPLETED",
            fields=fields,
            line_items=line_items,
        )
        db.add(extraction)
        db.commit()
        db.refresh(extraction)
        return str(extraction.id)


def _get_verification(client: TestClient, document_id: str) -> dict:
    response = client.get(f"/v1/documents/{document_id}/verification")
    assert response.status_code == 200, response.text
    return response.json()


def _approve_verification(
    client: TestClient,
    document_id: str,
    expected_version: int,
    reviewer_id: str = "auditor_e2e",
) -> dict:
    response = client.post(
        f"/v1/documents/{document_id}/verification/approve",
        json={
            "reviewer_id": reviewer_id,
            "expected_version": expected_version,
            "notes": "E2E approval",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "APPROVED"
    return data


def _seed_verified_document(
    db: Session,
    doc_type: str,
    document_number: str,
    supplier: Supplier,
    fields: dict,
    line_items: list,
    status: str = "APPROVED",
) -> Document:
    document = Document(
        document_type=doc_type,
        document_number=document_number,
        po_reference=fields.get("po_reference") or fields.get("po_number"),
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED" if status == "APPROVED" else status,
    )
    db.add(document)
    db.flush()

    extraction = DocumentExtraction(
        document_id=document.id,
        run_number=1,
        document_type=doc_type,
        extractor_version="e2e-seed-v1",
        status="COMPLETED",
        fields=fields,
        line_items=line_items,
    )
    db.add(extraction)
    db.flush()

    db.add(
        DocumentVerification(
            document_id=document.id,
            extraction_id=extraction.id,
            status=status,
            version=1,
            verified_fields=fields,
            verified_line_items=line_items,
            reviewed_by="e2e_seed",
        )
    )
    db.flush()
    return document


def _link_documents(
    db: Session,
    source: Document,
    target: Document,
    relationship_type: str,
) -> None:
    db.add(
        DocumentRelationship(
            source_document_id=source.id,
            target_document_id=target.id,
            relationship_type=relationship_type,
            status="CONFIRMED",
            matching_method="AUTOMATIC_EXPLICIT_REFERENCE",
            confidence_score=1.0,
            created_by="E2E",
        )
    )


def _metrics(client: TestClient) -> dict:
    response = client.get("/v1/dashboard/metrics")
    assert response.status_code == 200, response.text
    return response.json()


def test_clean_end_to_end_workflow_persists_report_and_dashboard(client):
    baseline = _metrics(client)
    unique = uuid.uuid4().hex[:10]
    supplier_name = f"E2E Supplier {unique}"
    po_number = f"PO-E2E-{unique}"
    dc_number = f"DC-E2E-{unique}"
    grn_number = f"GRN-E2E-{unique}"
    invoice_number = f"INV-E2E-{unique}"

    supplier_response = client.post(
        "/v1/suppliers",
        json={"name": supplier_name, "code": f"E2E-SUP-{unique}"},
    )
    assert supplier_response.status_code == 201, supplier_response.text
    supplier_id = supplier_response.json()["id"]

    po_response = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": po_number,
            "supplier_id": supplier_id,
            "currency": "USD",
            "items": [
                {
                    "line_item_id": "1",
                    "item_code": "BEAR-01",
                    "description": "Precision Bearing",
                    "ordered_quantity": "10",
                    "unit_price": "10.00",
                }
            ],
        },
    )
    assert po_response.status_code == 201, po_response.text
    purchase_order_id = po_response.json()["id"]

    po_document_id = _upload_document(
        client,
        "PURCHASE_ORDER",
        po_number,
        supplier_id,
        po_reference=po_number,
    )
    dc_document_id = _upload_document(
        client,
        "DELIVERY_CHALLAN",
        dc_number,
        supplier_id,
        po_reference=po_number,
        purchase_order_id=purchase_order_id,
    )
    grn_document_id = _upload_document(
        client,
        "GOODS_RECEIPT_NOTE",
        grn_number,
        supplier_id,
        po_reference=po_number,
        purchase_order_id=purchase_order_id,
    )
    invoice_document_id = _upload_document(
        client,
        "SUPPLIER_INVOICE",
        invoice_number,
        supplier_id,
        po_reference=po_number,
        purchase_order_id=purchase_order_id,
    )

    po_fields = {
        "po_number": po_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "payment_terms": "NET 30",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    po_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]
    dc_fields = {
        "dc_number": dc_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    dc_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "dispatched_quantity": "10",
        }
    ]
    grn_fields = {
        "grn_number": grn_number,
        "po_reference": po_number,
        "dc_reference": dc_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    grn_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "received_quantity": "10",
            "accepted_quantity": "10",
            "rejected_quantity": "0",
        }
    ]
    invoice_fields = {
        "invoice_number": invoice_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    invoice_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]

    _add_extraction(po_document_id, "PURCHASE_ORDER", po_fields, po_items)
    _add_extraction(dc_document_id, "DELIVERY_CHALLAN", dc_fields, dc_items)
    _add_extraction(grn_document_id, "GOODS_RECEIPT_NOTE", grn_fields, grn_items)
    _add_extraction(invoice_document_id, "SUPPLIER_INVOICE", invoice_fields, invoice_items)

    po_verification = _get_verification(client, po_document_id)
    assert po_verification["status"] == "PENDING_REVIEW"
    correction = client.post(
        f"/v1/documents/{po_document_id}/verification/correct-field",
        json={
            "field_name": "payment_terms",
            "corrected_value": "NET 45",
            "corrected_by": "auditor_e2e",
            "expected_version": po_verification["version"],
            "reason": "Verified contract terms from source document",
        },
    )
    assert correction.status_code == 200, correction.text
    assert correction.json()["verified_fields"]["payment_terms"] == "NET 45"
    _approve_verification(client, po_document_id, correction.json()["version"])

    for document_id in (dc_document_id, grn_document_id, invoice_document_id):
        verification = _get_verification(client, document_id)
        _approve_verification(client, document_id, verification["version"])

    delivery_match = client.post(f"/v1/documents/{dc_document_id}/match")
    assert delivery_match.status_code == 200, delivery_match.text
    assert delivery_match.json()["match_status"] == "MATCHED"
    assert delivery_match.json()["relationships"][0]["status"] == "CONFIRMED"

    grn_match = client.post(f"/v1/documents/{grn_document_id}/match")
    assert grn_match.status_code == 200, grn_match.text
    assert grn_match.json()["match_status"] == "MATCHED"
    assert len(grn_match.json()["relationships"]) == 2
    assert all(rel["status"] == "CONFIRMED" for rel in grn_match.json()["relationships"])

    invoice_match = client.post(f"/v1/documents/{invoice_document_id}/match")
    assert invoice_match.status_code == 200, invoice_match.text
    assert invoice_match.json()["match_status"] == "MATCHED"
    assert invoice_match.json()["relationships"][0]["status"] == "CONFIRMED"

    relationships = client.get(f"/v1/documents/{invoice_document_id}/relationships")
    assert relationships.status_code == 200, relationships.text
    chain = relationships.json()["transaction_chain"]
    assert chain["connected_document_count"] == 4
    assert len(chain["purchase_orders"]) == 1
    assert len(chain["delivery_challans"]) == 1
    assert len(chain["goods_receipt_notes"]) == 1
    assert len(chain["supplier_invoices"]) == 1

    reconciliation = client.post(
        f"/v1/documents/{po_document_id}/reconcile",
        json={"actor": "e2e_runner"},
    )
    assert reconciliation.status_code == 200, reconciliation.text
    run = reconciliation.json()
    assert run["status"] == "PASS"
    assert run["summary"]["discrepancy_count"] == 0
    assert len(run["reconciled_document_ids"]) == 4
    assert run["po_document_id"] == po_document_id

    with Session(get_engine()) as db:
        persisted_run = db.get(ReconciliationRun, uuid.UUID(run["id"]))
        assert persisted_run is not None
        assert persisted_run.status == "PASS"
        assert len(persisted_run.discrepancies) == 0

    exceptions = client.get(
        "/v1/reconciliation-exceptions",
        params={"reconciliation_run_id": run["id"]},
    )
    assert exceptions.status_code == 200, exceptions.text
    assert exceptions.json() == []

    report_response = client.get(f"/v1/reports/reconciliation-runs/{run['id']}")
    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    assert report["status"] == "PASS"
    assert report["po_reference"] == po_number
    assert report["supplier_name"] == supplier_name
    assert Decimal(str(report["total_amount_at_risk"])) == Decimal("0.0000")
    assert len(report["documents"]) == 4
    assert len(report["discrepancies"]) == 0

    po_report = next(
        document
        for document in report["documents"]
        if document["document_id"] == po_document_id
    )
    assert po_report["is_root"] is True
    assert po_report["is_purchase_order"] is True
    assert po_report["ai_extraction"]["fields"]["payment_terms"] == "NET 30"
    assert po_report["human_verification"]["verified_fields"]["payment_terms"] == "NET 45"
    assert all(document["ai_extraction"] for document in report["documents"])
    assert all(document["human_verification"] for document in report["documents"])
    assert all(
        document["human_verification"]["status"] == "APPROVED"
        for document in report["documents"]
    )

    after = _metrics(client)
    assert after["total_suppliers"] == baseline["total_suppliers"] + 1
    assert after["total_purchase_orders"] == baseline["total_purchase_orders"] + 1
    assert after["total_documents"] == baseline["total_documents"] + 4
    assert after["documents_awaiting_review"] == baseline["documents_awaiting_review"]
    assert after["open_exceptions"] == baseline["open_exceptions"]
    assert after["resolved_exceptions"] == baseline["resolved_exceptions"]
    assert after["financial_discrepancies"] == baseline["financial_discrepancies"]
    assert after["late_deliveries"] == baseline["late_deliveries"]
    assert after["partial_deliveries"] == baseline["partial_deliveries"]


def test_discrepancy_projects_exception_and_updates_dashboard_and_report(client):
    baseline = _metrics(client)
    unique = uuid.uuid4().hex[:10]
    supplier_name = f"Exception Supplier {unique}"
    po_number = f"PO-EXC-{unique}"
    grn_number = f"GRN-EXC-{unique}"
    invoice_number = f"INV-EXC-{unique}"

    supplier_response = client.post(
        "/v1/suppliers",
        json={"name": supplier_name, "code": f"EXC-SUP-{unique}"},
    )
    assert supplier_response.status_code == 201, supplier_response.text
    supplier_id = supplier_response.json()["id"]

    po_response = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": po_number,
            "supplier_id": supplier_id,
            "currency": "USD",
            "items": [
                {
                    "line_item_id": "1",
                    "item_code": "BEAR-01",
                    "description": "Precision Bearing",
                    "ordered_quantity": "10",
                    "unit_price": "10.00",
                }
            ],
        },
    )
    assert po_response.status_code == 201, po_response.text

    po_fields = {
        "po_number": po_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    po_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]
    grn_fields = {
        "grn_number": grn_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    grn_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "received_quantity": "10",
            "accepted_quantity": "10",
            "rejected_quantity": "0",
        }
    ]
    invoice_fields = {
        "invoice_number": invoice_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "120.00",
        "grand_total": "120.00",
    }
    invoice_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "12",
            "unit_price": "10.00",
            "line_total": "120.00",
        }
    ]

    with Session(get_engine()) as db:
        supplier = db.get(Supplier, uuid.UUID(supplier_id))
        assert supplier is not None
        purchase_order = _seed_verified_document(
            db, "PURCHASE_ORDER", po_number, supplier, po_fields, po_items
        )
        grn = _seed_verified_document(
            db, "GOODS_RECEIPT_NOTE", grn_number, supplier, grn_fields, grn_items
        )
        invoice = _seed_verified_document(
            db, "SUPPLIER_INVOICE", invoice_number, supplier, invoice_fields, invoice_items
        )
        _link_documents(
            db, grn, purchase_order, "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER"
        )
        _link_documents(db, invoice, purchase_order, "SUPPLIER_INVOICE_TO_PURCHASE_ORDER")
        db.commit()
        purchase_order_id = str(purchase_order.id)
        grn_id = str(grn.id)
        invoice_id = str(invoice.id)

    reconciliation = client.post(
        f"/v1/documents/{purchase_order_id}/reconcile",
        json={"actor": "e2e_finance", "allow_missing_dc": True},
    )
    assert reconciliation.status_code == 200, reconciliation.text
    run = reconciliation.json()
    assert run["status"] == "FAIL"

    overbilled = next(
        discrepancy
        for discrepancy in run["discrepancies"]
        if discrepancy["discrepancy_type"] == "OVER_BILLED_QUANTITY"
    )
    assert Decimal(str(overbilled["expected_value"])) == Decimal("10")
    assert Decimal(str(overbilled["actual_value"])) == Decimal("12")
    assert Decimal(str(overbilled["amount_at_risk"])) == Decimal("20.0000")
    assert set(run["reconciled_document_ids"]) == {
        purchase_order_id,
        grn_id,
        invoice_id,
    }

    exceptions_response = client.get(
        "/v1/reconciliation-exceptions",
        params={"reconciliation_run_id": run["id"]},
    )
    assert exceptions_response.status_code == 200, exceptions_response.text
    exceptions = exceptions_response.json()
    assert len(exceptions) == len(run["discrepancies"])
    overbilled_exception = next(
        exception
        for exception in exceptions
        if exception["exception_type"] == "OVER_BILLED_QUANTITY"
    )
    assert overbilled_exception["status"] == "OPEN"
    assert Decimal(str(overbilled_exception["financial_impact"])) == Decimal("20.0000")
    assert overbilled_exception["po_reference"] == po_number
    assert overbilled_exception["supplier_name"] == supplier_name
    assert purchase_order_id in overbilled_exception["related_document_ids"]
    assert overbilled_exception["history"][0]["action"] == "CREATED"

    after_reconciliation = _metrics(client)
    assert after_reconciliation["total_suppliers"] == baseline["total_suppliers"] + 1
    assert (
        after_reconciliation["total_purchase_orders"]
        == baseline["total_purchase_orders"] + 1
    )
    assert after_reconciliation["total_documents"] == baseline["total_documents"] + 3
    assert (
        after_reconciliation["open_exceptions"]
        == baseline["open_exceptions"] + len(exceptions)
    )
    expected_financial = len(
        [
            discrepancy
            for discrepancy in run["discrepancies"]
            if Decimal(str(discrepancy["amount_at_risk"])) > 0
        ]
    )
    assert (
        after_reconciliation["financial_discrepancies"]
        == baseline["financial_discrepancies"] + expected_financial
    )

    filtered = client.get(
        "/v1/reports/exceptions",
        params={"po_number": po_number, "status": "OPEN"},
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == len(exceptions)

    transition = client.patch(
        f"/v1/reconciliation-exceptions/{overbilled_exception['id']}/status",
        json={
            "status": "UNDER_REVIEW",
            "actor": "e2e_finance",
            "note": "Investigating supplier invoice quantity",
        },
    )
    assert transition.status_code == 200, transition.text
    assert transition.json()["status"] == "UNDER_REVIEW"

    resolution = client.post(
        f"/v1/reconciliation-exceptions/{overbilled_exception['id']}/resolve",
        json={
            "resolution_notes": "Supplier issued a credit note for the excess quantity.",
            "actor": "e2e_finance",
            "supporting_information": {"credit_note": "CN-E2E-001"},
        },
    )
    assert resolution.status_code == 200, resolution.text
    assert resolution.json()["status"] == "RESOLVED"
    assert resolution.json()["resolved_by"] == "e2e_finance"

    history = client.get(
        f"/v1/reconciliation-exceptions/{overbilled_exception['id']}/history"
    )
    assert history.status_code == 200, history.text
    assert [entry["action"] for entry in history.json()] == [
        "CREATED",
        "STATUS_CHANGED",
        "RESOLVED",
    ]
    assert history.json()[1]["from_status"] == "OPEN"
    assert history.json()[1]["to_status"] == "UNDER_REVIEW"

    after_resolution = _metrics(client)
    assert (
        after_resolution["open_exceptions"]
        == after_reconciliation["open_exceptions"] - 1
    )
    assert (
        after_resolution["resolved_exceptions"]
        == after_reconciliation["resolved_exceptions"] + 1
    )
    assert (
        after_resolution["financial_discrepancies"]
        == after_reconciliation["financial_discrepancies"]
    )

    report_response = client.get(f"/v1/reports/reconciliation-runs/{run['id']}")
    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    assert report["status"] == "FAIL"
    assert report["po_reference"] == po_number
    assert report["supplier_name"] == supplier_name
    assert any(
        discrepancy["discrepancy_type"] == "OVER_BILLED_QUANTITY"
        for discrepancy in report["discrepancies"]
    )
    assert Decimal(str(report["total_amount_at_risk"])) == sum(
        Decimal(str(discrepancy["amount_at_risk"]))
        for discrepancy in run["discrepancies"]
    )
    assert len(report["documents"]) == 3
    assert all(document["ai_extraction"] for document in report["documents"])
    assert all(document["human_verification"] for document in report["documents"])


def test_rejected_and_ambiguous_relationships_stay_out_of_confirmed_chains(client):
    unique = uuid.uuid4().hex[:10]
    ambiguous_po_number = f"PO-AMB-{unique}"
    ambiguous_dc_number = f"DC-AMB-{unique}"
    mismatch_po_number = f"PO-MIS-{unique}"
    mismatch_dc_number = f"DC-MIS-{unique}"

    with Session(get_engine()) as db:
        supplier_a = Supplier(name=f"Supplier A {unique}", code=f"SUP-A-{unique}")
        supplier_b = Supplier(name=f"Supplier B {unique}", code=f"SUP-B-{unique}")
        supplier_c = Supplier(name=f"Supplier C {unique}", code=f"SUP-C-{unique}")
        db.add_all([supplier_a, supplier_b, supplier_c])
        db.flush()

        rejected_dc = _seed_verified_document(
            db,
            "DELIVERY_CHALLAN",
            f"DC-REJ-{unique}",
            supplier_a,
            {
                "dc_number": f"DC-REJ-{unique}",
                "po_reference": f"PO-REJ-{unique}",
                "supplier_name": supplier_a.name,
                "currency": "USD",
            },
            [],
            status="REJECTED",
        )

        ambiguous_po_a = _seed_verified_document(
            db,
            "PURCHASE_ORDER",
            ambiguous_po_number,
            supplier_a,
            {
                "po_number": ambiguous_po_number,
                "po_reference": ambiguous_po_number,
                "supplier_name": supplier_a.name,
                "currency": "USD",
                "grand_total": "100.00",
            },
            [],
        )
        ambiguous_po_b = _seed_verified_document(
            db,
            "PURCHASE_ORDER",
            ambiguous_po_number,
            supplier_a,
            {
                "po_number": ambiguous_po_number,
                "po_reference": ambiguous_po_number,
                "supplier_name": supplier_a.name,
                "currency": "USD",
                "grand_total": "100.00",
            },
            [],
        )
        ambiguous_dc = _seed_verified_document(
            db,
            "DELIVERY_CHALLAN",
            ambiguous_dc_number,
            supplier_a,
            {
                "dc_number": ambiguous_dc_number,
                "po_reference": ambiguous_po_number,
                "supplier_name": supplier_a.name,
                "currency": "USD",
            },
            [],
        )

        mismatch_po = _seed_verified_document(
            db,
            "PURCHASE_ORDER",
            mismatch_po_number,
            supplier_b,
            {
                "po_number": mismatch_po_number,
                "po_reference": mismatch_po_number,
                "supplier_name": supplier_b.name,
                "currency": "USD",
                "grand_total": "100.00",
            },
            [],
        )
        mismatch_dc = _seed_verified_document(
            db,
            "DELIVERY_CHALLAN",
            mismatch_dc_number,
            supplier_c,
            {
                "dc_number": mismatch_dc_number,
                "po_reference": mismatch_po_number,
                "supplier_name": supplier_c.name,
                "currency": "USD",
            },
            [],
        )

        db.commit()
        rejected_dc_id = str(rejected_dc.id)
        ambiguous_po_ids = {str(ambiguous_po_a.id), str(ambiguous_po_b.id)}
        ambiguous_dc_id = str(ambiguous_dc.id)
        mismatch_po_id = str(mismatch_po.id)
        mismatch_dc_id = str(mismatch_dc.id)

    rejected_match = client.post(f"/v1/documents/{rejected_dc_id}/match")
    assert rejected_match.status_code == 422, rejected_match.text
    assert "only APPROVED human-verified" in rejected_match.json()["detail"]
    with Session(get_engine()) as db:
        rejected_relationships = db.execute(
            select(DocumentRelationship).where(
                DocumentRelationship.source_document_id == uuid.UUID(rejected_dc_id)
            )
        ).scalars().all()
        assert rejected_relationships == []

    ambiguous_match = client.post(f"/v1/documents/{ambiguous_dc_id}/match")
    assert ambiguous_match.status_code == 200, ambiguous_match.text
    ambiguous_result = ambiguous_match.json()
    assert ambiguous_result["match_status"] == "AMBIGUOUS"
    assert len(ambiguous_result["relationships"]) == 2
    assert all(
        relationship["status"] == "FLAGGED_AMBIGUOUS"
        for relationship in ambiguous_result["relationships"]
    )

    ambiguous_chain = client.get(
        f"/v1/documents/{ambiguous_dc_id}/relationships"
    ).json()["transaction_chain"]
    assert ambiguous_chain["connected_document_count"] == 1
    assert ambiguous_chain["purchase_orders"] == []
    assert not ambiguous_po_ids.intersection(
        {
            document["id"]
            for document in ambiguous_chain["purchase_orders"]
        }
    )

    ambiguous_reconciliation = client.post(
        f"/v1/documents/{ambiguous_dc_id}/reconcile",
        json={"actor": "e2e_runner"},
    )
    assert ambiguous_reconciliation.status_code == 200, ambiguous_reconciliation.text
    ambiguous_run = ambiguous_reconciliation.json()
    assert ambiguous_run["status"] == "INCOMPLETE"
    assert any(
        discrepancy["discrepancy_type"] == "MISSING_PO"
        for discrepancy in ambiguous_run["discrepancies"]
    )
    assert not ambiguous_po_ids.intersection(set(ambiguous_run["reconciled_document_ids"]))

    mismatch_match = client.post(f"/v1/documents/{mismatch_dc_id}/match")
    assert mismatch_match.status_code == 200, mismatch_match.text
    mismatch_result = mismatch_match.json()
    assert mismatch_result["match_status"] == "SUPPLIER_MISMATCH"
    assert len(mismatch_result["relationships"]) == 1
    assert mismatch_result["relationships"][0]["status"] == "REJECTED"

    mismatch_chain = client.get(
        f"/v1/documents/{mismatch_dc_id}/relationships"
    ).json()["transaction_chain"]
    assert mismatch_chain["connected_document_count"] == 1
    assert mismatch_chain["purchase_orders"] == []

    mismatch_reconciliation = client.post(
        f"/v1/documents/{mismatch_dc_id}/reconcile",
        json={"actor": "e2e_runner"},
    )
    assert mismatch_reconciliation.status_code == 200, mismatch_reconciliation.text
    mismatch_run = mismatch_reconciliation.json()
    assert mismatch_run["status"] == "INCOMPLETE"
    assert any(
        discrepancy["discrepancy_type"] == "MISSING_PO"
        for discrepancy in mismatch_run["discrepancies"]
    )
    assert mismatch_po_id not in mismatch_run["reconciled_document_ids"]


def test_multiple_partial_dc_and_grn_deliveries_against_one_po(client):
    """Case 1: Multiple partial DC/GRN deliveries against one PO.

    Verifies persisted relationships, cumulative quantities across deliveries,
    reconciliation results (PASS), discrepancies (none), and exceptions (none).
    """
    unique = _unique("MP-DEL")
    supplier_name = f"Partial Supplier {unique}"
    po_number = f"PO-{unique}"
    dc1_number = f"DC1-{unique}"
    grn1_number = f"GRN1-{unique}"
    dc2_number = f"DC2-{unique}"
    grn2_number = f"GRN2-{unique}"
    inv_number = f"INV-{unique}"

    po_fields = {
        "po_number": po_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "200.00",
        "grand_total": "200.00",
    }
    po_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "20",
            "unit_price": "10.00",
            "line_total": "200.00",
        }
    ]

    dc1_fields = {
        "dc_number": dc1_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    dc1_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "dispatched_quantity": "8",
        }
    ]

    grn1_fields = {
        "grn_number": grn1_number,
        "po_reference": po_number,
        "dc_reference": dc1_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    grn1_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "received_quantity": "8",
            "accepted_quantity": "8",
            "rejected_quantity": "0",
        }
    ]

    dc2_fields = {
        "dc_number": dc2_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    dc2_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "dispatched_quantity": "12",
        }
    ]

    grn2_fields = {
        "grn_number": grn2_number,
        "po_reference": po_number,
        "dc_reference": dc2_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    grn2_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "received_quantity": "12",
            "accepted_quantity": "12",
            "rejected_quantity": "0",
        }
    ]

    inv_fields = {
        "invoice_number": inv_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "200.00",
        "grand_total": "200.00",
    }
    inv_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "20",
            "unit_price": "10.00",
            "line_total": "200.00",
        }
    ]

    with Session(get_engine()) as db:
        supplier = Supplier(name=supplier_name, code=f"SUP-{unique}")
        db.add(supplier)
        db.flush()

        po_doc = _seed_verified_document(db, "PURCHASE_ORDER", po_number, supplier, po_fields, po_items)
        dc1_doc = _seed_verified_document(db, "DELIVERY_CHALLAN", dc1_number, supplier, dc1_fields, dc1_items)
        grn1_doc = _seed_verified_document(db, "GOODS_RECEIPT_NOTE", grn1_number, supplier, grn1_fields, grn1_items)
        dc2_doc = _seed_verified_document(db, "DELIVERY_CHALLAN", dc2_number, supplier, dc2_fields, dc2_items)
        grn2_doc = _seed_verified_document(db, "GOODS_RECEIPT_NOTE", grn2_number, supplier, grn2_fields, grn2_items)
        inv_doc = _seed_verified_document(db, "SUPPLIER_INVOICE", inv_number, supplier, inv_fields, inv_items)

        db.commit()
        po_id = str(po_doc.id)
        dc1_id = str(dc1_doc.id)
        grn1_id = str(grn1_doc.id)
        dc2_id = str(dc2_doc.id)
        grn2_id = str(grn2_doc.id)
        inv_id = str(inv_doc.id)

    # Execute matching for deliveries, receipts, and invoice
    m_dc1 = client.post(f"/v1/documents/{dc1_id}/match")
    assert m_dc1.status_code == 200, m_dc1.text
    assert m_dc1.json()["match_status"] == "MATCHED"

    m_grn1 = client.post(f"/v1/documents/{grn1_id}/match")
    assert m_grn1.status_code == 200, m_grn1.text
    assert m_grn1.json()["match_status"] == "MATCHED"

    m_dc2 = client.post(f"/v1/documents/{dc2_id}/match")
    assert m_dc2.status_code == 200, m_dc2.text
    assert m_dc2.json()["match_status"] == "MATCHED"

    m_grn2 = client.post(f"/v1/documents/{grn2_id}/match")
    assert m_grn2.status_code == 200, m_grn2.text
    assert m_grn2.json()["match_status"] == "MATCHED"

    m_inv = client.post(f"/v1/documents/{inv_id}/match")
    assert m_inv.status_code == 200, m_inv.text
    assert m_inv.json()["match_status"] == "MATCHED"

    # Verify persisted relationships in database
    with Session(get_engine()) as db:
        rels = db.execute(
            select(DocumentRelationship).where(
                DocumentRelationship.source_document_id.in_([
                    uuid.UUID(dc1_id), uuid.UUID(grn1_id),
                    uuid.UUID(dc2_id), uuid.UUID(grn2_id),
                    uuid.UUID(inv_id),
                ])
            )
        ).scalars().all()
        assert len(rels) >= 6
        assert all(r.status == "CONFIRMED" for r in rels)

    # Verify complete multi-delivery transaction chain
    chain_resp = client.get(f"/v1/documents/{po_id}/relationships")
    assert chain_resp.status_code == 200, chain_resp.text
    chain = chain_resp.json()["transaction_chain"]
    assert chain["connected_document_count"] == 6
    assert len(chain["delivery_challans"]) == 2
    assert len(chain["goods_receipt_notes"]) == 2
    assert len(chain["supplier_invoices"]) == 1

    # Reconcile: cumulative dispatched (8+12=20) and accepted (8+12=20) match ordered (20) and billed (20)
    rec = client.post(f"/v1/documents/{po_id}/reconcile", json={"actor": "e2e_partial_tester"})
    assert rec.status_code == 200, rec.text
    run = rec.json()
    assert run["status"] == "PASS"
    assert run["summary"]["discrepancy_count"] == 0
    assert Decimal(str(run["summary"]["total_po_amount"])) == Decimal("200.00")
    assert Decimal(str(run["summary"]["total_billed_amount"])) == Decimal("200.00")
    assert Decimal(str(run["summary"]["total_amount_at_risk"])) == Decimal("0.00")
    assert len(run["reconciled_document_ids"]) == 6

    # Verify report and exceptions
    report_resp = client.get(f"/v1/reports/reconciliation-runs/{run['id']}")
    assert report_resp.status_code == 200, report_resp.text
    assert len(report_resp.json()["documents"]) == 6

    exc_resp = client.get("/v1/reconciliation-exceptions", params={"reconciliation_run_id": run["id"]})
    assert exc_resp.status_code == 200, exc_resp.text
    assert exc_resp.json() == []


def test_multiple_invoices_against_one_po(client):
    """Case 2: Multiple invoices against one PO.

    Verifies persisted relationships for each invoice, cumulative quantities and
    amounts across split invoices, reconciliation results, and overbilling exceptions.
    """
    unique = _unique("MI-INV")
    supplier_name = f"Multi-Invoice Supplier {unique}"
    po_number = f"PO-{unique}"
    dc_number = f"DC-{unique}"
    grn_number = f"GRN-{unique}"
    inv1_number = f"INV1-{unique}"
    inv2_number = f"INV2-{unique}"

    po_fields = {
        "po_number": po_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    po_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]

    dc_fields = {
        "dc_number": dc_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    dc_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "dispatched_quantity": "10",
        }
    ]

    grn_fields = {
        "grn_number": grn_number,
        "po_reference": po_number,
        "dc_reference": dc_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    grn_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "received_quantity": "10",
            "accepted_quantity": "10",
            "rejected_quantity": "0",
        }
    ]

    inv1_fields = {
        "invoice_number": inv1_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "40.00",
        "grand_total": "40.00",
    }
    inv1_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "4",
            "unit_price": "10.00",
            "line_total": "40.00",
        }
    ]

    inv2_fields = {
        "invoice_number": inv2_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "60.00",
        "grand_total": "60.00",
    }
    inv2_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "6",
            "unit_price": "10.00",
            "line_total": "60.00",
        }
    ]

    with Session(get_engine()) as db:
        supplier = Supplier(name=supplier_name, code=f"SUP-{unique}")
        db.add(supplier)
        db.flush()

        po_doc = _seed_verified_document(db, "PURCHASE_ORDER", po_number, supplier, po_fields, po_items)
        dc_doc = _seed_verified_document(db, "DELIVERY_CHALLAN", dc_number, supplier, dc_fields, dc_items)
        grn_doc = _seed_verified_document(db, "GOODS_RECEIPT_NOTE", grn_number, supplier, grn_fields, grn_items)
        inv1_doc = _seed_verified_document(db, "SUPPLIER_INVOICE", inv1_number, supplier, inv1_fields, inv1_items)
        inv2_doc = _seed_verified_document(db, "SUPPLIER_INVOICE", inv2_number, supplier, inv2_fields, inv2_items)

        db.commit()
        po_id = str(po_doc.id)
        dc_id = str(dc_doc.id)
        grn_id = str(grn_doc.id)
        inv1_id = str(inv1_doc.id)
        inv2_id = str(inv2_doc.id)

    # Match delivery, receipt, and both invoices
    assert client.post(f"/v1/documents/{dc_id}/match").status_code == 200
    assert client.post(f"/v1/documents/{grn_id}/match").status_code == 200
    m_inv1 = client.post(f"/v1/documents/{inv1_id}/match")
    assert m_inv1.status_code == 200
    assert m_inv1.json()["match_status"] == "MATCHED"

    m_inv2 = client.post(f"/v1/documents/{inv2_id}/match")
    assert m_inv2.status_code == 200
    assert m_inv2.json()["match_status"] == "MATCHED"

    # Verify persisted relationships for both invoices in DB
    with Session(get_engine()) as db:
        inv_rels = db.execute(
            select(DocumentRelationship).where(
                DocumentRelationship.source_document_id.in_([uuid.UUID(inv1_id), uuid.UUID(inv2_id)]),
                DocumentRelationship.target_document_id == uuid.UUID(po_id),
            )
        ).scalars().all()
        assert len(inv_rels) == 2
        assert all(r.status == "CONFIRMED" for r in inv_rels)

    # Reconcile: cumulative billed quantity (4+6=10) and total ($40+$60=$100) match PO and GRN exactly
    rec = client.post(f"/v1/documents/{po_id}/reconcile", json={"actor": "e2e_multi_inv_tester"})
    assert rec.status_code == 200, rec.text
    run = rec.json()
    assert run["status"] == "PASS"
    assert run["summary"]["discrepancy_count"] == 0
    assert Decimal(str(run["summary"]["total_billed_amount"])) == Decimal("100.00")
    assert Decimal(str(run["summary"]["total_po_amount"])) == Decimal("100.00")
    assert Decimal(str(run["summary"]["total_amount_at_risk"])) == Decimal("0.00")
    assert len(run["reconciled_document_ids"]) == 5

    # Check exceptions: none for valid split invoices
    assert client.get("/v1/reconciliation-exceptions", params={"reconciliation_run_id": run["id"]}).json() == []

    # Add a third invoice that causes cumulative overbilling against the single PO
    inv3_number = f"INV3-{unique}"
    inv3_fields = {
        "invoice_number": inv3_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "30.00",
        "grand_total": "30.00",
    }
    inv3_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "3",
            "unit_price": "10.00",
            "line_total": "30.00",
        }
    ]
    with Session(get_engine()) as db:
        supplier = db.get(Supplier, (db.get(Document, uuid.UUID(po_id))).supplier_id)
        inv3_doc = _seed_verified_document(db, "SUPPLIER_INVOICE", inv3_number, supplier, inv3_fields, inv3_items)
        db.commit()
        inv3_id = str(inv3_doc.id)

    assert client.post(f"/v1/documents/{inv3_id}/match").status_code == 200

    rec2 = client.post(f"/v1/documents/{po_id}/reconcile", json={"actor": "e2e_multi_inv_tester"})
    assert rec2.status_code == 200, rec2.text
    run2 = rec2.json()
    assert run2["status"] == "FAIL"

    overbilled = next(d for d in run2["discrepancies"] if d["discrepancy_type"] == "OVER_BILLED_QUANTITY")
    assert Decimal(str(overbilled["expected_value"])) == Decimal("10")
    assert Decimal(str(overbilled["actual_value"])) == Decimal("13")
    assert Decimal(str(overbilled["delta_value"])) == Decimal("3")
    assert Decimal(str(overbilled["amount_at_risk"])) == Decimal("30.0000")

    # Verify exception projected for cumulative overbilling
    exc_resp = client.get("/v1/reconciliation-exceptions", params={"reconciliation_run_id": run2["id"]})
    assert exc_resp.status_code == 200
    exc_list = exc_resp.json()
    overbilled_exc = next(e for e in exc_list if e["exception_type"] == "OVER_BILLED_QUANTITY")
    assert overbilled_exc["status"] == "OPEN"
    assert Decimal(str(overbilled_exc["financial_impact"])) == Decimal("30.0000")


def test_missing_dc_and_missing_grn(client):
    """Case 3: Missing DC and missing GRN.

    Verifies persisted relationships, reconciliation results (FAIL),
    discrepancies (MISSING_GRN, MISSING_DC), and generated exceptions.
    """
    unique = _unique("MISSING-DC-GRN")
    supplier_name = f"Missing DC GRN Supplier {unique}"
    po_number = f"PO-{unique}"
    inv_number = f"INV-{unique}"

    po_fields = {
        "po_number": po_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    po_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]

    inv_fields = {
        "invoice_number": inv_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    inv_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]

    with Session(get_engine()) as db:
        supplier = Supplier(name=supplier_name, code=f"SUP-{unique}")
        db.add(supplier)
        db.flush()

        po_doc = _seed_verified_document(db, "PURCHASE_ORDER", po_number, supplier, po_fields, po_items)
        inv_doc = _seed_verified_document(db, "SUPPLIER_INVOICE", inv_number, supplier, inv_fields, inv_items)
        db.commit()
        po_id = str(po_doc.id)
        inv_id = str(inv_doc.id)

    # Match invoice to PO (only PO and Invoice exist; DC and GRN are completely absent)
    m_inv = client.post(f"/v1/documents/{inv_id}/match")
    assert m_inv.status_code == 200, m_inv.text
    assert m_inv.json()["match_status"] == "MATCHED"

    # Verify persisted relationships: exactly 1 CONFIRMED relationship (INV -> PO)
    with Session(get_engine()) as db:
        rels = db.execute(
            select(DocumentRelationship).where(
                DocumentRelationship.source_document_id == uuid.UUID(inv_id),
                DocumentRelationship.target_document_id == uuid.UUID(po_id),
            )
        ).scalars().all()
        assert len(rels) == 1
        assert rels[0].status == "CONFIRMED"

    # Reconcile without allow_missing_dc
    rec = client.post(f"/v1/documents/{po_id}/reconcile", json={"actor": "e2e_missing_checker", "allow_missing_dc": False})
    assert rec.status_code == 200, rec.text
    run = rec.json()
    assert run["status"] == "FAIL"

    discrepancy_types = {d["discrepancy_type"] for d in run["discrepancies"]}
    assert "MISSING_GRN" in discrepancy_types
    assert "MISSING_DC" in discrepancy_types

    missing_grn_d = next(d for d in run["discrepancies"] if d["discrepancy_type"] == "MISSING_GRN")
    assert missing_grn_d["severity"] == "CRITICAL"
    assert Decimal(str(missing_grn_d["amount_at_risk"])) == Decimal("100.0000")

    missing_dc_d = next(d for d in run["discrepancies"] if d["discrepancy_type"] == "MISSING_DC")
    assert missing_dc_d["severity"] == "WARNING"
    assert Decimal(str(missing_dc_d["amount_at_risk"])) == Decimal("0.0000")

    # Verify persisted reconciliation run in database
    with Session(get_engine()) as db:
        persisted = db.get(ReconciliationRun, uuid.UUID(run["id"]))
        assert persisted is not None
        assert persisted.status == "FAIL"
        disc_types = {d.discrepancy_type for d in persisted.discrepancies}
        assert "MISSING_GRN" in disc_types
        assert "MISSING_DC" in disc_types

    # Verify exceptions are generated for MISSING_GRN and MISSING_DC
    exc_resp = client.get("/v1/reconciliation-exceptions", params={"reconciliation_run_id": run["id"]})
    assert exc_resp.status_code == 200, exc_resp.text
    exceptions = exc_resp.json()
    exc_types = {e["exception_type"] for e in exceptions}
    assert "MISSING_GRN" in exc_types
    assert "MISSING_DC" in exc_types

    grn_exc = next(e for e in exceptions if e["exception_type"] == "MISSING_GRN")
    assert grn_exc["status"] == "OPEN"
    assert Decimal(str(grn_exc["financial_impact"])) == Decimal("100.0000")


def test_rejected_verification_resubmission_approval_and_matching(client):
    """Case 4: Rejected verification followed by resubmission, approval, and successful matching.

    Verifies rejection audit trail, rejection enforcement on matching,
    resubmission state transition, subsequent approval, confirmed relationship
    persistence, and successful reconciliation.
    """
    unique = _unique("REJ-RESUB")
    supplier_name = f"Resub Supplier {unique}"
    po_number = f"PO-{unique}"
    dc_number = f"DC-{unique}"

    po_fields = {
        "po_number": po_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    po_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]

    dc_fields = {
        "dc_number": dc_number,
        "po_reference": po_number,
        "supplier_name": supplier_name,
        "currency": "USD",
    }
    dc_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "dispatched_quantity": "10",
        }
    ]

    with Session(get_engine()) as db:
        supplier = Supplier(name=supplier_name, code=f"SUP-{unique}")
        db.add(supplier)
        db.flush()

        po_doc = _seed_verified_document(db, "PURCHASE_ORDER", po_number, supplier, po_fields, po_items, status="APPROVED")
        dc_doc = _seed_verified_document(db, "DELIVERY_CHALLAN", dc_number, supplier, dc_fields, dc_items, status="PENDING_REVIEW")
        db.commit()
        po_id = str(po_doc.id)
        dc_id = str(dc_doc.id)

    # 1. Verification is currently PENDING_REVIEW
    ver1 = _get_verification(client, dc_id)
    assert ver1["status"] == "PENDING_REVIEW"
    v_version = ver1["version"]

    # 2. Reject verification
    rej_resp = client.post(
        f"/v1/documents/{dc_id}/verification/reject",
        json={
            "reviewer_id": "auditor_e2e",
            "expected_version": v_version,
            "reason": "Blurry scan: dispatch count unreadable",
            "notes": "Please re-upload or provide clear proof",
        },
    )
    assert rej_resp.status_code == 200, rej_resp.text
    rejected_ver = rej_resp.json()
    assert rejected_ver["status"] == "REJECTED"
    assert rejected_ver["rejection_reason"] == "Blurry scan: dispatch count unreadable"
    assert rejected_ver["version"] == v_version + 1

    # Verify document status in DB is REJECTED
    with Session(get_engine()) as db:
        doc_in_db = db.get(Document, uuid.UUID(dc_id))
        assert doc_in_db.status == "REJECTED"

    # 3. Attempt matching on REJECTED document -> MUST FAIL with 422
    match_fail = client.post(f"/v1/documents/{dc_id}/match")
    assert match_fail.status_code == 422, match_fail.text
    assert "only APPROVED human-verified" in match_fail.json()["detail"]

    # Verify NO relationship was persisted
    with Session(get_engine()) as db:
        rels = db.execute(
            select(DocumentRelationship).where(DocumentRelationship.source_document_id == uuid.UUID(dc_id))
        ).scalars().all()
        assert rels == []

    # 4. Resubmit verification
    resub_resp = client.post(
        f"/v1/documents/{dc_id}/verification/resubmit",
        json={
            "actor_id": "operations_clerk",
            "expected_version": rejected_ver["version"],
            "reason": "Clear document image confirmed by warehouse supervisor",
        },
    )
    assert resub_resp.status_code == 200, resub_resp.text
    resub_ver = resub_resp.json()
    assert resub_ver["status"] == "PENDING_REVIEW"
    assert resub_ver["rejection_reason"] is None
    assert resub_ver["version"] == rejected_ver["version"] + 1

    # Verify document status in DB is INGESTED
    with Session(get_engine()) as db:
        doc_in_db = db.get(Document, uuid.UUID(dc_id))
        assert doc_in_db.status == "INGESTED"

    # 5. Approve verification
    appr_data = _approve_verification(client, dc_id, expected_version=resub_ver["version"], reviewer_id="auditor_lead")
    assert appr_data["status"] == "APPROVED"

    # Verify document status in DB is now VERIFIED
    with Session(get_engine()) as db:
        doc_in_db = db.get(Document, uuid.UUID(dc_id))
        assert doc_in_db.status == "VERIFIED"

    # 6. Match document -> MUST SUCCEED now
    match_ok = client.post(f"/v1/documents/{dc_id}/match")
    assert match_ok.status_code == 200, match_ok.text
    match_data = match_ok.json()
    assert match_data["match_status"] == "MATCHED"
    assert len(match_data["relationships"]) == 1
    assert match_data["relationships"][0]["status"] == "CONFIRMED"

    # Verify relationship persisted in DB
    with Session(get_engine()) as db:
        rels = db.execute(
            select(DocumentRelationship).where(
                DocumentRelationship.source_document_id == uuid.UUID(dc_id),
                DocumentRelationship.target_document_id == uuid.UUID(po_id),
            )
        ).scalars().all()
        assert len(rels) == 1
        assert rels[0].status == "CONFIRMED"

    # 7. Successful reconciliation on the approved transaction chain
    rec = client.post(f"/v1/documents/{po_id}/reconcile", json={"actor": "e2e_resub_verifier"})
    assert rec.status_code == 200, rec.text
    run = rec.json()
    assert run["status"] == "PASS"
    assert run["summary"]["discrepancy_count"] == 0
    assert set(run["reconciled_document_ids"]) == {po_id, dc_id}

