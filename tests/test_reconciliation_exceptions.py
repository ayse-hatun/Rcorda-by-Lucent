"""Focused tests for persistent reconciliation exception management."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timezone
from threading import Barrier
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.database import get_engine
from apps.api.main import app
from apps.api.models import (
    Document,
    DocumentExtraction,
    DocumentRelationship,
    DocumentVerification,
    ReconciliationDiscrepancy,
    ReconciliationException,
    ReconciliationExceptionHistory,
    ReconciliationRun,
    Supplier,
)
from apps.api.services.reconciliation_exception_service import (
    ReconciliationExceptionService,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _seed_reconciliation_case(discrepancy_specs=None) -> dict:
    discrepancy_specs = discrepancy_specs or [
        {
            "discrepancy_type": "OVER_BILLED_QUANTITY",
            "severity": "CRITICAL",
            "item_code": "BEAR-01",
            "line_item_description": "Precision Bearing",
            "amount_at_risk": Decimal("200.0000"),
            "explanation": "Invoice bills 12 units but only 10 were accepted.",
        }
    ]
    unique = uuid.uuid4().hex
    supplier = Supplier(name=f"Supplier {unique[:10]}", code=f"SUP-{unique[:16]}")
    purchase_order = Document(
        document_type="PURCHASE_ORDER",
        document_number=f"PO-{unique[:12]}",
        po_reference=f"PO-{unique[:12]}",
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )
    invoice = Document(
        document_type="SUPPLIER_INVOICE",
        document_number=f"INV-{unique[:12]}",
        po_reference=purchase_order.po_reference,
        supplier=supplier,
        sha256=uuid.uuid4().hex,
        status="VERIFIED",
    )

    engine = get_engine()
    with Session(engine) as db:
        db.add_all([supplier, purchase_order, invoice])
        db.flush()

        run = ReconciliationRun(
            root_document_id=invoice.id,
            po_document_id=purchase_order.id,
            status="FAIL",
            reconciled_document_ids=[
                str(purchase_order.id),
                str(invoice.id),
            ],
            summary={"discrepancy_count": len(discrepancy_specs)},
            created_by="TEST",
        )
        db.add(run)
        db.flush()

        discrepancies = []
        for spec in discrepancy_specs:
            discrepancy = ReconciliationDiscrepancy(
                run_id=run.id,
                discrepancy_type=spec["discrepancy_type"],
                severity=spec["severity"],
                item_code=spec.get("item_code"),
                line_item_description=spec.get("line_item_description"),
                expected_value=Decimal("10.0000"),
                actual_value=Decimal("12.0000"),
                delta_value=Decimal("2.0000"),
                currency="USD",
                amount_at_risk=spec["amount_at_risk"],
                source_evidence={"document_id": str(invoice.id)},
                explanation=spec["explanation"],
            )
            db.add(discrepancy)
            discrepancies.append(discrepancy)

        db.commit()
        return {
            "run_id": str(run.id),
            "discrepancy_ids": [str(d.id) for d in discrepancies],
            "po_id": str(purchase_order.id),
            "invoice_id": str(invoice.id),
            "supplier_name": supplier.name,
            "po_reference": purchase_order.po_reference,
            "run_status": run.status,
            "discrepancy_specs": discrepancy_specs,
        }


def _seed_reconcilable_chain() -> dict:
    unique = uuid.uuid4().hex
    supplier = Supplier(name=f"Chain Supplier {unique[:8]}", code=f"CHAIN-{unique[:16]}")
    po_reference = f"PO-{unique[:12]}"

    purchase_order = Document(
        document_type="PURCHASE_ORDER",
        document_number=po_reference,
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

    po_fields = {
        "po_number": po_reference,
        "supplier_name": supplier.name,
        "currency": "USD",
        "subtotal": "100.00",
        "grand_total": "100.00",
    }
    grn_fields = {
        "grn_number": grn.document_number,
        "po_reference": po_reference,
        "supplier_name": supplier.name,
        "currency": "USD",
    }
    invoice_fields = {
        "invoice_number": invoice.document_number,
        "po_reference": po_reference,
        "supplier_name": supplier.name,
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
    grn_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "received_quantity": "8",
            "accepted_quantity": "8",
            "rejected_quantity": "0",
        }
    ]
    invoice_items = [
        {
            "item_code": "BEAR-01",
            "item_description": "Precision Bearing",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ]

    engine = get_engine()
    with Session(engine) as db:
        db.add_all([supplier, purchase_order, grn, invoice])
        db.flush()

        documents = [
            (purchase_order, "PURCHASE_ORDER", po_fields, po_items),
            (grn, "GOODS_RECEIPT_NOTE", grn_fields, grn_items),
            (invoice, "SUPPLIER_INVOICE", invoice_fields, invoice_items),
        ]
        for document, document_type, fields, line_items in documents:
            extraction = DocumentExtraction(
                document_id=document.id,
                run_number=1,
                document_type=document_type,
                extractor_version="1.0.0",
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
                    status="APPROVED",
                    verified_fields=fields,
                    verified_line_items=line_items,
                    reviewed_by="TEST",
                    reviewed_at=datetime.now(timezone.utc),
                )
            )

        db.add_all(
            [
                DocumentRelationship(
                    source_document_id=grn.id,
                    target_document_id=purchase_order.id,
                    relationship_type="GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER",
                    status="CONFIRMED",
                    matching_method="AUTOMATIC_EXPLICIT_REFERENCE",
                    confidence_score=1.0,
                    created_by="TEST",
                ),
                DocumentRelationship(
                    source_document_id=invoice.id,
                    target_document_id=purchase_order.id,
                    relationship_type="SUPPLIER_INVOICE_TO_PURCHASE_ORDER",
                    status="CONFIRMED",
                    matching_method="AUTOMATIC_EXPLICIT_REFERENCE",
                    confidence_score=1.0,
                    created_by="TEST",
                ),
            ]
        )
        db.commit()

        return {
            "po_id": str(purchase_order.id),
            "supplier_name": supplier.name,
            "po_reference": po_reference,
        }


def _create_one_exception(client: TestClient, case: dict, actor: str = "TEST") -> dict:
    response = client.post(
        "/v1/reconciliation-exceptions/from-discrepancy",
        json={"discrepancy_id": case["discrepancy_ids"][0], "actor": actor},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_create_exceptions_from_run_is_idempotent(client):
    case = _seed_reconciliation_case(
        [
            {
                "discrepancy_type": "OVER_BILLED_QUANTITY",
                "severity": "CRITICAL",
                "item_code": "BEAR-01",
                "line_item_description": "Precision Bearing",
                "amount_at_risk": Decimal("200.0000"),
                "explanation": "Invoice bills 12 units but only 10 were accepted.",
            },
            {
                "discrepancy_type": "PRICE_VARIANCE",
                "severity": "WARNING",
                "item_code": "VALVE-02",
                "line_item_description": "Hydraulic Valve",
                "amount_at_risk": Decimal("250.0000"),
                "explanation": "Invoice unit rate exceeds the purchase order rate.",
            },
        ]
    )

    first = client.post(
        f"/v1/reconciliation-exceptions/from-run/{case['run_id']}",
        json={"actor": "RECON_ENGINE"},
    )
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert len(first_payload) == 2

    by_discrepancy = {item["discrepancy_id"]: item for item in first_payload}
    critical = by_discrepancy[case["discrepancy_ids"][0]]
    assert critical["exception_type"] == "OVER_BILLED_QUANTITY"
    assert critical["severity"] == "CRITICAL"
    assert critical["description"] == (
        "Invoice bills 12 units but only 10 were accepted."
    )
    assert critical["supplier_name"] == case["supplier_name"]
    assert critical["po_document_id"] == case["po_id"]
    assert critical["po_reference"] == case["po_reference"]
    assert set(critical["related_document_ids"]) == {
        case["po_id"],
        case["invoice_id"],
    }
    assert Decimal(str(critical["financial_impact"])) == Decimal("200.0000")
    assert critical["currency"] == "USD"
    assert critical["status"] == "OPEN"
    assert critical["reconciliation_run_id"] == case["run_id"]
    assert critical["created_by"] == "RECON_ENGINE"
    assert critical["created_at"] is not None
    assert critical["history"][0]["action"] == "CREATED"

    second = client.post(
        f"/v1/reconciliation-exceptions/from-run/{case['run_id']}",
        json={"actor": "RECON_ENGINE"},
    )
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert {item["id"] for item in second_payload} == {
        item["id"] for item in first_payload
    }

    duplicate = client.post(
        "/v1/reconciliation-exceptions/from-discrepancy",
        json={"discrepancy_id": case["discrepancy_ids"][0], "actor": "OTHER"},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == critical["id"]

    with Session(get_engine()) as db:
        count = db.execute(
            select(func.count())
            .select_from(ReconciliationException)
            .where(
                ReconciliationException.reconciliation_run_id
                == uuid.UUID(case["run_id"])
            )
        ).scalar_one()
        assert count == 2

        run = db.get(ReconciliationRun, uuid.UUID(case["run_id"]))
        assert run.status == case["run_status"]
        assert len(run.discrepancies) == 2
        assert run.discrepancies[0].explanation == (
            "Invoice bills 12 units but only 10 were accepted."
        )


def test_exception_lifecycle_resolution_and_history(client):
    case = _seed_reconciliation_case()
    created = _create_one_exception(client, case)
    exception_id = created["id"]
    base_url = f"/v1/reconciliation-exceptions/{exception_id}"

    invalid_direct = client.patch(
        f"{base_url}/status",
        json={"status": "RESOLVED", "actor": "REVIEWER"},
    )
    assert invalid_direct.status_code == 409

    invalid_status = client.patch(
        f"{base_url}/status",
        json={"status": "BOGUS", "actor": "REVIEWER"},
    )
    assert invalid_status.status_code == 422

    empty_resolution = client.post(
        f"{base_url}/resolve",
        json={"resolution_notes": "", "actor": "FINANCE"},
    )
    assert empty_resolution.status_code == 422

    under_review = client.patch(
        f"{base_url}/status",
        json={
            "status": "UNDER_REVIEW",
            "actor": "REVIEWER",
            "note": "Investigating with procurement.",
        },
    )
    assert under_review.status_code == 200, under_review.text
    assert under_review.json()["status"] == "UNDER_REVIEW"

    resolved = client.post(
        f"{base_url}/resolve",
        json={
            "resolution_notes": "Credit note CN-1 applied to the invoice.",
            "supporting_information": {"credit_note": "CN-1"},
            "actor": "FINANCE",
        },
    )
    assert resolved.status_code == 200, resolved.text
    resolved_payload = resolved.json()
    assert resolved_payload["status"] == "RESOLVED"
    assert resolved_payload["resolution_notes"] == (
        "Credit note CN-1 applied to the invoice."
    )
    assert resolved_payload["resolution_supporting_information"] == {
        "credit_note": "CN-1"
    }
    assert resolved_payload["resolved_by"] == "FINANCE"
    assert resolved_payload["resolved_at"] is not None

    second_resolve = client.post(
        f"{base_url}/resolve",
        json={"resolution_notes": "Duplicate resolution.", "actor": "FINANCE"},
    )
    assert second_resolve.status_code == 409

    reopened = client.patch(
        f"{base_url}/status",
        json={
            "status": "REOPENED",
            "actor": "FINANCE",
            "note": "Supplier disputed the credit note.",
        },
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "REOPENED"
    assert reopened.json()["resolved_at"] is None

    re_resolved = client.post(
        f"{base_url}/resolve",
        json={
            "resolution_notes": "Revised credit note CN-2 applied.",
            "supporting_information": {"credit_note": "CN-2"},
            "actor": "FINANCE",
        },
    )
    assert re_resolved.status_code == 200, re_resolved.text
    assert re_resolved.json()["status"] == "RESOLVED"

    history_response = client.get(f"{base_url}/history")
    assert history_response.status_code == 200, history_response.text
    history = history_response.json()
    actions = [entry["action"] for entry in history]
    assert actions == [
        "CREATED",
        "STATUS_CHANGED",
        "RESOLVED",
        "REOPENED",
        "RESOLVED",
    ]
    assert [
        (entry["from_status"], entry["to_status"]) for entry in history
    ] == [
        (None, "OPEN"),
        ("OPEN", "UNDER_REVIEW"),
        ("UNDER_REVIEW", "RESOLVED"),
        ("RESOLVED", "REOPENED"),
        ("REOPENED", "RESOLVED"),
    ]
    assert history[0]["note"] == case["discrepancy_specs"][0]["explanation"]
    assert history[1]["actor_id"] == "REVIEWER"
    assert history[1]["note"] == "Investigating with procurement."
    assert history[2]["actor_id"] == "FINANCE"
    assert history[2]["note"] == "Credit note CN-1 applied to the invoice."
    assert history[3]["note"] == "Supplier disputed the credit note."
    assert history[4]["note"] == "Revised credit note CN-2 applied."
    assert history[4]["details"] == {"credit_note": "CN-2"}

    detail = client.get(base_url)
    assert detail.status_code == 200, detail.text
    assert [entry["action"] for entry in detail.json()["history"]] == actions


def test_exception_rejection_and_reopen_transitions(client):
    case = _seed_reconciliation_case()
    created = _create_one_exception(client, case)
    exception_id = created["id"]
    base_url = f"/v1/reconciliation-exceptions/{exception_id}"

    direct_reject = client.patch(
        f"{base_url}/status",
        json={"status": "REJECTED", "actor": "REVIEWER"},
    )
    assert direct_reject.status_code == 409

    assert (
        client.patch(
            f"{base_url}/status",
            json={"status": "UNDER_REVIEW", "actor": "REVIEWER"},
        ).status_code
        == 200
    )

    rejected = client.patch(
        f"{base_url}/status",
        json={
            "status": "REJECTED",
            "actor": "REVIEWER",
            "note": "Discrepancy is within agreed contract tolerance.",
        },
    )
    assert rejected.status_code == 200, rejected.text
    rejected_payload = rejected.json()
    assert rejected_payload["status"] == "REJECTED"
    assert rejected_payload["resolved_by"] == "REVIEWER"
    assert rejected_payload["resolved_at"] is not None
    assert rejected_payload["resolution_notes"] == (
        "Discrepancy is within agreed contract tolerance."
    )

    invalid_review = client.patch(
        f"{base_url}/status",
        json={"status": "UNDER_REVIEW", "actor": "REVIEWER"},
    )
    assert invalid_review.status_code == 409

    reopened = client.patch(
        f"{base_url}/status",
        json={"status": "REOPENED", "actor": "AUDITOR"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "REOPENED"

    under_review_again = client.patch(
        f"{base_url}/status",
        json={"status": "UNDER_REVIEW", "actor": "REVIEWER"},
    )
    assert under_review_again.status_code == 200, under_review_again.text

    resolved_via_status = client.patch(
        f"{base_url}/status",
        json={
            "status": "RESOLVED",
            "actor": "FINANCE",
            "note": "Tolerance confirmed and case closed.",
        },
    )
    assert resolved_via_status.status_code == 200, resolved_via_status.text
    assert resolved_via_status.json()["status"] == "RESOLVED"


def test_exception_persistence_filters_and_immutable_history(client):
    case = _seed_reconciliation_case()
    created = _create_one_exception(client, case, actor="REVIEWER")
    exception_id = created["id"]

    with Session(get_engine()) as db:
        stored = db.get(ReconciliationException, uuid.UUID(exception_id))
        assert stored is not None
        assert stored.status == "OPEN"
        assert len(stored.history) == 1
        created_history_id = stored.history[0].id
        created_note = stored.history[0].note

    assert (
        client.patch(
            f"/v1/reconciliation-exceptions/{exception_id}/status",
            json={"status": "UNDER_REVIEW", "actor": "REVIEWER"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/reconciliation-exceptions/{exception_id}/resolve",
            json={
                "resolution_notes": "Supplier issued a corrective invoice.",
                "actor": "FINANCE",
            },
        ).status_code
        == 200
    )

    with Session(get_engine()) as db:
        stored = db.get(ReconciliationException, uuid.UUID(exception_id))
        assert stored.status == "RESOLVED"
        assert stored.resolution_notes == "Supplier issued a corrective invoice."
        assert stored.resolved_by == "FINANCE"
        assert stored.resolved_at is not None
        assert stored.created_at is not None
        assert stored.updated_at is not None

        history = (
            db.execute(
                select(ReconciliationExceptionHistory)
                .where(
                    ReconciliationExceptionHistory.exception_id
                    == uuid.UUID(exception_id)
                )
                .order_by(ReconciliationExceptionHistory.created_at.asc())
            )
            .scalars()
            .all()
        )
        assert len(history) == 3
        assert history[0].id == created_history_id
        assert history[0].note == created_note
        assert history[0].from_status is None
        assert history[0].to_status == "OPEN"
        assert history[1].action == "STATUS_CHANGED"
        assert history[2].action == "RESOLVED"

        discrepancy = db.get(
            ReconciliationDiscrepancy,
            uuid.UUID(case["discrepancy_ids"][0]),
        )
        assert discrepancy.explanation == case["discrepancy_specs"][0]["explanation"]
        assert discrepancy.amount_at_risk == Decimal("200.0000")

        run = db.get(ReconciliationRun, uuid.UUID(case["run_id"]))
        assert run.status == case["run_status"]
        assert run.reconciled_document_ids == [
            case["po_id"],
            case["invoice_id"],
        ]

    filtered = client.get(
        "/v1/reconciliation-exceptions",
        params={
            "status": "RESOLVED",
            "severity": "CRITICAL",
            "reconciliation_run_id": case["run_id"],
        },
    )
    assert filtered.status_code == 200, filtered.text
    filtered_payload = filtered.json()
    assert len(filtered_payload) == 1
    assert filtered_payload[0]["id"] == exception_id

    by_supplier = client.get(
        "/v1/reconciliation-exceptions",
        params={"supplier_name": case["supplier_name"]},
    )
    assert by_supplier.status_code == 200, by_supplier.text
    assert {item["id"] for item in by_supplier.json()} == {exception_id}

    missing = client.get(f"/v1/reconciliation-exceptions/{uuid.uuid4()}")
    assert missing.status_code == 404


def test_reconciliation_auto_creates_exceptions(client):
    chain = _seed_reconcilable_chain()

    response = client.post(
        f"/v1/documents/{chain['po_id']}/reconcile",
        json={"allow_missing_dc": True, "actor": "RECON_ENGINE"},
    )
    assert response.status_code == 200, response.text
    run_payload = response.json()
    assert run_payload["status"] == "FAIL"

    discrepancy_types = {
        item["discrepancy_type"] for item in run_payload["discrepancies"]
    }
    assert "OVER_BILLED_QUANTITY" in discrepancy_types

    exceptions_response = client.get(
        "/v1/reconciliation-exceptions",
        params={"reconciliation_run_id": run_payload["id"]},
    )
    assert exceptions_response.status_code == 200, exceptions_response.text
    exceptions = exceptions_response.json()
    assert len(exceptions) == len(run_payload["discrepancies"])
    assert {item["exception_type"] for item in exceptions} == discrepancy_types

    overbilled = next(
        item
        for item in exceptions
        if item["exception_type"] == "OVER_BILLED_QUANTITY"
    )
    assert overbilled["supplier_name"] == chain["supplier_name"]
    assert overbilled["po_reference"] == chain["po_reference"]
    assert overbilled["status"] == "OPEN"
    assert overbilled["created_by"] == "RECON_ENGINE"
    assert overbilled["history"][0]["action"] == "CREATED"


def test_database_unique_constraint_rejects_duplicate_discrepancy_exception():
    case = _seed_reconciliation_case()
    discrepancy_id = uuid.UUID(case["discrepancy_ids"][0])
    run_id = uuid.UUID(case["run_id"])

    with Session(get_engine()) as db:
        db.add(
            ReconciliationException(
                discrepancy_id=discrepancy_id,
                reconciliation_run_id=run_id,
                exception_type="OVER_BILLED_QUANTITY",
                severity="CRITICAL",
                description="First exception for the discrepancy.",
                created_by="TEST",
            )
        )
        db.commit()

        db.add(
            ReconciliationException(
                discrepancy_id=discrepancy_id,
                reconciliation_run_id=run_id,
                exception_type="OVER_BILLED_QUANTITY",
                severity="CRITICAL",
                description="Duplicate exception for the discrepancy.",
                created_by="TEST",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_concurrent_creation_returns_single_exception():
    case = _seed_reconciliation_case()
    discrepancy_id = uuid.UUID(case["discrepancy_ids"][0])
    barrier = Barrier(2)

    def create_exception() -> str:
        barrier.wait(timeout=15)
        with Session(get_engine()) as db:
            exception = ReconciliationExceptionService(db).create_from_discrepancy(
                discrepancy_id=discrepancy_id,
                actor="CONCURRENT",
            )
            return str(exception.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create_exception) for _ in range(2)]
        exception_ids = [future.result(timeout=60) for future in futures]

    assert len(set(exception_ids)) == 1

    with Session(get_engine()) as db:
        count = db.execute(
            select(func.count())
            .select_from(ReconciliationException)
            .where(ReconciliationException.discrepancy_id == discrepancy_id)
        ).scalar_one()
        assert count == 1


def test_run_creation_does_not_swallow_non_duplicate_integrity_error(monkeypatch):
    case = _seed_reconciliation_case()
    original_build = ReconciliationExceptionService._build_exception

    def build_invalid_exception(self, discrepancy, run, actor):
        exception = original_build(self, discrepancy, run, actor)
        exception.description = None
        return exception

    monkeypatch.setattr(
        ReconciliationExceptionService,
        "_build_exception",
        build_invalid_exception,
    )

    with Session(get_engine()) as db:
        with pytest.raises(IntegrityError):
            ReconciliationExceptionService(db).create_from_reconciliation_run(
                reconciliation_run_id=uuid.UUID(case["run_id"]),
                actor="TEST",
            )

    with Session(get_engine()) as db:
        count = db.execute(
            select(func.count())
            .select_from(ReconciliationException)
            .where(
                ReconciliationException.reconciliation_run_id
                == uuid.UUID(case["run_id"])
            )
        ).scalar_one()
        assert count == 0


def test_reconciliation_projection_failure_does_not_break_reconciliation(
    client, monkeypatch
):
    chain = _seed_reconcilable_chain()

    def fail_projection(self, reconciliation_run_id, actor="SYSTEM"):
        raise SQLAlchemyError("simulated exception projection failure")

    monkeypatch.setattr(
        ReconciliationExceptionService,
        "create_from_reconciliation_run",
        fail_projection,
    )

    response = client.post(
        f"/v1/documents/{chain['po_id']}/reconcile",
        json={"allow_missing_dc": True, "actor": "RECON_ENGINE"},
    )
    assert response.status_code == 200, response.text
    run_id = uuid.UUID(response.json()["id"])

    with Session(get_engine()) as db:
        assert db.get(ReconciliationRun, run_id) is not None
        count = db.execute(
            select(func.count())
            .select_from(ReconciliationException)
            .where(ReconciliationException.reconciliation_run_id == run_id)
        ).scalar_one()
        assert count == 0


def test_repeated_transitions_rejected_and_status_resolution_persists_support(client):
    case = _seed_reconciliation_case()
    created = _create_one_exception(client, case)
    exception_id = created["id"]
    base_url = f"/v1/reconciliation-exceptions/{exception_id}"

    assert (
        client.patch(
            f"{base_url}/status",
            json={"status": "UNDER_REVIEW", "actor": "REVIEWER"},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"{base_url}/status",
            json={"status": "UNDER_REVIEW", "actor": "REVIEWER"},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"{base_url}/status",
            json={"status": "OPEN", "actor": "REVIEWER"},
        ).status_code
        == 409
    )

    resolved = client.patch(
        f"{base_url}/status",
        json={
            "status": "RESOLVED",
            "actor": "FINANCE",
            "note": "Approved by finance controller.",
            "supporting_information": {"approval": "APR-1"},
        },
    )
    assert resolved.status_code == 200, resolved.text
    payload = resolved.json()
    assert payload["status"] == "RESOLVED"
    assert payload["resolution_notes"] == "Approved by finance controller."
    assert payload["resolution_supporting_information"] == {"approval": "APR-1"}
    assert payload["resolved_by"] == "FINANCE"
    assert payload["resolved_at"] is not None

    assert (
        client.patch(
            f"{base_url}/status",
            json={"status": "RESOLVED", "actor": "FINANCE"},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"{base_url}/status",
            json={"status": "UNDER_REVIEW", "actor": "REVIEWER"},
        ).status_code
        == 409
    )

    history = client.get(f"{base_url}/history").json()
    assert history[-1]["action"] == "RESOLVED"
    assert history[-1]["details"] == {"approval": "APR-1"}
    assert history[-1]["note"] == "Approved by finance controller."

    with Session(get_engine()) as db:
        stored = db.get(ReconciliationException, uuid.UUID(exception_id))
        assert stored.resolution_notes == "Approved by finance controller."
        assert stored.resolution_supporting_information == {"approval": "APR-1"}
        assert stored.resolved_by == "FINANCE"
        assert stored.resolved_at is not None
