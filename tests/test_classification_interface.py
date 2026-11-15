"""Tests for the Recorda Document Classification interface and persistence foundation.

Verifies:
1. Expected input/output contract enforcement.
2. Versioned model artifact loading and validation.
3. Contract-compliant baseline classifier behavior without synthetic data.
4. Confidence scores, threshold enforcement, and human-review flags.
5. PostgreSQL persistence across runs and sessions (version history preservation).
6. Human review confirmation and document type update workflow.
7. Non-blocking behavior for documents and downstream operations.
"""

import io
import json
from pathlib import Path
import uuid

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.main import app
from apps.api.models import Document, DocumentClassification, OcrRun
from apps.api.routes.documents import get_classification_service, get_ocr_service
from apps.api.services.classification_service import (
    ClassificationInputMissingError,
    ClassificationNotFoundError,
    DocumentClassificationService,
    DocumentNotFoundError,
    InvalidDocumentTypeError,
)
from apps.api.services.ocr_service import DocumentOcrService
from apps.api.storage import FileSystemStorage, get_storage_service
from packages.classification.artifact import (
    ArtifactBasedClassifier,
    ArtifactMetadata,
    ModelArtifactLoader,
)
from packages.classification.base import (
    BaseDocumentClassifier,
    ClassificationError,
    ClassificationInput,
    ClassificationResult,
    DocumentType,
    InvalidClassificationInputError,
    ModelArtifactError,
)
from packages.classification.heuristic_classifier import ContractCompliantClassifier
from packages.ocr.deterministic_engine import DeterministicOcrEngine


@pytest.fixture
def isolated_storage(tmp_path):
    storage = FileSystemStorage(tmp_path / "storage")
    app.dependency_overrides[get_storage_service] = lambda: storage
    yield storage
    app.dependency_overrides.pop(get_storage_service, None)


@pytest.fixture
def client(isolated_storage):
    def _override_classification_service(db: Session = Depends(get_db)):
        return DocumentClassificationService(
            db=db,
            classifier=ContractCompliantClassifier(),
            storage=isolated_storage,
        )

    def _override_ocr_service(db: Session = Depends(get_db)):
        return DocumentOcrService(
            db=db,
            storage=isolated_storage,
            engine=DeterministicOcrEngine(),
        )

    app.dependency_overrides[get_classification_service] = _override_classification_service
    app.dependency_overrides[get_ocr_service] = _override_ocr_service
    yield TestClient(app)
    app.dependency_overrides.pop(get_classification_service, None)
    app.dependency_overrides.pop(get_ocr_service, None)


def _upload_test_document(client, text_content: str = "Test document content") -> dict:
    """Helper to upload a simple PDF document for testing with guaranteed unique SHA-256."""
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({
        "/Title": text_content,
        "/Producer": f"RecordaTest-{uuid.uuid4()}",
    })
    buf = io.BytesIO()
    writer.write(buf)

    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "PURCHASE_ORDER"},
        files={"file": (f"test_{uuid.uuid4().hex[:8]}.pdf", buf.getvalue(), "application/pdf")},
    )
    assert resp.status_code == 201
    return resp.json()


# ==============================================================================
# Unit Tests: Contract & Interfaces
# ==============================================================================


def test_classification_input_contract_validation():
    # Valid input
    valid_input = ClassificationInput(
        raw_text="Purchase Order PO-98721 from Vendor Corp",
        document_id=uuid.uuid4(),
    )
    valid_input.validate()

    # Empty raw_text rejected
    with pytest.raises(InvalidClassificationInputError):
        invalid_input = ClassificationInput(raw_text="")
        invalid_input.validate()

    with pytest.raises(InvalidClassificationInputError):
        whitespace_input = ClassificationInput(raw_text="   \n\t  ")
        whitespace_input.validate()


def test_classification_result_contract_to_dict():
    result = ClassificationResult(
        predicted_type=DocumentType.PURCHASE_ORDER.value,
        confidence=0.95,
        model_name="TestModel",
        model_version="1.0.0",
        probabilities={"PURCHASE_ORDER": 0.95, "DELIVERY_CHALLAN": 0.05},
        needs_human_review=False,
    )
    d = result.to_dict()
    assert d["predicted_type"] == "PURCHASE_ORDER"
    assert d["confidence"] == 0.95
    assert d["model_name"] == "TestModel"
    assert d["needs_human_review"] is False
    assert "timestamp" in d


# ==============================================================================
# Unit Tests: Versioned Model Artifact Loading
# ==============================================================================


