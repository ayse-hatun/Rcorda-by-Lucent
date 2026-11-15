"""PostgreSQL integration tests for Recorda Human Verification & Review Workflow.

Verifies:
1. Verification session initialization cloning immutable extraction data.
2. Review assignment lifecycle (claim, release).
3. Field-level corrections with complete immutable audit trail.
4. Line-item corrections (update, add, delete) with audit trail.
5. Concurrency protection with optimistic locking (409 Conflict on stale expected_version).
6. State machine enforcement (terminal states, prohibited transitions).
7. Document and ExtractedDocumentData synchronization upon approval.
8. Rejection and resubmission workflow lifecycle.
9. USD-only and non-negative monetary constraints.
10. Cross-session persistence in PostgreSQL.
11. Handling of missing documents and extractions.
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
    DocumentVerification,
    ExtractedDocumentData,
    OcrPageResult,
    OcrRun,
    VerificationCorrection,
)
from apps.api.routes.documents import get_extraction_service, get_ocr_service
from apps.api.routes.verifications import get_verification_service
from apps.api.services.extraction_service import DocumentExtractionService
from apps.api.services.ocr_service import DocumentOcrService
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
        return DocumentOcrService(
            db=db,
            storage=isolated_storage,
            engine=DeterministicOcrEngine(),
        )

    def _override_extraction_service(db: Session = Depends(get_db)):
        return DocumentExtractionService(db=db)

    def _override_verification_service(db: Session = Depends(get_db)):
        return DocumentVerificationService(db=db)

    app.dependency_overrides[get_ocr_service] = _override_ocr_service
    app.dependency_overrides[get_extraction_service] = _override_extraction_service
    app.dependency_overrides[get_verification_service] = _override_verification_service
    yield TestClient(app)
    app.dependency_overrides.pop(get_ocr_service, None)
    app.dependency_overrides.pop(get_extraction_service, None)
    app.dependency_overrides.pop(get_verification_service, None)


def _upload_test_pdf(client, title: str, doc_type: str = "PURCHASE_ORDER") -> dict:
    """Helper to upload a valid PDF document with a unique checksum."""
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({
        "/Title": title,
        "/Producer": f"RecordaVerificationTest-{uuid.uuid4()}",
    })
    buf = io.BytesIO()
    writer.write(buf)

    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": doc_type},
        files={"file": (f"test_{uuid.uuid4().hex[:8]}.pdf", buf.getvalue(), "application/pdf")},
    )
    assert resp.status_code == 201
    return resp.json()


def _setup_extracted_document(client, doc_type: str = "PURCHASE_ORDER") -> dict:
    """Create a document with completed OCR and structured extraction."""
    doc = _upload_test_pdf(client, f"Verification Doc {uuid.uuid4()}", doc_type=doc_type)
    doc_id = doc["id"]

    engine = get_engine()
    with Session(engine) as db:
        sample_ocr = (
            "PURCHASE ORDER\n"
            "PO Number: PO-2026-9001\n"
            "Vendor: Acme Widgets Corp\n"
            "Order Date: 2026-03-15\n"
            "Currency: USD\n\n"
            "Line Items:\n"
            "ITEM-01 Precision Bearings 10 pcs @ 50.00 500.00\n\n"
            "Subtotal: 500.00\n"
            "Grand Total: 500.00\n"
        )
        ocr_run = OcrRun(
            document_id=uuid.UUID(doc_id),
            run_number=1,
            status="COMPLETED",
            ocr_engine="DeterministicTestEngine",
            ocr_version="1.0.0",
            total_pages=1,
            full_text=sample_ocr,
            confidence_score=0.98,
        )
        db.add(ocr_run)
        db.flush()

        page = OcrPageResult(
            ocr_run_id=ocr_run.id,
            document_id=uuid.UUID(doc_id),
            page_number=1,
            raw_text=sample_ocr,
            confidence_score=0.98,
        )
        db.add(page)
        db.commit()

    # Trigger extraction
    ext_resp = client.post(f"/v1/documents/{doc_id}/extract")
    assert ext_resp.status_code == 200
    ext_data = ext_resp.json()
    assert ext_data["status"] in ("COMPLETED", "REVIEW_REQUIRED")

    return {"doc_id": doc_id, "extraction": ext_data}


def test_initialization_clones_immutable_extraction(client):
    """Test verification initialization creates session and preserves original extraction."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]
    ext_id = setup["extraction"]["id"]

    # Initialize verification
    resp = client.get(f"/v1/documents/{doc_id}/verification")
    assert resp.status_code == 200
    v_data = resp.json()
    assert v_data["document_id"] == doc_id
    assert v_data["extraction_id"] == ext_id
    assert v_data["status"] == "PENDING_REVIEW"
    assert v_data["version"] == 1
    assert v_data["verified_fields"]["po_number"] == "PO-2026-9001"
    assert v_data["verified_fields"]["grand_total"] == "500.00"
    assert len(v_data["verified_line_items"]) == 1

    # Calling get again returns the same session without recreating
    resp2 = client.get(f"/v1/documents/{doc_id}/verification")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == v_data["id"]
    assert resp2.json()["version"] == 1


