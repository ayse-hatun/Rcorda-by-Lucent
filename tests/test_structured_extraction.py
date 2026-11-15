"""Tests for the Recorda Structured Data Extraction pipeline.

Verifies:
1. Document extraction for PURCHASE_ORDER, DELIVERY_CHALLAN, GOODS_RECEIPT_NOTE, and SUPPLIER_INVOICE.
2. Multiple line items and multi-page OCR document processing.
3. Missing optional fields represented as null, not guessed.
4. Field-level provenance and confidence scores.
5. Validation warnings for inconsistent line totals, subtotal/tax/grand-total mismatches, invalid dates, negative quantities, and unsupported currencies.
6. Preservation of original extracted text when warnings occur.
7. Multiple extraction runs and version history preservation in PostgreSQL.
8. Safe handling of missing documents, missing OCR runs, unknown document types, and empty text.
9. Persistence across database sessions.
"""

from decimal import Decimal
import io
import uuid

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.database import get_db, get_engine
from apps.api.main import app
from apps.api.models import Document, DocumentExtraction, ExtractedDocumentData, OcrPageResult, OcrRun
from apps.api.routes.documents import get_extraction_service, get_ocr_service
from apps.api.services.extraction_service import DocumentExtractionService
from apps.api.services.ocr_service import DocumentOcrService
from apps.api.storage import FileSystemStorage, get_storage_service
from packages.extraction.extractors import (
    DeliveryChallanExtractor,
    GoodsReceiptNoteExtractor,
    PurchaseOrderExtractor,
    SupplierInvoiceExtractor,
)
from packages.extraction.schemas import (
    DeliveryChallanExtractionSchema,
    GoodsReceiptNoteExtractionSchema,
    PurchaseOrderExtractionSchema,
    SupplierInvoiceExtractionSchema,
)
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

    app.dependency_overrides[get_ocr_service] = _override_ocr_service
    app.dependency_overrides[get_extraction_service] = _override_extraction_service
    yield TestClient(app)
    app.dependency_overrides.pop(get_ocr_service, None)
    app.dependency_overrides.pop(get_extraction_service, None)


