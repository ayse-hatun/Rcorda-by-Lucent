"""Document classification domain service.

Orchestrates document classification, persists classification runs in PostgreSQL,
manages versioning and run history, and supports human review workflows.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from apps.api.models import Document, DocumentClassification, OcrRun
from apps.api.storage import StorageBackend
from packages.classification.base import (
    BaseDocumentClassifier,
    ClassificationError,
    ClassificationInput,
    ClassificationResult,
    DocumentType,
)
from packages.classification.heuristic_classifier import ContractCompliantClassifier


from apps.api.services.ocr_service import DocumentNotFoundError


class DocumentClassificationServiceError(Exception):
    """Base exception for classification service."""
    pass


class ClassificationNotFoundError(DocumentClassificationServiceError):
    """Raised when the specified classification record is not found."""
    pass


class ClassificationInputMissingError(DocumentClassificationServiceError):
    """Raised when no text or OCR results are available to perform classification."""
    pass


class InvalidDocumentTypeError(DocumentClassificationServiceError):
    """Raised when an unsupported document type is supplied for human review."""
    pass


class DocumentClassificationService:
    """Domain service managing document classification lifecycles and persistence."""

    def __init__(
        self,
        db: Session,
        classifier: Optional[BaseDocumentClassifier] = None,
        storage: Optional[StorageBackend] = None,
    ):
        self.db = db
        self.classifier = classifier or ContractCompliantClassifier()
        self.storage = storage

    def classify_document(
        self,
        document_id: uuid.UUID,
        raw_text: Optional[str] = None,
        force_reclassify: bool = False,
        actor: Optional[str] = None,
    ) -> DocumentClassification:
        """Classify a document using its raw text or existing OCR results.

        Preserves complete version history in PostgreSQL. Never overwrites historical runs.
        """
        doc = self.db.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        # Determine input text
        text_to_classify = raw_text
        if not text_to_classify:
            # Check latest completed OCR run
            latest_ocr = self.db.execute(
                select(OcrRun)
                .where(OcrRun.document_id == document_id, OcrRun.status == "COMPLETED")
                .order_by(desc(OcrRun.run_number))
            ).scalars().first()

            if latest_ocr and latest_ocr.full_text and latest_ocr.full_text.strip():
                text_to_classify = latest_ocr.full_text
            elif doc.extracted_data and doc.extracted_data.raw_text:
                text_to_classify = doc.extracted_data.raw_text

        if not text_to_classify or not text_to_classify.strip():
            raise ClassificationInputMissingError(
                f"No extractable text found for document '{document_id}'. "
                "Execute OCR first or provide raw_text explicitly."
            )

        # Determine next run_number for audit preservation
        latest_run = self.db.execute(
            select(DocumentClassification)
            .where(DocumentClassification.document_id == document_id)
            .order_by(desc(DocumentClassification.run_number))
        ).scalars().first()
        next_run_number = (latest_run.run_number + 1) if latest_run else 1

        # Run inference
        input_data = ClassificationInput(
            document_id=document_id,
            raw_text=text_to_classify,
            mime_type=doc.mime_type,
            metadata={"file_name": doc.file_name, "actor": actor},
        )

        try:
            result = self.classifier.predict(input_data)
            record = DocumentClassification(
                document_id=document_id,
                run_number=next_run_number,
                predicted_type=result.predicted_type,
                confidence_score=result.confidence,
                probabilities=result.probabilities,
                needs_human_review=result.needs_human_review,
                review_reasons=result.review_reasons,
                status="COMPLETED",
                model_name=result.model_name,
                model_version=result.model_version,
            )

            # If confident and not flagged for review, update the document's type
            if not result.needs_human_review and result.predicted_type != DocumentType.UNKNOWN.value:
                doc.document_type = result.predicted_type

        except Exception as exc:
            # Record failure in PostgreSQL to preserve audit trail
            record = DocumentClassification(
                document_id=document_id,
                run_number=next_run_number,
                predicted_type=DocumentType.UNKNOWN.value,
                confidence_score=0.0,
                probabilities={},
                needs_human_review=True,
                review_reasons=[f"Classifier execution failed: {exc}"],
                status="FAILED",
                model_name=self.classifier.model_name,
                model_version=self.classifier.model_version,
                error_message=str(exc),
            )

        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def submit_human_review(
        self,
        document_id: uuid.UUID,
        classification_id: uuid.UUID,
        actual_type: str,
        reviewer_id: str,
        notes: Optional[str] = None,
    ) -> DocumentClassification:
        """Submit human review/correction for a classification record."""
        # Normalize and validate document type
        valid_types = DocumentType.values()
        normalized_type = actual_type.strip().upper()
        if normalized_type not in valid_types:
            raise InvalidDocumentTypeError(
                f"Invalid document type '{actual_type}'. Valid types: {', '.join(valid_types)}"
            )

        record = self.db.execute(
            select(DocumentClassification).where(
                DocumentClassification.id == classification_id,
                DocumentClassification.document_id == document_id,
            )
        ).scalar_one_or_none()

        if not record:
            raise ClassificationNotFoundError(
                f"Classification '{classification_id}' not found for document '{document_id}'."
            )

        doc = self.db.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        # Update audit record
        record.actual_type = normalized_type
        record.reviewed_by = reviewer_id
        record.reviewed_at = datetime.now(timezone.utc)
        record.needs_human_review = False
        record.status = "CONFIRMED"

        # Update the document type to the human-confirmed type
        doc.document_type = normalized_type

        self.db.commit()
        self.db.refresh(record)
        return record

    def list_classifications(self, document_id: uuid.UUID) -> List[DocumentClassification]:
        """List all historical classifications for a document ordered by run_number."""
        doc = self.db.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        return list(
            self.db.execute(
                select(DocumentClassification)
                .where(DocumentClassification.document_id == document_id)
                .order_by(DocumentClassification.run_number.asc())
            ).scalars().all()
        )

    def get_classification(
        self, document_id: uuid.UUID, classification_id: uuid.UUID
    ) -> Optional[DocumentClassification]:
        """Retrieve a specific classification record."""
        return self.db.execute(
            select(DocumentClassification).where(
                DocumentClassification.id == classification_id,
                DocumentClassification.document_id == document_id,
            )
        ).scalar_one_or_none()