def test_artifact_metadata_parsing_and_validation():
    data = {
        "model_name": "RecordaDistilBertClassifier",
        "model_version": "1.2.0",
        "model_format": "sklearn",
        "labels": ["PURCHASE_ORDER", "DELIVERY_CHALLAN", "GOODS_RECEIPT_NOTE", "SUPPLIER_INVOICE"],
        "confidence_threshold": 0.85,
    }
    meta = ArtifactMetadata.from_dict(data)
    assert meta.model_name == "RecordaDistilBertClassifier"
    assert meta.model_version == "1.2.0"
    assert meta.confidence_threshold == 0.85
    assert len(meta.labels) == 4

    # Missing required key raises ModelArtifactError
    with pytest.raises(ModelArtifactError):
        ArtifactMetadata.from_dict({"model_name": "MissingFields"})


def test_artifact_loader_missing_directory_or_metadata(tmp_path):
    # Non-existent directory
    with pytest.raises(ModelArtifactError):
        ModelArtifactLoader.load_from_directory(tmp_path / "does_not_exist")

    # Directory without metadata file
    empty_dir = tmp_path / "empty_model"
    empty_dir.mkdir()
    with pytest.raises(ModelArtifactError) as exc_info:
        ModelArtifactLoader.load_from_directory(empty_dir)
    assert "not found" in str(exc_info.value)


def test_artifact_based_classifier_runtime():
    meta = ArtifactMetadata(
        model_name="CustomNeuralModel",
        model_version="2.0.0",
        model_format="custom",
        labels=["PURCHASE_ORDER", "DELIVERY_CHALLAN"],
        confidence_threshold=0.80,
    )

    # Mock prediction function simulating loaded weights
    def mock_predict_fn(text: str):
        if "challan" in text.lower():
            return {"PURCHASE_ORDER": 0.10, "DELIVERY_CHALLAN": 0.90}
        return {"PURCHASE_ORDER": 0.55, "DELIVERY_CHALLAN": 0.45}

    classifier = ArtifactBasedClassifier(metadata=meta, predict_fn=mock_predict_fn)
    assert classifier.model_name == "CustomNeuralModel"
    assert classifier.model_version == "2.0.0"

    # Confident prediction
    res_confident = classifier.predict(ClassificationInput(raw_text="Delivery Challan DC-101"))
    assert res_confident.predicted_type == "DELIVERY_CHALLAN"
    assert res_confident.confidence == 0.90
    assert res_confident.needs_human_review is False

    # Low confidence + ambiguous prediction flags human review
    res_ambiguous = classifier.predict(ClassificationInput(raw_text="Unclear document text"))
    assert res_ambiguous.needs_human_review is True
    assert any("below threshold" in r for r in res_ambiguous.review_reasons)
    assert any("Ambiguous prediction" in r for r in res_ambiguous.review_reasons)


# ==============================================================================
# Unit Tests: Contract-Compliant Baseline Classifier
# ==============================================================================


def test_contract_compliant_classifier_recognition():
    classifier = ContractCompliantClassifier()

    # Purchase Order text
    po_text = "PURCHASE ORDER PO-45091 Vendor: Acme Inc Bill to: Recorda Corp Order Date: 2026-05-01"
    po_res = classifier.predict(ClassificationInput(raw_text=po_text))
    assert po_res.predicted_type == "PURCHASE_ORDER"
    assert po_res.confidence >= 0.80
    assert po_res.needs_human_review is False

    # Delivery Challan text
    dc_text = "DELIVERY CHALLAN Challan No: CH-881 Dispatched by Transporter Vehicle No: TR-991"
    dc_res = classifier.predict(ClassificationInput(raw_text=dc_text))
    assert dc_res.predicted_type == "DELIVERY_CHALLAN"
    assert dc_res.needs_human_review is False

    # Supplier Invoice text
    inv_text = "TAX INVOICE Commercial Invoice No: INV-2026-99 Taxable Amount Total Tax GST"
    inv_res = classifier.predict(ClassificationInput(raw_text=inv_text))
    assert inv_res.predicted_type == "SUPPLIER_INVOICE"
    assert inv_res.needs_human_review is False

    # Unrecognized / ambiguous text triggers UNKNOWN and human review
    unknown_text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."
    unk_res = classifier.predict(ClassificationInput(raw_text=unknown_text))
    assert unk_res.predicted_type == "UNKNOWN"
    assert unk_res.needs_human_review is True


# ==============================================================================
# Integration Tests: PostgreSQL Persistence & API Endpoints
# ==============================================================================