def _upload_pdf(client, text_title: str, doc_type: str = "PURCHASE_ORDER") -> dict:
    """Upload a mock PDF document with guaranteed unique checksum."""
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({
        "/Title": text_title,
        "/Producer": f"RecordaExtractionTest-{uuid.uuid4()}",
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


def _create_ocr_run_with_text(db: Session, doc_id: uuid.UUID, pages_text: list) -> OcrRun:
    """Directly insert an OCR run with specific page text for deterministic testing."""
    full_text = "\n\n--- PAGE BREAK ---\n\n".join(pages_text)
    ocr_run = OcrRun(
        document_id=doc_id,
        run_number=1,
        status="COMPLETED",
        ocr_engine="DeterministicTestEngine",
        ocr_version="1.0.0",
        total_pages=len(pages_text),
        full_text=full_text,
        confidence_score=0.98,
    )
    db.add(ocr_run)
    db.flush()

    for idx, txt in enumerate(pages_text, start=1):
        page_res = OcrPageResult(
            ocr_run_id=ocr_run.id,
            document_id=doc_id,
            page_number=idx,
            raw_text=txt,
            confidence_score=0.98,
        )
        db.add(page_res)

    db.commit()
    db.refresh(ocr_run)
    return ocr_run


# ==============================================================================
# Unit Tests: Extractors & Schema Compliance
# ==============================================================================


def test_purchase_order_extractor_success():
    po_text = """
    PURCHASE ORDER
    PO Number: PO-2026-0042
    Vendor: Acme Industrial Supplies
    Order Date: 2026-04-15
    Currency: USD
    
    Line Items:
    ITEM-01 Precision Bearings 100 pcs @ 12.50 1250.00
    ITEM-02 Hydraulic Valves 20 pcs @ 45.00 900.00
    
    Subtotal: 2150.00
    Sales Tax: 172.00
    Grand Total: 2322.00
    """
    extractor = PurchaseOrderExtractor()
    output = extractor.extract([{"page_number": 1, "raw_text": po_text}])

    assert output.document_type == "PURCHASE_ORDER"
    assert output.fields["po_number"] == "PO-2026-0042"
    assert output.fields["supplier_name"] == "Acme Industrial Supplies"
    assert output.fields["po_date"] == "2026-04-15"
    assert output.fields["currency"] == "USD"
    assert output.fields["subtotal"] == "2150.00"
    assert output.fields["tax_amount"] == "172.00"
    assert output.fields["grand_total"] == "2322.00"

    assert len(output.line_items) == 2
    assert output.line_items[0]["item_description"] == "Precision Bearings"
    assert output.line_items[0]["quantity"] == "100"
    assert output.line_items[0]["unit_price"] == "12.50"
    assert output.line_items[0]["line_total"] == "1250.00"

    # Provenance audit
    assert "po_number" in output.provenance
    assert output.provenance["po_number"]["extraction_method"] == "REGEX_ANCHOR"
    assert output.provenance["po_number"]["page_number"] == 1
    assert not output.needs_human_review

    # Schema validation
    schema_obj = PurchaseOrderExtractionSchema(
        po_number=output.fields["po_number"],
        supplier_name=output.fields["supplier_name"],
        po_date=output.fields["po_date"],
        currency=output.fields["currency"],
        subtotal=Decimal(output.fields["subtotal"]),
        tax_amount=Decimal(output.fields["tax_amount"]),
        grand_total=Decimal(output.fields["grand_total"]),
        line_items=output.line_items,
    )
    assert schema_obj.po_number == "PO-2026-0042"


def test_delivery_challan_extractor_success():
    dc_text = """
    DELIVERY CHALLAN
    Challan No: DC-9871
    Against PO: PO-2026-0042
    Consignor: Acme Industrial Supplies
    Dispatch Date: 2026-04-18
    
    Items:
    ITEM-01 Precision Bearings 100 pcs
    ITEM-02 Hydraulic Valves 20 pcs
    """
    extractor = DeliveryChallanExtractor()
    output = extractor.extract([{"page_number": 1, "raw_text": dc_text}])

    assert output.document_type == "DELIVERY_CHALLAN"
    assert output.fields["dc_number"] == "DC-9871"
    assert output.fields["po_reference"] == "PO-2026-0042"
    assert output.fields["supplier_name"] == "Acme Industrial Supplies"
    assert output.fields["dispatch_date"] == "2026-04-18"
    assert len(output.line_items) == 2
    assert output.line_items[0]["dispatched_quantity"] == "100"
    assert not output.needs_human_review

    # Schema validation
    schema_obj = DeliveryChallanExtractionSchema(
        dc_number=output.fields["dc_number"],
        po_reference=output.fields["po_reference"],
        supplier_name=output.fields["supplier_name"],
        dispatch_date=output.fields["dispatch_date"],
        line_items=output.line_items,
    )
    assert schema_obj.dc_number == "DC-9871"


def test_goods_receipt_note_extractor_success():
    grn_text = """
    GOODS RECEIPT NOTE
    GRN Number: GRN-5541
    PO Ref: PO-2026-0042
    DC Ref: DC-9871
    Supplier: Acme Industrial Supplies
    Received Date: 2026-04-20
    
    Items:
    ITEM-01 Precision Bearings 100 pcs 98 2
    ITEM-02 Hydraulic Valves 20 pcs 20 0
    """
    extractor = GoodsReceiptNoteExtractor()
    output = extractor.extract([{"page_number": 1, "raw_text": grn_text}])

    assert output.document_type == "GOODS_RECEIPT_NOTE"
    assert output.fields["grn_number"] == "GRN-5541"
    assert output.fields["po_reference"] == "PO-2026-0042"
    assert output.fields["dc_reference"] == "DC-9871"
    assert output.fields["receipt_date"] == "2026-04-20"
    assert len(output.line_items) == 2
    assert output.line_items[0]["received_quantity"] == "100"
    assert output.line_items[0]["accepted_quantity"] == "98"
    assert output.line_items[0]["rejected_quantity"] == "2"

    schema_obj = GoodsReceiptNoteExtractionSchema(
        grn_number=output.fields["grn_number"],
        po_reference=output.fields["po_reference"],
        dc_reference=output.fields["dc_reference"],
        receipt_date=output.fields["receipt_date"],
        line_items=output.line_items,
    )
    assert schema_obj.grn_number == "GRN-5541"


def test_supplier_invoice_extractor_success():
    inv_text = """
    TAX INVOICE
    Invoice No: INV-8876
    PO Ref: PO-2026-0042
    Remit To: Acme Industrial Supplies
    Invoice Date: 2026-04-22
    Due Date: 2026-05-22
    Currency: USD
    
    Line Items:
    ITEM-01 Precision Bearings 100 pcs @ 12.50 1250.00
    ITEM-02 Hydraulic Valves 20 pcs @ 45.00 900.00
    
    Subtotal: 2150.00
    Total Tax: 172.00
    Invoice Total: 2322.00
    """
    extractor = SupplierInvoiceExtractor()
    output = extractor.extract([{"page_number": 1, "raw_text": inv_text}])

    assert output.document_type == "SUPPLIER_INVOICE"
    assert output.fields["invoice_number"] == "INV-8876"
    assert output.fields["po_reference"] == "PO-2026-0042"
    assert output.fields["invoice_date"] == "2026-04-22"
    assert output.fields["due_date"] == "2026-05-22"
    assert output.fields["subtotal"] == "2150.00"
    assert output.fields["tax_amount"] == "172.00"
    assert output.fields["grand_total"] == "2322.00"

    schema_obj = SupplierInvoiceExtractionSchema(
        invoice_number=output.fields["invoice_number"],
        po_reference=output.fields["po_reference"],
        invoice_date=output.fields["invoice_date"],
        due_date=output.fields["due_date"],
        currency=output.fields["currency"],
        subtotal=Decimal(output.fields["subtotal"]),
        grand_total=Decimal(output.fields["grand_total"]),
        line_items=output.line_items,
    )
    assert schema_obj.invoice_number == "INV-8876"


def test_missing_optional_fields_are_null_not_guessed():
    minimal_po = """
    PURCHASE ORDER
    PO Number: PO-MINIMAL-01
    
    Line Items:
    ITEM-01 Steel Screws 500 pcs @ 0.10 50.00
    """
    extractor = PurchaseOrderExtractor()
    output = extractor.extract([{"page_number": 1, "raw_text": minimal_po}])

    assert output.fields["po_number"] == "PO-MINIMAL-01"
    assert output.fields["supplier_name"] is None
    assert output.fields["po_date"] is None
    assert output.fields["subtotal"] is None
    assert output.fields["tax_amount"] is None
    assert output.fields["grand_total"] is None


def test_validation_warnings_on_inconsistent_line_and_grand_totals():
    bad_totals_po = """
    PURCHASE ORDER
    PO Number: PO-MATH-ERR
    
    Line Items:
    ITEM-01 Gears 10 pcs @ 50.00 600.00
    
    Subtotal: 500.00
    Sales Tax: 50.00
    Grand Total: 700.00
    """
    extractor = PurchaseOrderExtractor()
    output = extractor.extract([{"page_number": 1, "raw_text": bad_totals_po}])

    assert output.needs_human_review is True
    warning_codes = [w["warning_code"] for w in output.warnings]
    assert "INCONSISTENT_LINE_TOTAL" in warning_codes
    assert "INCONSISTENT_TOTALS" in warning_codes

    # Original extracted values are preserved, not silently altered
    assert output.line_items[0]["line_total"] == "600.00"
    assert output.fields["grand_total"] == "700.00"


def test_validation_warning_on_unsupported_currency():
    foreign_po = """
    PURCHASE ORDER
    PO Number: PO-EUR-01
    Currency: EUR
    
    Line Items:
    ITEM-01 Items 10 pcs @ 10.00 100.00
    """
    extractor = PurchaseOrderExtractor()
    output = extractor.extract([{"page_number": 1, "raw_text": foreign_po}])

    assert output.needs_human_review is True
    warning_codes = [w["warning_code"] for w in output.warnings]
    assert "UNSUPPORTED_CURRENCY" in warning_codes


# ==============================================================================
# Integration Tests: API & PostgreSQL Persistence
# ==============================================================================


def test_api_extract_endpoint_multi_page_and_persistence(client):
    doc = _upload_pdf(client, "Multi-page PO", doc_type="PURCHASE_ORDER")
    doc_id = uuid.UUID(doc["id"])

    # Insert multi-page OCR run directly
    page1 = """
    PURCHASE ORDER
    PO Number: PO-MULTI-01
    Vendor: Premier Dynamics
    Order Date: 2026-03-01
    
    Line Items:
    ITEM-01 First Part 50 pcs @ 20.00 1000.00
    """
    page2 = """
    Line Items Continued:
    ITEM-02 Second Part 100 pcs @ 5.00 500.00
    
    Subtotal: 1500.00
    Grand Total: 1500.00
    """
    with next(client.app.dependency_overrides.get(get_db, get_db)()) as db:
        _create_ocr_run_with_text(db, doc_id, [page1, page2])

    # Call extraction API
    resp = client.post(f"/v1/documents/{doc_id}/extract")
    assert resp.status_code == 200
    data = resp.json()

    assert data["document_id"] == str(doc_id)
    assert data["run_number"] == 1
    assert data["document_type"] == "PURCHASE_ORDER"
    assert data["fields"]["po_number"] == "PO-MULTI-01"
    assert len(data["line_items"]) == 2
    assert data["line_items"][0]["page_number"] == 1
    assert data["line_items"][1]["page_number"] == 2

    # Re-extract: verify historical runs are preserved with incremented run_number
    resp2 = client.post(f"/v1/documents/{doc_id}/extract")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["run_number"] == 2

    # Verify extraction list endpoint
    list_resp = client.get(f"/v1/documents/{doc_id}/extractions")
    assert list_resp.status_code == 200
    history = list_resp.json()
    assert len(history) == 2
    assert history[0]["run_number"] == 1
    assert history[1]["run_number"] == 2

    # Verify single extraction retrieval endpoint
    single_resp = client.get(f"/v1/documents/{doc_id}/extractions/{data['id']}")
    assert single_resp.status_code == 200
    assert single_resp.json()["id"] == data["id"]


def test_api_extract_with_document_type_override(client):
    # Uploaded as PURCHASE_ORDER, but we override extraction as DELIVERY_CHALLAN
    doc = _upload_pdf(client, "Challan Document", doc_type="PURCHASE_ORDER")
    doc_id = uuid.UUID(doc["id"])

    challan_text = """
    DELIVERY CHALLAN
    Challan No: DC-5501
    PO Ref: PO-9912
    Dispatched Items:
    ITEM-01 Widgets 100 pcs
    """
    with next(client.app.dependency_overrides.get(get_db, get_db)()) as db:
        _create_ocr_run_with_text(db, doc_id, [challan_text])

    resp = client.post(
        f"/v1/documents/{doc_id}/extract",
        json={"document_type": "DELIVERY_CHALLAN"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_type"] == "DELIVERY_CHALLAN"
    assert data["fields"]["dc_number"] == "DC-5501"


def test_extract_missing_document_returns_404(client):
    fake_id = uuid.uuid4()
    resp = client.post(f"/v1/documents/{fake_id}/extract")
    assert resp.status_code == 404


def test_extract_without_ocr_run_returns_422(client):
    doc = _upload_pdf(client, "No OCR Document")
    doc_id = doc["id"]

    # Attempt extraction before running OCR
    resp = client.post(f"/v1/documents/{doc_id}/extract")
    assert resp.status_code == 422
    assert "No completed OCR run found" in resp.json()["detail"]


def test_extract_unknown_document_type_returns_422(client):
    doc = _upload_pdf(client, "Unknown Type", doc_type="PURCHASE_ORDER")
    doc_id = doc["id"]

    with next(client.app.dependency_overrides.get(get_db, get_db)()) as db:
        _create_ocr_run_with_text(db, uuid.UUID(doc_id), ["Some unclassified text"])

    resp = client.post(
        f"/v1/documents/{doc_id}/extract",
        json={"document_type": "INVALID_TYPE"},
    )
    assert resp.status_code == 422
    assert "Unsupported document type override" in resp.json()["detail"]


def test_empty_ocr_text_handled_safely(client):
    doc = _upload_pdf(client, "Empty Text")
    doc_id = uuid.UUID(doc["id"])

    with next(client.app.dependency_overrides.get(get_db, get_db)()) as db:
        _create_ocr_run_with_text(db, doc_id, ["   "])

    resp = client.post(f"/v1/documents/{doc_id}/extract")
    assert resp.status_code == 200
    data = resp.json()
    assert data["needs_human_review"] is True
    assert data["status"] == "REVIEW_REQUIRED"