def test_claim_and_release_review_workflow(client):
    """Test claiming and releasing a review session."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]

    # Claim review
    claim_resp = client.post(
        f"/v1/documents/{doc_id}/verification/claim",
        json={"reviewer_id": "reviewer_alice", "expected_version": 1},
    )
    assert claim_resp.status_code == 200
    claimed = claim_resp.json()
    assert claimed["status"] == "IN_REVIEW"
    assert claimed["assigned_reviewer_id"] == "reviewer_alice"
    assert claimed["version"] == 2

    # Release claim
    release_resp = client.post(
        f"/v1/documents/{doc_id}/verification/release",
        json={"reviewer_id": "reviewer_alice", "expected_version": 2},
    )
    assert release_resp.status_code == 200
    released = release_resp.json()
    assert released["status"] == "PENDING_REVIEW"
    assert released["assigned_reviewer_id"] is None
    assert released["version"] == 3


def test_field_corrections_and_immutable_audit_history(client):
    """Test modifying a field records exact audit history without touching original extraction."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]
    ext_id = setup["extraction"]["id"]

    # Apply field correction
    corr_resp = client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "grand_total",
            "corrected_value": "550.00",
            "corrected_by": "auditor_1",
            "expected_version": 1,
            "reason": "Corrected line item price discrepancy from physical document",
        },
    )
    assert corr_resp.status_code == 200
    corrected = corr_resp.json()
    assert corrected["version"] == 2
    assert corrected["verified_fields"]["grand_total"] == "550.00"

    # Query audit history
    audit_resp = client.get(f"/v1/documents/{doc_id}/verification/corrections")
    assert audit_resp.status_code == 200
    corrections = audit_resp.json()
    assert len(corrections) == 1
    audit = corrections[0]
    assert audit["correction_type"] == "FIELD"
    assert audit["field_name"] == "grand_total"
    assert audit["original_value"] == "500.00"
    assert audit["corrected_value"] == "550.00"
    assert audit["corrected_by"] == "auditor_1"
    assert "discrepancy" in audit["reason"]

    # Verify original DocumentExtraction was NOT mutated
    engine = get_engine()
    with Session(engine) as db:
        original_ext = db.get(DocumentExtraction, uuid.UUID(ext_id))
        assert original_ext.fields["grand_total"] == "500.00"


