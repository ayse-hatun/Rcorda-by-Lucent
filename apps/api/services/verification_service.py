"""Human verification and review workflow domain service.

Manages review sessions, field and line-item corrections, complete audit history,
state machine transitions, and optimistic concurrency locking.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from apps.api.models import (
    Document,
    DocumentExtraction,
    DocumentVerification,
    ExtractedDocumentData,
    VerificationCorrection,
)
from apps.api.services.ocr_service import DocumentNotFoundError


class VerificationServiceError(Exception):
    """Base exception for verification service."""
    pass


class VerificationNotFoundError(VerificationServiceError):
    """Raised when the requested verification session is not found."""
    pass


class ExtractionMissingError(VerificationServiceError):
    """Raised when no extraction exists to initiate human verification."""
    pass


class InvalidStateTransitionError(VerificationServiceError):
    """Raised when an illegal verification workflow state transition is attempted."""
    pass


class ConcurrencyConflictError(VerificationServiceError):
    """Raised when an update is attempted with a stale version number."""
    pass


class InvalidCorrectionError(VerificationServiceError):
    """Raised when a field or line item correction violates business constraints."""
    pass


VALID_VERIFICATION_STATUSES = {
    "PENDING_REVIEW",
    "IN_REVIEW",
    "APPROVED",
    "REJECTED",
    "RESUBMITTED",
}

MONETARY_FIELDS = {
    "subtotal",
    "grand_total",
    "tax_amount",
    "unit_price",
    "line_total",
}


class DocumentVerificationService:
    """Domain service managing human review, corrections, and state machine lifecycles."""

    def __init__(self, db: Session):
        self.db = db

    def get_or_create_verification(
        self, document_id: uuid.UUID, extraction_id: Optional[uuid.UUID] = None
    ) -> DocumentVerification:
        """Retrieve existing verification session or initialize one from the latest extraction."""
        doc = self.db.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        # If a verification already exists for this document, retrieve the latest one
        existing_verification = self.db.execute(
            select(DocumentVerification)
            .where(DocumentVerification.document_id == document_id)
            .order_by(desc(DocumentVerification.created_at))
        ).scalars().first()

        if existing_verification:
            return existing_verification

        # Resolve extraction to base verification on
        if extraction_id:
            extraction = self.db.execute(
                select(DocumentExtraction).where(
                    DocumentExtraction.id == extraction_id,
                    DocumentExtraction.document_id == document_id,
                )
            ).scalar_one_or_none()
            if not extraction:
                raise ExtractionMissingError(
                    f"Extraction '{extraction_id}' not found for document '{document_id}'."
                )
        else:
            extraction = self.db.execute(
                select(DocumentExtraction)
                .where(DocumentExtraction.document_id == document_id)
                .order_by(desc(DocumentExtraction.run_number))
            ).scalars().first()

            if not extraction:
                raise ExtractionMissingError(
                    f"No extraction found for document '{document_id}'. "
                    "Execute structured data extraction before initiating verification."
                )

        # Clone extracted fields and line items to preserve immutable original extraction
        initial_fields = deepcopy(extraction.fields or {})
        initial_line_items = deepcopy(extraction.line_items or [])

        verification = DocumentVerification(
            document_id=document_id,
            extraction_id=extraction.id,
            status="PENDING_REVIEW",
            version=1,
            verified_fields=initial_fields,
            verified_line_items=initial_line_items,
        )
        self.db.add(verification)
        self.db.commit()
        self.db.refresh(verification)
        return verification

    def _lock_verification(
        self, verification_id: uuid.UUID, expected_version: int
    ) -> DocumentVerification:
        """Fetch verification session with row-level lock and verify concurrency version."""
        verification = self.db.execute(
            select(DocumentVerification)
            .where(DocumentVerification.id == verification_id)
            .with_for_update()
        ).scalar_one_or_none()

        if not verification:
            raise VerificationNotFoundError(f"Verification '{verification_id}' not found.")

        if verification.version != expected_version:
            raise ConcurrencyConflictError(
                f"Concurrent modification detected: expected version {expected_version}, "
                f"but current version is {verification.version}. Refresh and retry."
            )

        return verification

    def claim_review(
        self, verification_id: uuid.UUID, reviewer_id: str, expected_version: int
    ) -> DocumentVerification:
        """Assign reviewer and transition from PENDING_REVIEW to IN_REVIEW."""
        if not reviewer_id or not reviewer_id.strip():
            raise InvalidCorrectionError("Reviewer ID is required to claim a review.")

        verification = self._lock_verification(verification_id, expected_version)

        if verification.status not in ("PENDING_REVIEW", "IN_REVIEW"):
            raise InvalidStateTransitionError(
                f"Cannot claim review in state '{verification.status}'. Must be PENDING_REVIEW."
            )

        verification.status = "IN_REVIEW"
        verification.assigned_reviewer_id = reviewer_id.strip()
        verification.version += 1

        self.db.commit()
        self.db.refresh(verification)
        return verification

    def release_claim(
        self, verification_id: uuid.UUID, reviewer_id: str, expected_version: int
    ) -> DocumentVerification:
        """Release reviewer assignment and return to PENDING_REVIEW."""
        verification = self._lock_verification(verification_id, expected_version)

        if verification.status != "IN_REVIEW":
            raise InvalidStateTransitionError(
                f"Cannot release review claim from status '{verification.status}'. Must be IN_REVIEW."
            )

        verification.status = "PENDING_REVIEW"
        verification.assigned_reviewer_id = None
        verification.version += 1

        self.db.commit()
        self.db.refresh(verification)
        return verification

    def correct_field(
        self,
        verification_id: uuid.UUID,
        field_name: str,
        corrected_value: Any,
        corrected_by: str,
        expected_version: int,
        reason: Optional[str] = None,
    ) -> DocumentVerification:
        """Apply a field-level correction, preserving original values in the audit history."""
        verification = self._lock_verification(verification_id, expected_version)

        if verification.status in ("APPROVED", "REJECTED"):
            raise InvalidStateTransitionError(
                f"Cannot modify fields on a verification in terminal state '{verification.status}'."
            )

        if not corrected_by or not corrected_by.strip():
            raise InvalidCorrectionError("corrected_by user identifier is required.")

        field_key = field_name.strip()
        # Validate monetary or currency constraints
        if field_key in MONETARY_FIELDS and corrected_value is not None:
            try:
                dec_val = Decimal(str(corrected_value).replace("$", "").replace(",", "").strip())
                if dec_val < 0:
                    raise InvalidCorrectionError(f"Monetary field '{field_key}' cannot be negative ({dec_val}).")
                formatted_value = str(dec_val)
            except (InvalidOperation, ValueError):
                raise InvalidCorrectionError(f"Invalid monetary value '{corrected_value}' for field '{field_key}'.")
        elif field_key == "currency" and corrected_value is not None:
            code = str(corrected_value).strip().upper()
            if code not in ("USD", "$"):
                raise InvalidCorrectionError(f"Unsupported currency '{code}'. Recorda MVP supports USD only.")
            formatted_value = "USD"
        else:
            formatted_value = corrected_value

        # Capture original value before updating
        original_val = verification.verified_fields.get(field_key) if verification.verified_fields else None

        # Apply update
        if verification.verified_fields is None:
            verification.verified_fields = {}
        # Work with new dictionary to ensure SQLAlchemy detects JSON mutation
        updated_fields = dict(verification.verified_fields)
        updated_fields[field_key] = formatted_value
        verification.verified_fields = updated_fields

        # Persist immutable correction record
        correction = VerificationCorrection(
            verification_id=verification.id,
            document_id=verification.document_id,
            correction_type="FIELD",
            field_name=field_key,
            original_value=original_val,
            corrected_value=formatted_value,
            reason=reason,
            corrected_by=corrected_by.strip(),
        )
        self.db.add(correction)

        verification.version += 1
        self.db.commit()
        self.db.refresh(verification)
        return verification

    def correct_line_item(
        self,
        verification_id: uuid.UUID,
        line_item_index: int,
        item_updates: Dict[str, Any],
        corrected_by: str,
        expected_version: int,
        reason: Optional[str] = None,
    ) -> DocumentVerification:
        """Apply an update to an existing line item, recording the change in audit history."""
        verification = self._lock_verification(verification_id, expected_version)

        if verification.status in ("APPROVED", "REJECTED"):
            raise InvalidStateTransitionError(
                f"Cannot modify line items on a verification in terminal state '{verification.status}'."
            )

        if not corrected_by or not corrected_by.strip():
            raise InvalidCorrectionError("corrected_by user identifier is required.")

        line_items = list(verification.verified_line_items or [])
        if line_item_index < 0 or line_item_index >= len(line_items):
            raise InvalidCorrectionError(
                f"Line item index {line_item_index} out of range (0..{len(line_items) - 1})."
            )

        original_item = deepcopy(line_items[line_item_index])
        target_item = dict(original_item)

        # Apply and validate updates
        for k, v in item_updates.items():
            if k in ("quantity", "ordered_quantity", "dispatched_quantity", "received_quantity"):
                try:
                    q_dec = Decimal(str(v).replace(",", "").strip())
                    if q_dec <= 0:
                        raise InvalidCorrectionError(f"Quantity must be positive ({q_dec}).")
                    target_item[k] = str(q_dec)
                except (InvalidOperation, ValueError):
                    raise InvalidCorrectionError(f"Invalid quantity value '{v}'.")
            elif k in ("unit_price", "line_total"):
                try:
                    p_dec = Decimal(str(v).replace("$", "").replace(",", "").strip())
                    if p_dec < 0:
                        raise InvalidCorrectionError(f"Price/total cannot be negative ({p_dec}).")
                    target_item[k] = str(p_dec)
                except (InvalidOperation, ValueError):
                    raise InvalidCorrectionError(f"Invalid price value '{v}'.")
            else:
                target_item[k] = v

        line_items[line_item_index] = target_item
        verification.verified_line_items = line_items

        correction = VerificationCorrection(
            verification_id=verification.id,
            document_id=verification.document_id,
            correction_type="LINE_ITEM_UPDATE",
            line_item_index=line_item_index,
            original_value=original_item,
            corrected_value=target_item,
            reason=reason,
            corrected_by=corrected_by.strip(),
        )
        self.db.add(correction)

        verification.version += 1
        self.db.commit()
        self.db.refresh(verification)
        return verification

    def add_line_item(
        self,
        verification_id: uuid.UUID,
        line_item: Dict[str, Any],
        corrected_by: str,
        expected_version: int,
        reason: Optional[str] = None,
    ) -> DocumentVerification:
        """Add a new line item to verified data and record it in audit history."""
        verification = self._lock_verification(verification_id, expected_version)

        if verification.status in ("APPROVED", "REJECTED"):
            raise InvalidStateTransitionError(
                f"Cannot add line items on a verification in terminal state '{verification.status}'."
            )

        if not corrected_by or not corrected_by.strip():
            raise InvalidCorrectionError("corrected_by user identifier is required.")

        line_items = list(verification.verified_line_items or [])
        item_copy = dict(line_item)
        line_items.append(item_copy)
        verification.verified_line_items = line_items

        correction = VerificationCorrection(
            verification_id=verification.id,
            document_id=verification.document_id,
            correction_type="LINE_ITEM_ADD",
            line_item_index=len(line_items) - 1,
            original_value=None,
            corrected_value=item_copy,
            reason=reason,
            corrected_by=corrected_by.strip(),
        )
        self.db.add(correction)

        verification.version += 1
        self.db.commit()
        self.db.refresh(verification)
        return verification

    def delete_line_item(
        self,
        verification_id: uuid.UUID,
        line_item_index: int,
        corrected_by: str,
        expected_version: int,
        reason: Optional[str] = None,
    ) -> DocumentVerification:
        """Remove a line item from verified data and record it in audit history."""
        verification = self._lock_verification(verification_id, expected_version)

        if verification.status in ("APPROVED", "REJECTED"):
            raise InvalidStateTransitionError(
                f"Cannot delete line items on a verification in terminal state '{verification.status}'."
            )

        line_items = list(verification.verified_line_items or [])
        if line_item_index < 0 or line_item_index >= len(line_items):
            raise InvalidCorrectionError(
                f"Line item index {line_item_index} out of range (0..{len(line_items) - 1})."
            )

        deleted_item = line_items.pop(line_item_index)
        verification.verified_line_items = line_items

        correction = VerificationCorrection(
            verification_id=verification.id,
            document_id=verification.document_id,
            correction_type="LINE_ITEM_DELETE",
            line_item_index=line_item_index,
            original_value=deleted_item,
            corrected_value=None,
            reason=reason,
            corrected_by=corrected_by.strip(),
        )
        self.db.add(correction)

        verification.version += 1
        self.db.commit()
        self.db.refresh(verification)
        return verification

    def approve_verification(
        self,
        verification_id: uuid.UUID,
        reviewer_id: str,
        expected_version: int,
        notes: Optional[str] = None,
    ) -> DocumentVerification:
        """Approve verification session and sync verified fields to Document & ExtractedData."""
        if not reviewer_id or not reviewer_id.strip():
            raise InvalidCorrectionError("Reviewer ID is required to approve.")

        verification = self._lock_verification(verification_id, expected_version)

        if verification.status not in ("PENDING_REVIEW", "IN_REVIEW"):
            raise InvalidStateTransitionError(
                f"Cannot approve verification in state '{verification.status}'. Must be PENDING_REVIEW or IN_REVIEW."
            )

        verification.status = "APPROVED"
        verification.reviewed_by = reviewer_id.strip()
        verification.reviewed_at = datetime.now(timezone.utc)
        verification.notes = notes
        verification.version += 1

        # Synchronize with Document and ExtractedDocumentData
        doc = self.db.execute(
            select(Document).where(Document.id == verification.document_id)
        ).scalar_one_or_none()
        if doc:
            doc.status = "VERIFIED"
            fields = verification.verified_fields or {}
            for id_key in ("po_number", "dc_number", "grn_number", "invoice_number"):
                if fields.get(id_key):
                    doc.document_number = fields[id_key]
                    break
            if fields.get("po_reference"):
                doc.po_reference = fields["po_reference"]

            # Synchronize ExtractedDocumentData
            if doc.extracted_data:
                doc.extracted_data.fields = verification.verified_fields
                doc.extracted_data.line_items = verification.verified_line_items

        self.db.commit()
        self.db.refresh(verification)
        return verification

    def reject_verification(
        self,
        verification_id: uuid.UUID,
        reviewer_id: str,
        expected_version: int,
        reason: str,
        notes: Optional[str] = None,
    ) -> DocumentVerification:
        """Reject document verification with a mandatory reason."""
        if not reviewer_id or not reviewer_id.strip():
            raise InvalidCorrectionError("Reviewer ID is required to reject.")
        if not reason or not reason.strip():
            raise InvalidCorrectionError("Rejection reason is mandatory.")

        verification = self._lock_verification(verification_id, expected_version)

        if verification.status not in ("PENDING_REVIEW", "IN_REVIEW"):
            raise InvalidStateTransitionError(
                f"Cannot reject verification in state '{verification.status}'. Must be PENDING_REVIEW or IN_REVIEW."
            )

        verification.status = "REJECTED"
        verification.reviewed_by = reviewer_id.strip()
        verification.reviewed_at = datetime.now(timezone.utc)
        verification.rejection_reason = reason.strip()
        verification.notes = notes
        verification.version += 1

        # Update Document status
        doc = self.db.execute(
            select(Document).where(Document.id == verification.document_id)
        ).scalar_one_or_none()
        if doc:
            doc.status = "REJECTED"

        self.db.commit()
        self.db.refresh(verification)
        return verification

    def resubmit_verification(
        self,
        verification_id: uuid.UUID,
        actor_id: str,
        expected_version: int,
        reason: Optional[str] = None,
    ) -> DocumentVerification:
        """Resubmit a previously rejected verification for fresh review."""
        if not actor_id or not actor_id.strip():
            raise InvalidCorrectionError("Actor ID is required to resubmit.")

        verification = self._lock_verification(verification_id, expected_version)

        if verification.status != "REJECTED":
            raise InvalidStateTransitionError(
                f"Cannot resubmit verification in state '{verification.status}'. Only REJECTED verifications can be resubmitted."
            )

        verification.status = "PENDING_REVIEW"
        verification.assigned_reviewer_id = None
        verification.rejection_reason = None
        verification.notes = f"Resubmitted by {actor_id}: {reason}" if reason else f"Resubmitted by {actor_id}"
        verification.version += 1

        doc = self.db.execute(
            select(Document).where(Document.id == verification.document_id)
        ).scalar_one_or_none()
        if doc:
            doc.status = "INGESTED"

        self.db.commit()
        self.db.refresh(verification)
        return verification

    def get_verification(self, verification_id: uuid.UUID) -> Optional[DocumentVerification]:
        """Retrieve a specific verification session by ID."""
        return self.db.execute(
            select(DocumentVerification).where(DocumentVerification.id == verification_id)
        ).scalar_one_or_none()

    def get_corrections(self, verification_id: uuid.UUID) -> List[VerificationCorrection]:
        """Retrieve full immutable audit history of corrections for a verification session."""
        return list(
            self.db.execute(
                select(VerificationCorrection)
                .where(VerificationCorrection.verification_id == verification_id)
                .order_by(VerificationCorrection.created_at.asc())
            ).scalars().all()
        )
