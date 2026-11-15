import hashlib
import io
import uuid
from typing import List
from PIL import Image, ImageDraw

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.database import get_db, get_engine
from apps.api.main import app
from apps.api.models import Document, OcrPageResult, OcrRun
from apps.api.routes.documents import get_ocr_service
from apps.api.services.ocr_service import DocumentOcrService
from apps.api.storage import FileSystemStorage, get_storage_service
from packages.ocr.base import BaseOcrEngine, OcrBlock, OcrError
from packages.ocr.deterministic_engine import DeterministicOcrEngine


def _make_pdf(pages_text: List[str]) -> bytes:
    """Generate a multi-page PDF with specific text per page using pypdf."""
    import pypdf

    writer = pypdf.PdfWriter()
    for txt in pages_text:
        # Add blank page and store unique metadata
        writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": "; ".join(pages_text), "/Subject": str(uuid.uuid4())})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_image(text: str, fmt: str = "PNG") -> bytes:
    """Generate a test image with text rendered onto it."""
    img = Image.new("RGB", (300, 100), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((10, 40), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


class DynamicPageAwareEngine(BaseOcrEngine):
    """Deterministic OCR engine that returns page-specific text for tests."""

    @property
    def engine_name(self) -> str:
        return "DeterministicTestEngine"

    @property
    def engine_version(self) -> str:
        return "1.0.0"

    def process_image(self, image: Image.Image) -> List[OcrBlock]:
        w, h = image.size
        return [
            OcrBlock(
                text=f"Extracted content {w}x{h}",
                confidence=0.975,
                bbox=[[0.0, 0.0], [float(w), 0.0], [float(w), float(h)], [0.0, float(h)]],
            )
        ]


@pytest.fixture
def isolated_storage(tmp_path):
    storage = FileSystemStorage(tmp_path / "storage")
    app.dependency_overrides[get_storage_service] = lambda: storage
    yield storage
    app.dependency_overrides.pop(get_storage_service, None)


@pytest.fixture
def test_engine():
    return DynamicPageAwareEngine()


@pytest.fixture
def client(isolated_storage, test_engine):
    from apps.api.database import get_db

    def _override_ocr_service(
        db: Session = Depends(get_db),
    ):
        return DocumentOcrService(db=db, storage=isolated_storage, engine=test_engine)

    app.dependency_overrides[get_ocr_service] = _override_ocr_service
    yield TestClient(app)
    app.dependency_overrides.pop(get_ocr_service, None)


def _upload_doc(client, filename: str, content: bytes, doc_type: str = "PURCHASE_ORDER") -> dict:
    ext = filename.split(".")[-1].lower()
    mime = "application/pdf" if ext == "pdf" else f"image/{ext}"
    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": doc_type},
        files={"file": (filename, content, mime)},
    )
    assert resp.status_code == 201
    return resp.json()


def test_multipage_pdf_ocr(client):
    pdf = _make_pdf(["Page 1 Invoice Details", "Page 2 Line Items", "Page 3 Summary Terms"])
    doc = _upload_doc(client, "multipage.pdf", pdf)
    doc_id = doc["id"]

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "COMPLETED"
    assert data["total_pages"] == 3
    assert data["run_number"] == 1
    assert data["ocr_engine"] == "DeterministicTestEngine"
    assert len(data["pages"]) == 3
    assert [p["page_number"] for p in data["pages"]] == [1, 2, 3]


def test_png_image_ocr(client):
    png = _make_image(f"PNG Challan {uuid.uuid4()}", fmt="PNG")
    doc = _upload_doc(client, "challan.png", png, doc_type="DELIVERY_CHALLAN")
    doc_id = doc["id"]

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "COMPLETED"
    assert data["total_pages"] == 1
    assert len(data["pages"]) == 1
    page = data["pages"][0]
    assert page["page_number"] == 1
    assert page["confidence_score"] == 0.975
    assert len(page["text_blocks"]) >= 1
    assert page["text_blocks"][0]["bbox"] is not None


def test_jpeg_image_ocr(client):
    jpeg = _make_image(f"GRN Receipt {uuid.uuid4()}", fmt="JPEG")
    doc = _upload_doc(client, "receipt.jpg", jpeg, doc_type="GOODS_RECEIPT_NOTE")
    doc_id = doc["id"]

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "COMPLETED"
    assert data["total_pages"] == 1
    assert data["pages"][0]["confidence_score"] > 0


def test_empty_ocr_result(client, isolated_storage):
    from apps.api.database import get_db

    # OCR engine returning empty blocks
    empty_engine = DeterministicOcrEngine(extractor=lambda img: [])

    def _override_empty(db: Session = Depends(get_db)):
        return DocumentOcrService(db=db, storage=isolated_storage, engine=empty_engine)

    app.dependency_overrides[get_ocr_service] = _override_empty

    pdf = _make_pdf(["Blank Document"])
    doc = _upload_doc(client, "blank.pdf", pdf)
    doc_id = doc["id"]

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "COMPLETED"
    assert data["total_pages"] == 1
    assert data["full_text"] == ""
    assert data["pages"][0]["raw_text"] == ""


def test_corrupted_pdf_ocr(client, isolated_storage):
    # Upload a valid PDF first
    valid_pdf = _make_pdf(["Temporary"])
    doc = _upload_doc(client, "corrupt.pdf", valid_pdf)
    doc_id = doc["id"]

    # Now corrupt the file in storage to test OCR resilience
    isolated_storage.put_object(doc["s3_key"], b"%PDF-corrupted-random-bytes-not-a-real-pdf-page-tree")

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 422
    assert "Failed to parse PDF document" in resp.json()["detail"]

    # Verify a failed run was recorded
    runs_resp = client.get(f"/v1/documents/{doc_id}/ocr")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    assert len(runs) == 1
    assert runs[0]["status"] == "FAILED"
    assert "Failed to parse PDF" in runs[0]["error_message"]


def test_missing_document_returns_404(client):
    random_id = uuid.uuid4()
    resp_post = client.post(f"/v1/documents/{random_id}/ocr")
    assert resp_post.status_code == 404

    resp_get = client.get(f"/v1/documents/{random_id}/ocr")
    assert resp_get.status_code == 404


def test_missing_storage_object_returns_404(client, isolated_storage):
    pdf = _make_pdf(["File to be removed"])
    doc = _upload_doc(client, "missing_storage.pdf", pdf)
    doc_id = doc["id"]

    # Delete from storage
    isolated_storage.delete_object(doc["s3_key"])

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 404
    assert "not found in storage" in resp.json()["detail"]


def test_ocr_engine_failure_handled_safely(client, isolated_storage):
    class CrashingEngine(BaseOcrEngine):
        @property
        def engine_name(self) -> str:
            return "CrashingEngine"

        @property
        def engine_version(self) -> str:
            return "0.0.1"

        def process_image(self, image: Image.Image):
            raise OcrError("CUDA Out of Memory or neural model crashed")

    def _override_crashing(db: Session = Depends(get_db)):
        return DocumentOcrService(db=db, storage=isolated_storage, engine=CrashingEngine())

    app.dependency_overrides[get_ocr_service] = _override_crashing
    try:
        pdf = _make_pdf(["Crash test"])
        doc = _upload_doc(client, "crash.pdf", pdf)
        doc_id = doc["id"]

        resp = client.post(f"/v1/documents/{doc_id}/ocr")
        assert resp.status_code == 500
        assert "neural model crashed" in resp.json()["detail"]

        # Verify audit record in DB has status FAILED
        runs = client.get(f"/v1/documents/{doc_id}/ocr").json()
        assert len(runs) == 1
        assert runs[0]["status"] == "FAILED"
        assert "neural model crashed" in runs[0]["error_message"]
    finally:
        app.dependency_overrides.pop(get_ocr_service, None)


def test_multiple_ocr_runs_and_historical_preservation(client):
    pdf = _make_pdf(["First Version"])
    doc = _upload_doc(client, "history.pdf", pdf)
    doc_id = doc["id"]

    # Run 1
    run1 = client.post(f"/v1/documents/{doc_id}/ocr").json()
    assert run1["run_number"] == 1

    # Run 2
    run2 = client.post(f"/v1/documents/{doc_id}/ocr").json()
    assert run2["run_number"] == 2

    # Query all runs for document
    runs_resp = client.get(f"/v1/documents/{doc_id}/ocr")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    assert len(runs) == 2
    assert runs[0]["run_number"] == 1
    assert runs[1]["run_number"] == 2
    assert runs[0]["id"] == run1["id"]
    assert runs[1]["id"] == run2["id"]

    # Query specific run
    run1_detail = client.get(f"/v1/documents/{doc_id}/ocr/{run1['id']}").json()
    assert run1_detail["run_number"] == 1


def test_persistence_across_database_sessions(client):
    pdf = _make_pdf(["Session Persistence Test Page 1", "Session Persistence Test Page 2"])
    doc = _upload_doc(client, "session_doc.pdf", pdf)
    doc_id = uuid.UUID(doc["id"])

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 200
    run_id = uuid.UUID(resp.json()["id"])

    # Independent session directly to PostgreSQL
    engine = get_engine()
    with Session(engine) as session:
        stored_run = session.query(OcrRun).filter(OcrRun.id == run_id).first()
        assert stored_run is not None
        assert stored_run.document_id == doc_id
        assert stored_run.status == "COMPLETED"
        assert stored_run.total_pages == 2

        pages = (
            session.query(OcrPageResult)
            .filter(OcrPageResult.ocr_run_id == run_id)
            .order_by(OcrPageResult.page_number.asc())
            .all()
        )
        assert len(pages) == 2
        assert pages[0].page_number == 1
        assert pages[1].page_number == 2
        assert len(pages[0].text_blocks) >= 1


def test_correct_page_ordering(client):
    # 5-page PDF
    pdf = _make_pdf(["Page 1", "Page 2", "Page 3", "Page 4", "Page 5"])
    doc = _upload_doc(client, "ordered.pdf", pdf)
    doc_id = doc["id"]

    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 200
    data = resp.json()

    page_numbers = [p["page_number"] for p in data["pages"]]
    assert page_numbers == [1, 2, 3, 4, 5]


def test_original_file_remains_unchanged(client, isolated_storage):
    pdf = _make_pdf(["Checksum Integrity Test"])
    original_sha = hashlib.sha256(pdf).hexdigest()
    doc = _upload_doc(client, "integrity.pdf", pdf)
    doc_id = doc["id"]

    # Run OCR
    resp = client.post(f"/v1/documents/{doc_id}/ocr")
    assert resp.status_code == 200

    # Retrieve file from storage and check sha256
    stored_bytes = isolated_storage.get_object_bytes(doc["s3_key"])
    after_sha = hashlib.sha256(stored_bytes).hexdigest()

    assert after_sha == original_sha
    assert stored_bytes == pdf