def test_line_item_lifecycle_corrections(client):
    """Test line item update, addition, and deletion with audit tracking."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]

    # 1. Update line item 0
    upd_resp = client.post(
        f"/v1/documents/{doc_id}/verification/correct-line-item",
        json={
            "line_item_index": 0,
            "item_updates": {"quantity": "12", "line_total": "600.00"},
            "corrected_by": "auditor_1",
            "expected_version": 1,
            "reason": "Adjusted quantity to match packing slip",
        },
    )
    assert upd_resp.status_code == 200
    res_upd = upd_resp.json()
    assert res_upd["version"] == 2
    assert res_upd["verified_line_items"][0]["quantity"] == "12"
    assert res_upd["verified_line_items"][0]["line_total"] == "600.00"

    # 2. Add a new line item
    add_resp = client.post(
        f"/v1/documents/{doc_id}/verification/add-line-item",
        json={
            "line_item": {
                "item_code": "ITEM-EXP-2",
                "description": "Extra Bracket",
                "quantity": "2",
                "unit_price": "25.00",
                "line_total": "50.00",
            },
            "corrected_by": "auditor_1",
            "expected_version": 2,
            "reason": "Missing secondary line item on page 1",
        },
    )
    assert add_resp.status_code == 200
    res_add = add_resp.json()
    assert res_add["version"] == 3
    assert len(res_add["verified_line_items"]) == 2

    # 3. Delete original line item (index 0)
    del_resp = client.post(
        f"/v1/documents/{doc_id}/verification/delete-line-item",
        json={
            "line_item_index": 0,
            "corrected_by": "auditor_1",
            "expected_version": 3,
            "reason": "Removed canceled item line",
        },
    )
    assert del_resp.status_code == 200
    res_del = del_resp.json()
    assert res_del["version"] == 4
    assert len(res_del["verified_line_items"]) == 1
    assert res_del["verified_line_items"][0]["item_code"] == "ITEM-EXP-2"

    # 4. Verify all 3 operations recorded in correction audit trail
    audit_resp = client.get(f"/v1/documents/{doc_id}/verification/corrections")
    assert audit_resp.status_code == 200
    corrections = audit_resp.json()
    assert len(corrections) == 3
    types = [c["correction_type"] for c in corrections]
    assert types == ["LINE_ITEM_UPDATE", "LINE_ITEM_ADD", "LINE_ITEM_DELETE"]


def test_optimistic_concurrency_conflict_rejection(client):
    """Test optimistic concurrency control prevents lost updates."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]

    # Client A updates grand_total at version 1 -> succeeds, version becomes 2
    resp_a = client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "grand_total",
            "corrected_value": "600.00",
            "corrected_by": "client_a",
            "expected_version": 1,
        },
    )
    assert resp_a.status_code == 200
    assert resp_a.json()["version"] == 2

    # Client B (stale) attempts update with stale expected_version=1 -> 409 Conflict
    resp_b = client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "grand_total",
            "corrected_value": "700.00",
            "corrected_by": "client_b",
            "expected_version": 1,
        },
    )
    assert resp_b.status_code == 409
    assert "Concurrent modification detected" in resp_b.json()["detail"]

    # Client B refreshes and retries with expected_version=2 -> succeeds, version becomes 3
    resp_b_retry = client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "grand_total",
            "corrected_value": "700.00",
            "corrected_by": "client_b",
            "expected_version": 2,
        },
    )
    assert resp_b_retry.status_code == 200
    assert resp_b_retry.json()["version"] == 3
    assert resp_b_retry.json()["verified_fields"]["grand_total"] == "700.00"


def test_approval_workflow_and_document_synchronization(client):
    """Test approval locks document and synchronizes verified values to Document model."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]

    # Perform a correction on PO number
    client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "po_number",
            "corrected_value": "PO-2026-FINAL",
            "corrected_by": "auditor_bob",
            "expected_version": 1,
        },
    )

    # Approve verification
    appr_resp = client.post(
        f"/v1/documents/{doc_id}/verification/approve",
        json={
            "reviewer_id": "auditor_bob",
            "expected_version": 2,
            "notes": "Verified against physical paper stamp",
        },
    )
    assert appr_resp.status_code == 200
    appr_data = appr_resp.json()
    assert appr_data["status"] == "APPROVED"
    assert appr_data["reviewed_by"] == "auditor_bob"
    assert appr_data["version"] == 3

    # Check Document entity and ExtractedDocumentData updated
    engine = get_engine()
    with Session(engine) as db:
        doc = db.get(Document, uuid.UUID(doc_id))
        assert doc.status == "VERIFIED"
        assert doc.document_number == "PO-2026-FINAL"
        assert doc.extracted_data.fields["po_number"] == "PO-2026-FINAL"

    # Attempting modification on APPROVED verification fails with 409 Conflict
    locked_edit = client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "grand_total",
            "corrected_value": "999.00",
            "corrected_by": "auditor_bob",
            "expected_version": 3,
        },
    )
    assert locked_edit.status_code == 409
    assert "terminal state 'APPROVED'" in locked_edit.json()["detail"]


def test_rejection_and_resubmission_lifecycle(client):
    """Test rejection with mandatory reason and resubmission workflow."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]

    # Reject verification
    rej_resp = client.post(
        f"/v1/documents/{doc_id}/verification/reject",
        json={
            "reviewer_id": "auditor_alice",
            "expected_version": 1,
            "reason": "Missing signatures and illegible item table",
            "notes": "Requested re-scan from supplier",
        },
    )
    assert rej_resp.status_code == 200
    rej_data = rej_resp.json()
    assert rej_data["status"] == "REJECTED"
    assert rej_data["rejection_reason"] == "Missing signatures and illegible item table"
    assert rej_data["version"] == 2

    # Verify Document status updated to REJECTED
    engine = get_engine()
    with Session(engine) as db:
        doc = db.get(Document, uuid.UUID(doc_id))
        assert doc.status == "REJECTED"

    # Cannot approve while REJECTED
    invalid_approve = client.post(
        f"/v1/documents/{doc_id}/verification/approve",
        json={"reviewer_id": "auditor_alice", "expected_version": 2},
    )
    assert invalid_approve.status_code == 409
    assert "Cannot approve" in invalid_approve.json()["detail"]

    # Resubmit verification
    resub_resp = client.post(
        f"/v1/documents/{doc_id}/verification/resubmit",
        json={
            "actor_id": "store_manager",
            "expected_version": 2,
            "reason": "Re-scanned document attached with clear resolution",
        },
    )
    assert resub_resp.status_code == 200
    resub_data = resub_resp.json()
    assert resub_data["status"] == "PENDING_REVIEW"
    assert resub_data["rejection_reason"] is None
    assert resub_data["version"] == 3

    # Document status reverted to INGESTED
    with Session(engine) as db:
        doc = db.get(Document, uuid.UUID(doc_id))
        assert doc.status == "INGESTED"

    # Now approval is allowed again
    approve_again = client.post(
        f"/v1/documents/{doc_id}/verification/approve",
        json={"reviewer_id": "auditor_alice", "expected_version": 3},
    )
    assert approve_again.status_code == 200
    assert approve_again.json()["status"] == "APPROVED"


