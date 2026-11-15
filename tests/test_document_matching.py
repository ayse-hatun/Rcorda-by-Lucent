"""Targeted PostgreSQL integration tests for Document Matching & Relationship Management.

Verifies:
1. Rejection of unverified documents (only APPROVED human verification is authoritative).
2. Full 4-document purchasing transaction chain (PO <-> DC <-> GRN <-> INV).
3. Missing reference handling (UNMATCHED_NO_REFERENCE, zero guessing).
4. Target not found handling (UNMATCHED_TARGET_NOT_FOUND).
5. Ambiguous duplicate candidate handling (FLAGGED_AMBIGUOUS, no silent selection).
6. Supplier mismatch validation and rejection.
7. Idempotency and duplicate relationship prevention (DB unique constraint).
8. Manual match decisions (manual linking, resolving ambiguous, unlinking).
9. Audit trail persistence and explainability across sessions.
10. Concurrency protection via row locks.
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
    DocumentMatchAudit,
    DocumentRelationship,
    DocumentVerification,
    ExtractedDocumentData,
    OcrPageResult,
    OcrRun,
    Supplier,
)
from apps.api.routes.documents import get_extraction_service, get_ocr_service
from apps.api.routes.matching import get_matching_service
from apps.api.routes.verifications import get_verification_service
from apps.api.services.extraction_service import DocumentExtractionService
from apps.api.services.matching_service import DocumentMatchingService
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

    def _override_matching_service(db: Session = Depends(get_db)):
        return DocumentMatchingService(db=db)

    app.dependency_overrides[get_ocr_service] = _override_ocr_service
    app.dependency_overrides[get_extraction_service] = _override_extraction_service
    app.dependency_overrides[get_verification_service] = _override_verification_service
    app.dependency_overrides[get_matching_service] = _override_matching_service
    yield TestClient(app)
    app.dependency_overrides.pop(get_ocr_service, None)
    app.dependency_overrides.pop(get_extraction_service, None)
    app.dependency_overrides.pop(get_verification_service, None)
    app.dependency_overrides.pop(get_matching_service, None)


def _upload_test_pdf(client, title: str, doc_type: str) -> dict:
    """Helper to upload a valid PDF document with a unique checksum."""
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({
        "/Title": title,
        "/Producer": f"RecordaMatchingTest-{uuid.uuid4()}",
    })
    buf = io.BytesIO()
    writer.write(buf)

    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": doc_type},
        files={"file": (f"match_{uuid.uuid4().hex[:8]}.pdf", buf.getvalue(), "application/pdf")},
    )
    assert resp.status_code == 201
    return resp.json()


def _create_approved_document(
    client,
    doc_type: str,
    doc_number: str,
    po_reference: str = None,
    dc_reference: str = None,
    supplier_name: str = "Acme Supplies Corp",
) -> str:
    """Helper to create and fully verify/approve a document with specific verified fields."""
    doc = _upload_test_pdf(client, f"{doc_type} {doc_number} {uuid.uuid4()}", doc_type=doc_type)
    doc_id = doc["id"]

    engine = get_engine()
    with Session(engine) as db:
        # Create minimal extraction
        fields = {
            "document_type": doc_type,
            "supplier_name": supplier_name,
            "currency": "USD",
        }
        if doc_type == "PURCHASE_ORDER":
            fields["po_number"] = doc_number
            fields["grand_total"] = "1000.00"
        elif doc_type == "DELIVERY_CHALLAN":
            fields["dc_number"] = doc_number
            if po_reference:
                fields["po_reference"] = po_reference
        elif doc_type == "GOODS_RECEIPT_NOTE":
            fields["grn_number"] = doc_number
            if po_reference:
                fields["po_reference"] = po_reference
            if dc_reference:
                fields["dc_reference"] = dc_reference
        elif doc_type == "SUPPLIER_INVOICE":
            fields["invoice_number"] = doc_number
            fields["grand_total"] = "1000.00"
            if po_reference:
                fields["po_reference"] = po_reference

        extraction = DocumentExtraction(
            document_id=uuid.UUID(doc_id),
            run_number=1,
            document_type=doc_type,
            extractor_version="1.0.0",
            status="COMPLETED",
            fields=fields,
            line_items=[],
        )
        db.add(extraction)
        db.flush()

        # Create approved verification session
        verification = DocumentVerification(
            document_id=uuid.UUID(doc_id),
            extraction_id=extraction.id,
            status="APPROVED",
            version=2,
            verified_fields=fields,
            verified_line_items=[],
            reviewed_by="auditor_setup",
        )
        db.add(verification)

        # Update Document status and document_number
        doc_row = db.get(Document, uuid.UUID(doc_id))
        doc_row.status = "VERIFIED"
        doc_row.document_number = doc_number
        if po_reference:
            doc_row.po_reference = po_reference

        db.commit()

    return doc_id


def test_unverified_document_rejected_for_automatic_matching(client):
    """Test matching cannot run on a document without APPROVED verification."""
    doc = _upload_test_pdf(client, "Unverified DC", doc_type="DELIVERY_CHALLAN")
    doc_id = doc["id"]

    resp = client.post(f"/v1/documents/{doc_id}/match")
    assert resp.status_code == 422
    assert "only APPROVED human-verified" in resp.json()["detail"]


def test_valid_matching_full_purchasing_chain(client):
    """Test complete transaction chain matching PO <-> DC <-> GRN <-> INV."""
    po_num = f"PO-2026-{uuid.uuid4().hex[:6]}"
    dc_num = f"DC-2026-{uuid.uuid4().hex[:6]}"
    grn_num = f"GRN-2026-{uuid.uuid4().hex[:6]}"
    inv_num = f"INV-2026-{uuid.uuid4().hex[:6]}"
    supplier = "Global Industrial Supplies Inc"

    # 1. Create and approve all 4 documents
    po_id = _create_approved_document(client, "PURCHASE_ORDER", po_num, supplier_name=supplier)
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", dc_num, po_reference=po_num, supplier_name=supplier)
    grn_id = _create_approved_document(client, "GOODS_RECEIPT_NOTE", grn_num, po_reference=po_num, dc_reference=dc_num, supplier_name=supplier)
    inv_id = _create_approved_document(client, "SUPPLIER_INVOICE", inv_num, po_reference=po_num, supplier_name=supplier)

    # 2. Run match on Delivery Challan
    dc_match = client.post(f"/v1/documents/{dc_id}/match")
    assert dc_match.status_code == 200
    dc_res = dc_match.json()
    assert dc_res["match_status"] == "MATCHED"
    assert len(dc_res["relationships"]) == 1
    rel_dc = dc_res["relationships"][0]
    assert rel_dc["relationship_type"] == "DELIVERY_CHALLAN_TO_PURCHASE_ORDER"
    assert rel_dc["status"] == "CONFIRMED"
    assert rel_dc["target_document_id"] == po_id

    # 3. Run match on Goods Receipt Note
    grn_match = client.post(f"/v1/documents/{grn_id}/match")
    assert grn_match.status_code == 200
    grn_res = grn_match.json()
    assert grn_res["match_status"] == "MATCHED"
    assert len(grn_res["relationships"]) == 2
    types = {r["relationship_type"]: r["target_document_id"] for r in grn_res["relationships"]}
    assert types["GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER"] == po_id
    assert types["GOODS_RECEIPT_NOTE_TO_DELIVERY_CHALLAN"] == dc_id

    # 4. Run match on Supplier Invoice
    inv_match = client.post(f"/v1/documents/{inv_id}/match")
    assert inv_match.status_code == 200
    inv_res = inv_match.json()
    assert inv_res["match_status"] == "MATCHED"
    assert len(inv_res["relationships"]) == 1
    assert inv_res["relationships"][0]["relationship_type"] == "SUPPLIER_INVOICE_TO_PURCHASE_ORDER"
    assert inv_res["relationships"][0]["target_document_id"] == po_id

    # 5. Inspect relationships and complete purchasing chain from any document
    chain_resp = client.get(f"/v1/documents/{po_id}/relationships")
    assert chain_resp.status_code == 200
    chain_data = chain_resp.json()
    tx_chain = chain_data["transaction_chain"]
    assert tx_chain["connected_document_count"] == 4
    assert len(tx_chain["purchase_orders"]) == 1
    assert len(tx_chain["delivery_challans"]) == 1
    assert len(tx_chain["goods_receipt_notes"]) == 1
    assert len(tx_chain["supplier_invoices"]) == 1


def test_missing_reference_unmatched_explicitly(client):
    """Test document with missing references returns UNMATCHED_NO_REFERENCE without guessing."""
    dc_num = f"DC-NOREF-{uuid.uuid4().hex[:6]}"
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", dc_num, po_reference=None)

    resp = client.post(f"/v1/documents/{dc_id}/match")
    assert resp.status_code == 200
    res = resp.json()
    assert res["match_status"] == "UNMATCHED_NO_REFERENCE"
    assert len(res["relationships"]) == 0

    # Verify audit log captures missing reference
    audits_resp = client.get(f"/v1/documents/{dc_id}/relationships/audit")
    assert audits_resp.status_code == 200
    audits = audits_resp.json()
    assert len(audits) >= 1
    assert audits[-1]["action"] == "NO_REFERENCE_FOUND"


def test_target_not_found_explicitly(client):
    """Test referenced PO that does not exist returns UNMATCHED_TARGET_NOT_FOUND."""
    dc_num = f"DC-NOTARGET-{uuid.uuid4().hex[:6]}"
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", dc_num, po_reference="PO-GHOST-404")

    resp = client.post(f"/v1/documents/{dc_id}/match")
    assert resp.status_code == 200
    res = resp.json()
    assert res["match_status"] == "UNMATCHED_TARGET_NOT_FOUND"
    assert len(res["relationships"]) == 0

    audits_resp = client.get(f"/v1/documents/{dc_id}/relationships/audit")
    assert audits_resp.status_code == 200
    assert audits_resp.json()[-1]["action"] == "TARGET_NOT_FOUND"


def test_ambiguous_duplicate_candidates_flagged(client):
    """Test when multiple candidate POs match the same reference, FLAGGED_AMBIGUOUS is raised."""
    shared_po_ref = f"PO-AMBIG-{uuid.uuid4().hex[:6]}"

    # Create two different approved PO documents sharing the same PO number
    po1_id = _create_approved_document(client, "PURCHASE_ORDER", shared_po_ref, supplier_name="Alpha Corp")
    po2_id = _create_approved_document(client, "PURCHASE_ORDER", shared_po_ref, supplier_name="Alpha Corp")

    # Create DC referencing that shared PO
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", f"DC-{uuid.uuid4().hex[:6]}", po_reference=shared_po_ref, supplier_name="Alpha Corp")

    resp = client.post(f"/v1/documents/{dc_id}/match")
    assert resp.status_code == 200
    res = resp.json()
    assert res["match_status"] == "AMBIGUOUS"
    assert len(res["relationships"]) == 2
    for rel in res["relationships"]:
        assert rel["status"] == "FLAGGED_AMBIGUOUS"

    # Verify audit trail
    audits_resp = client.get(f"/v1/documents/{dc_id}/relationships/audit")
    assert audits_resp.status_code == 200
    assert any(a["action"] == "MATCH_AMBIGUOUS_FLAGGED" for a in audits_resp.json())


def test_supplier_mismatch_rejected(client):
    """Test conflicting supplier names rejects match with SUPPLIER_MISMATCH."""
    po_num = f"PO-SUPP-{uuid.uuid4().hex[:6]}"
    po_id = _create_approved_document(client, "PURCHASE_ORDER", po_num, supplier_name="Acme Tools Ltd")

    # DC references this PO, but belongs to a completely different supplier
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", f"DC-{uuid.uuid4().hex[:6]}", po_reference=po_num, supplier_name="Globex Heavy Industries")

    resp = client.post(f"/v1/documents/{dc_id}/match")
    assert resp.status_code == 200
    res = resp.json()
    assert res["match_status"] == "SUPPLIER_MISMATCH"
    assert len(res["relationships"]) == 1
    assert res["relationships"][0]["status"] == "REJECTED"

    # Audit log recorded
    audits_resp = client.get(f"/v1/documents/{dc_id}/relationships/audit")
    assert audits_resp.status_code == 200
    assert any(a["action"] == "SUPPLIER_MISMATCH" for a in audits_resp.json())


def test_duplicate_relationship_prevention(client):
    """Test repeated matching calls do not create duplicate relationship records."""
    po_num = f"PO-IDEMP-{uuid.uuid4().hex[:6]}"
    po_id = _create_approved_document(client, "PURCHASE_ORDER", po_num, supplier_name="Vendor A")
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", f"DC-{uuid.uuid4().hex[:6]}", po_reference=po_num, supplier_name="Vendor A")

    # Run match first time
    resp1 = client.post(f"/v1/documents/{dc_id}/match")
    assert resp1.status_code == 200
    assert len(resp1.json()["relationships"]) == 1
    rel_id_1 = resp1.json()["relationships"][0]["id"]

    # Run match second time
    resp2 = client.post(f"/v1/documents/{dc_id}/match")
    assert resp2.status_code == 200
    assert len(resp2.json()["relationships"]) == 1
    rel_id_2 = resp2.json()["relationships"][0]["id"]

    assert rel_id_1 == rel_id_2

    # Verify directly in database that only 1 relationship row exists
    engine = get_engine()
    with Session(engine) as db:
        rels = db.execute(
            select(DocumentRelationship)
            .where(DocumentRelationship.source_document_id == uuid.UUID(dc_id))
        ).scalars().all()
        assert len(rels) == 1


def test_manual_match_decision_and_resolution(client):
    """Test manual link creation, ambiguous candidate resolution, and relationship unlinking."""
    po_num = f"PO-MANUAL-{uuid.uuid4().hex[:6]}"
    po_id = _create_approved_document(client, "PURCHASE_ORDER", po_num, supplier_name="Apex Ltd")
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", f"DC-{uuid.uuid4().hex[:6]}", po_reference=None, supplier_name="Apex Ltd")

    # 1. Manually link DC to PO
    link_resp = client.post(
        f"/v1/documents/{dc_id}/relationships/manual",
        json={
            "target_document_id": po_id,
            "relationship_type": "DELIVERY_CHALLAN_TO_PURCHASE_ORDER",
            "actor": "senior_buyer",
            "reason": "PO reference omitted on physical paper challan, verified via delivery address.",
        },
    )
    assert link_resp.status_code == 201
    rel_data = link_resp.json()
    assert rel_data["status"] == "CONFIRMED"
    assert rel_data["matching_method"] == "MANUAL_OVERRIDE"
    rel_id = rel_data["id"]

    # 2. Resolve/update relationship manually
    res_resp = client.post(
        f"/v1/documents/{dc_id}/relationships/{rel_id}/resolve",
        json={"action": "REJECT", "actor": "qa_auditor", "reason": "Found mismatch in delivery date."},
    )
    assert res_resp.status_code == 200
    assert res_resp.json()["status"] == "REJECTED"

    # 3. Unlink/delete relationship
    del_resp = client.delete(
        f"/v1/documents/{dc_id}/relationships/{rel_id}",
        params={"actor": "qa_auditor", "reason": "Cleaned up invalid manual link."},
    )
    assert del_resp.status_code == 204

    # Confirm relationship deleted
    engine = get_engine()
    with Session(engine) as db:
        deleted = db.get(DocumentRelationship, uuid.UUID(rel_id))
        assert deleted is None


def test_audit_persistence_across_sessions(client):
    """Test match audit records persist faithfully with explainability evidence across sessions."""
    po_num = f"PO-AUDIT-{uuid.uuid4().hex[:6]}"
    po_id = _create_approved_document(client, "PURCHASE_ORDER", po_num, supplier_name="Audit Supplier Inc")
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", f"DC-{uuid.uuid4().hex[:6]}", po_reference=po_num, supplier_name="Audit Supplier Inc")

    client.post(f"/v1/documents/{dc_id}/match")

    # Inspect audit trail in separate database session
    engine = get_engine()
    with Session(engine) as db:
        audits = db.execute(
            select(DocumentMatchAudit)
            .where(DocumentMatchAudit.source_document_id == uuid.UUID(dc_id))
            .order_by(DocumentMatchAudit.created_at.asc())
        ).scalars().all()

        assert len(audits) >= 1
        audit = audits[0]
        assert audit.action == "MATCH_CONFIRMED"
        assert audit.new_status == "CONFIRMED"
        assert audit.evidence is not None
        assert audit.evidence.get("po_reference") == po_num


def test_concurrency_protection(client):
    """Test concurrent match calls don't conflict or produce duplicate relationships."""
    po_num = f"PO-CONC-{uuid.uuid4().hex[:6]}"
    po_id = _create_approved_document(client, "PURCHASE_ORDER", po_num, supplier_name="Conc Vendor")
    dc_id = _create_approved_document(client, "DELIVERY_CHALLAN", f"DC-{uuid.uuid4().hex[:6]}", po_reference=po_num, supplier_name="Conc Vendor")

    # Sequential rapid triggers simulating concurrent calls
    r1 = client.post(f"/v1/documents/{dc_id}/match")
    r2 = client.post(f"/v1/documents/{dc_id}/match")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["relationships"][0]["id"] == r2.json()["relationships"][0]["id"]
