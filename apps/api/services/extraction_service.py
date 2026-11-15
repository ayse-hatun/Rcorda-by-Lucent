"""Document structured data extraction domain service.

Orchestrates structured data extraction from OCR results, persists extraction runs
in PostgreSQL, enforces validation and field provenance, and manages versioning.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from apps.api.models import (
    Document,
    DocumentClassification,
    DocumentExtraction,
    ExtractedDocumentData,
    OcrRun,
)
from apps.api.services.ocr_service import DocumentNotFoundError
from packages.extraction import (
    DeliveryChallanExtractor,
    GoodsReceiptNoteExtractor,
    PurchaseOrderExtractor,
    SupplierInvoiceExtractor,
)
from packages.extraction.base import BaseExtractor, ExtractionOutput


class DocumentExtractionServiceError(Exception):
    """Base exception for extraction service."""
    pass


class ExtractionNotFoundError(DocumentExtractionServiceError):
    """Raised when the requested extraction record is not found."""
    pass


class OcrRunMissingError(DocumentExtractionServiceError):
    """Raised when no completed OCR run is available for extraction."""
    pass


class UnknownDocumentTypeError(DocumentExtractionServiceError):
    """Raised when document type is unknown or invalid and cannot be resolved."""
    pass


VALID_DOCUMENT_TYPES = {
    "PURCHASE_ORDER",
    "DELIVERY_CHALLAN",
    "GOODS_RECEIPT_NOTE",
    "SUPPLIER_INVOICE",
}


class DocumentExtractionService:
    """Domain service managing document structured data extraction lifecycles."""

    def __init__(self, db: Session):
        self.db = db
        self._extractors: Dict[str, BaseExtractor] = {
            "PURCHASE_ORDER": PurchaseOrderExtractor(),
            "DELIVERY_CHALLAN": DeliveryChallanExtractor(),
            "GOODS_RECEIPT_NOTE": GoodsReceiptNoteExtractor(),
            "SUPPLIER_INVOICE": SupplierInvoiceExtractor(),
        }

    def extract_document(
        self,
        document_id: uuid.UUID,
        ocr_run_id: Optional[uuid.UUID] = None,
        document_type_override: Optional[str] = None,
    ) -> DocumentExtraction:
        """Extract structured fields from OCR pages of a document."""
        # 1. Retrieve Document
        doc = self.db.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        # 2. Retrieve OCR Run
        if ocr_run_id:
            ocr_run = self.db.execute(
                select(OcrRun).where(
                    OcrRun.id == ocr_run_id, OcrRun.document_id == document_id
                )
            ).scalar_one_or_none()
            if not ocr_run:
                raise OcrRunMissingError(
                    f"Specified OCR run '{ocr_run_id}' not found for document '{document_id}'."
                )
        else:
            ocr_run = self.db.execute(
                select(OcrRun)
                .where(OcrRun.document_id == document_id, OcrRun.status == "COMPLETED")
                .order_by(desc(OcrRun.run_number))
            ).scalars().first()

            if not ocr_run:
                raise OcrRunMissingError(
                    f"No completed OCR run found for document '{document_id}'. "
                    "Execute OCR prior to structured data extraction."
                )

        # 3. Resolve Document Type
        target_type = None
        if document_type_override:
            cleaned_override = document_type_override.strip().upper()
            if cleaned_override not in VALID_DOCUMENT_TYPES:
                raise UnknownDocumentTypeError(
                    f"Unsupported document type override '{document_type_override}'. "
                    f"Supported types: {', '.join(sorted(VALID_DOCUMENT_TYPES))}"
                )
            target_type = cleaned_override
        elif doc.document_type and doc.document_type.upper() in VALID_DOCUMENT_TYPES:
            target_type = doc.document_type.upper()
        else:
            # Check latest confirmed or predicted classification
            latest_class = self.db.execute(
                select(DocumentClassification)
                .where(DocumentClassification.document_id == document_id)
                .order_by(desc(DocumentClassification.run_number))
            ).scalars().first()
            if latest_class:
                candidate = latest_class.actual_type or latest_class.predicted_type
                if candidate and candidate.upper() in VALID_DOCUMENT_TYPES:
                    target_type = candidate.upper()

        if not target_type or target_type not in VALID_DOCUMENT_TYPES:
            raise UnknownDocumentTypeError(
                f"Document type for '{document_id}' is unknown or unsupported. "
                "Specify a document_type override to proceed with extraction."
            )

        # 4. Select Extractor
        extractor = self._extractors[target_type]

        # 5. Prepare OCR Page Payloads
        pages_payload = [
            {
                "page_number": p.page_number,
                "raw_text": p.raw_text,
                "text_blocks": p.text_blocks,
                "confidence_score": p.confidence_score,
            }
            for p in ocr_run.pages
        ]

        # 6. Execute Extraction
        output: ExtractionOutput = extractor.extract(pages_payload)

        # 7. Compute next run_number for versioning
        latest_ext = self.db.execute(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == document_id)
            .order_by(desc(DocumentExtraction.run_number))
        ).scalars().first()
        next_run_number = (latest_ext.run_number + 1) if latest_ext else 1

        # 8. Persist DocumentExtraction
        status_val = "REVIEW_REQUIRED" if output.needs_human_review else "COMPLETED"
        extraction_record = DocumentExtraction(
            document_id=document_id,
            ocr_run_id=ocr_run.id,
            run_number=next_run_number,
            document_type=target_type,
            extractor_version=output.extractor_version,
            status=status_val,
            fields=output.fields,
            line_items=output.line_items,
            field_provenance=output.provenance,
            validation_warnings=output.warnings,
            needs_human_review=output.needs_human_review,
        )
        self.db.add(extraction_record)

        # 9. Also update/populate ExtractedDocumentData for backwards compatibility
        if not doc.extracted_data:
            legacy_data = ExtractedDocumentData(
                document_id=document_id,
                raw_text=ocr_run.full_text,
                fields=output.fields,
                line_items=output.line_items,
                confidence_score=0.90 if not output.needs_human_review else 0.65,
                model_version=output.extractor_version,
            )
            self.db.add(legacy_data)
        else:
            doc.extracted_data.raw_text = ocr_run.full_text
            doc.extracted_data.fields = output.fields
            doc.extracted_data.line_items = output.line_items
            doc.extracted_data.model_version = output.extractor_version

        # Update document_number or po_reference if newly extracted and empty
        if not doc.document_number:
            for id_field in ("po_number", "dc_number", "grn_number", "invoice_number"):
                if output.fields.get(id_field):
                    doc.document_number = output.fields[id_field]
                    break
        if not doc.po_reference and output.fields.get("po_reference"):
            doc.po_reference = output.fields["po_reference"]

        self.db.commit()
        self.db.refresh(extraction_record)
        return extraction_record

    def list_extractions(self, document_id: uuid.UUID) -> List[DocumentExtraction]:
        """List all historical extractions for a document ordered by run_number."""
        doc = self.db.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        return list(
            self.db.execute(
                select(DocumentExtraction)
                .where(DocumentExtraction.document_id == document_id)
                .order_by(DocumentExtraction.run_number.asc())
            ).scalars().all()
        )

    def get_extraction(
        self, document_id: uuid.UUID, extraction_id: uuid.UUID
    ) -> Optional[DocumentExtraction]:
        """Retrieve a specific extraction record."""
        return self.db.execute(
            select(DocumentExtraction).where(
                DocumentExtraction.id == extraction_id,
                DocumentExtraction.document_id == document_id,
            )
        ).scalar_one_or_none()
