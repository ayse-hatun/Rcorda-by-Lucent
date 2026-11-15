import hashlib
import io
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from apps.api.database import get_db, get_engine
from apps.api.main import app
from apps.api.models import Document, PurchaseOrder, PurchaseOrderItem, Supplier
from apps.api.storage import (
    FileSystemStorage,
    FileNotFoundInStorageError,
    StorageBackend,
    StorageError,
    get_storage_service,
)
from sqlalchemy.orm import Session


def _make_valid_pdf(text: str = None) -> bytes:
    """Generate minimal valid PDF binary using pypdf writer."""
    if text is None:
        text = f"Test PDF Document {uuid.uuid4()}"
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": text, "/Subject": str(uuid.uuid4())})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_valid_png() -> bytes:
    import random

    buf = io.BytesIO()
    color = (random.randint(1, 254), random.randint(1, 254), random.randint(1, 254))
    img = Image.new("RGB", (20, 20), color=color)
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_valid_jpeg() -> bytes:
    import random

    buf = io.BytesIO()
    color = (random.randint(1, 254), random.randint(1, 254), random.randint(1, 254))
    img = Image.new("RGB", (20, 20), color=color)
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def isolated_storage(tmp_path):
    storage = FileSystemStorage(tmp_path / "storage")
    app.dependency_overrides[get_storage_service] = lambda: storage
    yield storage
    app.dependency_overrides.pop(get_storage_service, None)


@pytest.fixture
def client(isolated_storage):
    return TestClient(app)


def test_valid_pdf_upload(client):
    pdf_bytes = _make_valid_pdf()
    expected_sha = hashlib.sha256(pdf_bytes).hexdigest()

    response = client.post(
        "/v1/documents/upload",
        data={
            "document_type": "PURCHASE_ORDER",
            "document_number": "PO-TEST-001",
            "po_reference": "PO-TEST-001",
        },
        files={
            "file": ("purchase_order.pdf", pdf_bytes, "application/pdf"),
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["document_type"] == "PURCHASE_ORDER"
    assert data["file_name"] == "purchase_order.pdf"
    assert data["mime_type"] == "application/pdf"
    assert data["file_size"] == len(pdf_bytes)
    assert data["sha256"] == expected_sha
    assert data["status"] == "INGESTED"
    assert data["s3_key"].startswith("documents/purchase_order/")


def test_valid_image_upload_png_and_jpeg(client):
    # Test PNG
    png_bytes = _make_valid_png()
    png_resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "DELIVERY_CHALLAN"},
        files={"file": ("challan.png", png_bytes, "image/png")},
    )
    assert png_resp.status_code == 201
    assert png_resp.json()["mime_type"] == "image/png"
    assert png_resp.json()["file_size"] == len(png_bytes)

    # Test JPEG
    jpeg_bytes = _make_valid_jpeg()
    jpeg_resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "GOODS_RECEIPT_NOTE"},
        files={"file": ("grn.jpg", jpeg_bytes, "image/jpeg")},
    )
    assert jpeg_resp.status_code == 201
    assert jpeg_resp.json()["mime_type"] == "image/jpeg"
    assert jpeg_resp.json()["file_size"] == len(jpeg_bytes)


def test_invalid_extension_rejected(client):
    txt_content = b"This is plain text"
    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "SUPPLIER_INVOICE"},
        files={"file": ("invoice.txt", txt_content, "text/plain")},
    )
    assert resp.status_code == 400
    assert "Unsupported file extension" in resp.json()["detail"]


def test_invalid_file_content_rejected(client):
    # Fake PDF (text pretending to be PDF)
    fake_pdf = b"Hello, I am pretending to be a PDF file."
    resp1 = client.post(
        "/v1/documents/upload",
        data={"document_type": "PURCHASE_ORDER"},
        files={"file": ("fake.pdf", fake_pdf, "application/pdf")},
    )
    assert resp1.status_code == 400
    assert "Invalid PDF content" in resp1.json()["detail"]

    # Corrupt PNG
    fake_png = b"\x89PNG\r\n\x1a\nCorruptedGarbageBytes1234567890"
    resp2 = client.post(
        "/v1/documents/upload",
        data={"document_type": "DELIVERY_CHALLAN"},
        files={"file": ("corrupt.png", fake_png, "image/png")},
    )
    assert resp2.status_code == 400
    assert "Malformed image file" in resp2.json()["detail"]