def test_classification_persistence_and_versioning(client):
    doc = _upload_test_document(client)
    doc_id = doc["id"]

    po_text = "PURCHASE ORDER PO-991 Vendor: Prime Supplies Order Date: 2026-10-10"

    # Run 1: Classify with raw text
    resp1 = client.post(f"/v1/documents/{doc_id}/classify", json={"raw_text": po_text})
    assert resp1.status_code == 200
    data1 = resp1.json()

    assert data1["document_id"] == doc_id
    assert data1["run_number"] == 1
    assert data1["predicted_type"] == "PURCHASE_ORDER"
    assert data1["confidence_score"] >= 0.80
    assert data1["needs_human_review"] is False
    assert data1["status"] == "COMPLETED"

    # Run 2: Re-classify with different text
    challan_text = "DELIVERY CHALLAN Challan No: CH-500 Dispatched Vehicle: V-10"
    resp2 = client.post(f"/v1/documents/{doc_id}/classify", json={"raw_text": challan_text})
    assert resp2.status_code == 200
    data2 = resp2.json()

    # Historical runs preserved: run_number incremented
    assert data2["run_number"] == 2
    assert data2["predicted_type"] == "DELIVERY_CHALLAN"

    # Verify both runs are retrieved in history
    history_resp = client.get(f"/v1/documents/{doc_id}/classifications")
    assert history_resp.status_code == 200
    history = history_resp.json()
    assert len(history) == 2
    assert history[0]["run_number"] == 1
    assert history[1]["run_number"] == 2


def test_human_review_workflow_and_document_type_update(client):
    doc = _upload_test_document(client)
    doc_id = doc["id"]

    # Classify with ambiguous text to trigger review flag
    resp = client.post(
        f"/v1/documents/{doc_id}/classify",
        json={"raw_text": "Random text without standard procurement keywords"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["needs_human_review"] is True
    classification_id = data["id"]

    # Human reviewer confirms it is a GOODS_RECEIPT_NOTE
    review_resp = client.post(
        f"/v1/documents/{doc_id}/classifications/{classification_id}/review",
        json={
            "actual_type": "GOODS_RECEIPT_NOTE",
            "reviewer_id": "auditor_user_42",
            "notes": "Verified against warehouse receiving report stamp.",
        },
    )
    assert review_resp.status_code == 200
    reviewed_data = review_resp.json()

    assert reviewed_data["needs_human_review"] is False
    assert reviewed_data["actual_type"] == "GOODS_RECEIPT_NOTE"
    assert reviewed_data["reviewed_by"] == "auditor_user_42"
    assert reviewed_data["status"] == "CONFIRMED"

    # Verify Document metadata was updated in PostgreSQL
    doc_resp = client.get(f"/v1/documents/{doc_id}")
    assert doc_resp.status_code == 200
    assert doc_resp.json()["document_type"] == "GOODS_RECEIPT_NOTE"


def test_classification_uses_ocr_text_when_raw_text_omitted(client):
    doc = _upload_test_document(client)
    doc_id = doc["id"]

    # Perform OCR on the document first
    ocr_resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert ocr_resp.status_code == 200

    # Classify without supplying raw_text: should extract from OCR
    resp = client.post(f"/v1/documents/{doc_id}/classify", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "COMPLETED"
    assert data["predicted_type"] in DocumentType.values()


def test_classification_missing_document_returns_404(client):
    fake_id = uuid.uuid4()
    resp = client.post(f"/v1/documents/{fake_id}/classify", json={"raw_text": "Sample text"})
    assert resp.status_code == 404


def test_classification_missing_text_and_no_ocr_returns_422(client):
    doc = _upload_test_document(client)
    doc_id = doc["id"]

    # No OCR has been executed and no raw_text supplied
    resp = client.post(f"/v1/documents/{doc_id}/classify", json={})
    assert resp.status_code == 422
    assert "No extractable text found" in resp.json()["detail"]


def test_human_review_rejects_invalid_document_type(client):
    doc = _upload_test_document(client)
    doc_id = doc["id"]

    resp = client.post(
        f"/v1/documents/{doc_id}/classify", json={"raw_text": "Tax Invoice 101"}
    )
    classification_id = resp.json()["id"]

    bad_review = client.post(
        f"/v1/documents/{doc_id}/classifications/{classification_id}/review",
        json={"actual_type": "INVALID_PASSPORT_TYPE", "reviewer_id": "user1"},
    )
    assert bad_review.status_code == 422
    assert "Invalid document type" in bad_review.json()["detail"]


def test_non_blocking_dependency_for_downstream_operations(client):
    """Verifies that documents and downstream endpoints operate without requiring classification."""
    doc = _upload_test_document(client)
    doc_id = doc["id"]

    # Document can be retrieved directly without classification
    retrieved = client.get(f"/v1/documents/{doc_id}")
    assert retrieved.status_code == 200
    assert retrieved.json()["document_type"] == "PURCHASE_ORDER"

    # Document download functions normally without classification
    download = client.get(f"/v1/documents/{doc_id}/download")
    assert download.status_code == 200
    assert len(download.content) > 0
