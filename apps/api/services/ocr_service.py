import uuid
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import func
from sqlalchemy.orm import Session

from apps.api.models import Document, OcrPageResult, OcrRun
from apps.api.storage import FileNotFoundInStorageError, StorageBackend
from packages.ocr.base import BaseOcrEngine, CorruptedDocumentError, OcrError, UnsupportedDocumentFormatError
from packages.ocr.paddle_engine import PaddleOcrEngine


class DocumentNotFoundError(Exception):
    """Raised when a document is not found in the database."""
    pass


class DocumentOcrService:
    """
    Decoupled OCR domain service.
    Orchestrates file retrieval from Storage, image conversion, OCR inference,
    and PostgreSQL persistence for multi-page versioned runs.
    """

    def __init__(
        self,
        db: Session,
        storage: StorageBackend,
        engine: Optional[BaseOcrEngine] = None,
    ):
        self.db = db
        self.storage = storage
        self.engine = engine or PaddleOcrEngine.get_instance()

    def run_ocr(self, document_id: uuid.UUID) -> OcrRun:
        """
        Execute OCR processing on an existing document.
        Creates a new versioned OcrRun preserving previous run history.
        """
        # 1. Fetch document from database
        doc = self.db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found")

        # 2. Check storage key and object existence
        if not doc.s3_key or not self.storage.exists(doc.s3_key):
            raise FileNotFoundInStorageError(
                f"Original file for document '{document_id}' not found in storage"
            )

        # 3. Retrieve raw binary content from storage
        file_bytes = self.storage.get_object_bytes(doc.s3_key)

        # 4. Determine next sequential run_number
        current_max = (
            self.db.query(func.max(OcrRun.run_number))
            .filter(OcrRun.document_id == document_id)
            .scalar()
        )
        run_number = (current_max or 0) + 1

        run_id = uuid.uuid4()
        file_name = doc.file_name or "document.bin"

        # 5. Execute OCR inference
        try:
            ocr_result = self.engine.process_document(file_bytes, file_name)

            ocr_run = OcrRun(
                id=run_id,
                document_id=document_id,
                run_number=run_number,
                status="COMPLETED",
                ocr_engine=ocr_result.engine,
                ocr_version=ocr_result.version,
                total_pages=ocr_result.total_pages,
                full_text=ocr_result.full_text,
                confidence_score=ocr_result.confidence,
                completed_at=datetime.now(timezone.utc),
            )

            # Record each page result
            for page in ocr_result.pages:
                page_record = OcrPageResult(
                    id=uuid.uuid4(),
                    ocr_run_id=run_id,
                    document_id=document_id,
                    page_number=page.page_number,
                    raw_text=page.raw_text,
                    text_blocks=[b.to_dict() for b in page.blocks],
                    confidence_score=page.confidence,
                )
                ocr_run.pages.append(page_record)

            self.db.add(ocr_run)
            self.db.commit()
            self.db.refresh(ocr_run)
            return ocr_run

        except Exception as exc:
            self.db.rollback()
            # Record failed run to preserve error audit trail
            failed_run = OcrRun(
                id=run_id,
                document_id=document_id,
                run_number=run_number,
                status="FAILED",
                ocr_engine=self.engine.engine_name,
                ocr_version=self.engine.engine_version,
                total_pages=0,
                full_text=None,
                confidence_score=0.0,
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc),
            )
            try:
                self.db.add(failed_run)
                self.db.commit()
                self.db.refresh(failed_run)
            except Exception:
                self.db.rollback()
            raise exc

    def list_runs(self, document_id: uuid.UUID) -> List[OcrRun]:
        doc = self.db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found")

        return (
            self.db.query(OcrRun)
            .filter(OcrRun.document_id == document_id)
            .order_by(OcrRun.run_number.asc())
            .all()
        )

    def get_run(self, document_id: uuid.UUID, ocr_run_id: uuid.UUID) -> Optional[OcrRun]:
        doc = self.db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found")

        return (
            self.db.query(OcrRun)
            .filter(OcrRun.id == ocr_run_id, OcrRun.document_id == document_id)
            .first()
        )