def test_oversized_upload_rejected(client):
    # Over 10 MB: 10 * 1024 * 1024 + 10 bytes
    oversized = b"%PDF-" + (b"0" * (10 * 1024 * 1024 + 10))
    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "PURCHASE_ORDER"},
        files={"file": ("large.pdf", oversized, "application/pdf")},
    )
    assert resp.status_code == 413
    assert "exceeds maximum allowed size" in resp.json()["detail"]


def test_duplicate_checksum_rejected(client):
    pdf = _make_valid_pdf(f"Unique duplicate test {uuid.uuid4()}")
    resp1 = client.post(
        "/v1/documents/upload",
        data={"document_type": "SUPPLIER_INVOICE"},
        files={"file": ("inv1.pdf", pdf, "application/pdf")},
    )
    assert resp1.status_code == 201

    # Upload identical file
    resp2 = client.post(
        "/v1/documents/upload",
        data={"document_type": "SUPPLIER_INVOICE"},
        files={"file": ("inv1_copy.pdf", pdf, "application/pdf")},
    )
    assert resp2.status_code == 409
    assert "identical checksum already exists" in resp2.json()["detail"]


def test_database_persistence_across_sessions(client):
    uid = uuid.uuid4().hex[:8]
    pdf = _make_valid_pdf(f"Session test {uid}")
    sha = hashlib.sha256(pdf).hexdigest()

    resp = client.post(
        "/v1/documents/upload",
        data={
            "document_type": "PURCHASE_ORDER",
            "document_number": f"PO-SESS-{uid}",
        },
        files={"file": ("session_test.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 201
    doc_id = uuid.UUID(resp.json()["id"])

    # Independent session directly with PostgreSQL engine
    engine = get_engine()
    with Session(engine) as session:
        stored_doc = session.query(Document).filter(Document.id == doc_id).first()
        assert stored_doc is not None
        assert stored_doc.sha256 == sha
        assert stored_doc.file_size == len(pdf)
        assert stored_doc.status == "INGESTED"
        assert stored_doc.file_name == "session_test.pdf"


def test_successful_download(client):
    pdf = _make_valid_pdf()
    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "PURCHASE_ORDER"},
        files={"file": ("download_me.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 201
    doc_id = resp.json()["id"]

    dl_resp = client.get(f"/v1/documents/{doc_id}/download")
    assert dl_resp.status_code == 200
    assert dl_resp.content == pdf
    assert "download_me.pdf" in dl_resp.headers.get("Content-Disposition", "")
    assert dl_resp.headers.get("Content-Type") == "application/pdf"


def test_missing_document_returns_404(client):
    random_id = uuid.uuid4()
    resp_get = client.get(f"/v1/documents/{random_id}")
    assert resp_get.status_code == 404

    resp_dl = client.get(f"/v1/documents/{random_id}/download")
    assert resp_dl.status_code == 404


def test_missing_storage_object_returns_404(client, isolated_storage):
    pdf = _make_valid_pdf()
    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "DELIVERY_CHALLAN"},
        files={"file": ("missing_obj.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 201
    doc_id = resp.json()["id"]
    storage_key = resp.json()["s3_key"]

    # Delete the underlying physical file
    deleted = isolated_storage.delete_object(storage_key)
    assert deleted is True

    dl_resp = client.get(f"/v1/documents/{doc_id}/download")
    assert dl_resp.status_code == 404
    assert "Original file not found in storage" in dl_resp.json()["detail"]


def test_storage_failure_handled_safely(client):
    class FailingStorage(StorageBackend):
        def put_object(self, key, data, content_type=None):
            raise StorageError("Disk full or S3 connection timeout")

        def get_object(self, key, chunk_size=65536):
            raise StorageError("Read failed")

        def get_object_bytes(self, key):
            raise StorageError("Read failed")

        def delete_object(self, key):
            return False

        def exists(self, key):
            return False

    app.dependency_overrides[get_storage_service] = lambda: FailingStorage()

    pdf = _make_valid_pdf("Failure safety test")
    sha = hashlib.sha256(pdf).hexdigest()

    resp = client.post(
        "/v1/documents/upload",
        data={"document_type": "PURCHASE_ORDER"},
        files={"file": ("fail.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 500
    assert "Storage service failure" in resp.json()["detail"]

    # Verify no orphaned record was committed to PostgreSQL
    engine = get_engine()
    with Session(engine) as session:
        orphan = session.query(Document).filter(Document.sha256 == sha).first()
        assert orphan is None


def test_supplier_po_relationship_validation(client):
    engine = get_engine()
    uid = uuid.uuid4().hex[:8]

    # Create supplier and PO in database
    with Session(engine) as session:
        supplier = Supplier(
            name=f"Document Supplier {uid}",
            code=f"DOC-SUP-{uid}",
        )
        session.add(supplier)
        session.flush()

        po = PurchaseOrder(
            po_number=f"DOC-PO-{uid}",
            supplier_id=supplier.id,
            total_amount=Decimal("1000.0000"),
            currency="USD",
            status="ISSUED",
        )
        po.items.append(
            PurchaseOrderItem(
                line_item_id="1",
                description="Test Item",
                ordered_quantity=Decimal("10.0000"),
                unit_price=Decimal("100.0000"),
                total_price=Decimal("1000.0000"),
            )
        )
        session.add(po)
        session.commit()
        supplier_id = supplier.id
        po_id = po.id

    # 1. Non-existent supplier returns 404
    pdf1 = _make_valid_pdf("Rel test 1")
    resp_bad_sup = client.post(
        "/v1/documents/upload",
        data={
            "document_type": "PURCHASE_ORDER",
            "supplier_id": str(uuid.uuid4()),
        },
        files={"file": ("doc1.pdf", pdf1, "application/pdf")},
    )
    assert resp_bad_sup.status_code == 404
    assert "Supplier" in resp_bad_sup.json()["detail"]

    # 2. Non-existent PO returns 404
    pdf2 = _make_valid_pdf("Rel test 2")
    resp_bad_po = client.post(
        "/v1/documents/upload",
        data={
            "document_type": "PURCHASE_ORDER",
            "purchase_order_id": str(uuid.uuid4()),
        },
        files={"file": ("doc2.pdf", pdf2, "application/pdf")},
    )
    assert resp_bad_po.status_code == 404
    assert "Purchase order" in resp_bad_po.json()["detail"]

    # 3. Valid supplier and PO linkage
    pdf3 = _make_valid_pdf(f"Rel test 3 {uid}")
    resp_valid = client.post(
        "/v1/documents/upload",
        data={
            "document_type": "PURCHASE_ORDER",
            "supplier_id": str(supplier_id),
            "purchase_order_id": str(po_id),
        },
        files={"file": ("doc3.pdf", pdf3, "application/pdf")},
    )
    assert resp_valid.status_code == 201
    doc_data = resp_valid.json()
    assert doc_data["supplier_id"] == str(supplier_id)
    assert doc_data["purchase_order_id"] == str(po_id)
    assert doc_data["po_reference"] == f"DOC-PO-{uid}"

    # 4. Filter documents by document_type and supplier_id
    list_resp = client.get(
        f"/v1/documents?document_type=PURCHASE_ORDER&supplier_id={supplier_id}"
    )
    assert list_resp.status_code == 200
    results = list_resp.json()
    assert len(results) >= 1
    assert any(d["id"] == doc_data["id"] for d in results)