def test_usd_only_and_monetary_constraints(client):
    """Test validation blocks unsupported currencies and negative amounts."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]

    # Reject non-USD currency
    resp_eur = client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "currency",
            "corrected_value": "EUR",
            "corrected_by": "auditor_1",
            "expected_version": 1,
        },
    )
    assert resp_eur.status_code == 422
    assert "USD only" in resp_eur.json()["detail"]

    # Reject negative monetary values
    resp_neg = client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "grand_total",
            "corrected_value": "-50.00",
            "corrected_by": "auditor_1",
            "expected_version": 1,
        },
    )
    assert resp_neg.status_code == 422
    assert "cannot be negative" in resp_neg.json()["detail"]

    # Reject negative line item quantity
    resp_neg_qty = client.post(
        f"/v1/documents/{doc_id}/verification/correct-line-item",
        json={
            "line_item_index": 0,
            "item_updates": {"quantity": "-5"},
            "corrected_by": "auditor_1",
            "expected_version": 1,
        },
    )
    assert resp_neg_qty.status_code == 422
    assert "must be positive" in resp_neg_qty.json()["detail"]


def test_persistence_across_separate_db_sessions(client):
    """Test all corrections and metadata persist faithfully across fresh database sessions."""
    setup = _setup_extracted_document(client)
    doc_id = setup["doc_id"]

    # Perform field correction
    client.post(
        f"/v1/documents/{doc_id}/verification/correct-field",
        json={
            "field_name": "supplier_name",
            "corrected_value": "Acme Widgets Corp.",
            "corrected_by": "auditor_pers",
            "expected_version": 1,
            "reason": "Corrected truncated name",
        },
    )

    # In a completely separate database session
    engine = get_engine()
    with Session(engine) as db:
        v_row = db.execute(
            select(DocumentVerification).where(DocumentVerification.document_id == uuid.UUID(doc_id))
        ).scalar_one()

        assert v_row.version == 2
        assert v_row.verified_fields["supplier_name"] == "Acme Widgets Corp."

        corrections = db.execute(
            select(VerificationCorrection)
            .where(VerificationCorrection.verification_id == v_row.id)
            .order_by(VerificationCorrection.created_at.asc())
        ).scalars().all()

        assert len(corrections) == 1
        assert corrections[0].corrected_by == "auditor_pers"
        assert corrections[0].corrected_value == "Acme Widgets Corp."
        assert corrections[0].field_name == "supplier_name"


def test_missing_document_and_extraction_handling(client):
    """Test 404 for non-existent document and 422 when extraction has not been performed."""
    non_existent = uuid.uuid4()
    resp_404 = client.get(f"/v1/documents/{non_existent}/verification")
    assert resp_404.status_code == 404

    # Document uploaded but no extraction performed
    doc = _upload_test_pdf(client, "No Extraction Doc")
    doc_id = doc["id"]

    resp_no_ext = client.get(f"/v1/documents/{doc_id}/verification")
    assert resp_no_ext.status_code == 422
    assert "extraction" in resp_no_ext.json()["detail"].lower()
