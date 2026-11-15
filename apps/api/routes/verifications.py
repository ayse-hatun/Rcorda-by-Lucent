"""FastAPI route handlers for document human verification and review workflows."""

from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.schemas import (
    AddLineItemRequest,
    ApproveVerificationRequest,
    ClaimReviewRequest,
    CorrectFieldRequest,
    CorrectLineItemRequest,
    CorrectionResponse,
    DeleteLineItemRequest,
    RejectVerificationRequest,
    ReleaseReviewRequest,
    ResubmitVerificationRequest,
    VerificationResponse,
)
from apps.api.services.ocr_service import DocumentNotFoundError
from apps.api.services.verification_service import (
    ConcurrencyConflictError,
    DocumentVerificationService,
    ExtractionMissingError,
    InvalidCorrectionError,
    InvalidStateTransitionError,
    VerificationNotFoundError,
    VerificationServiceError,
)

router = APIRouter()


def get_verification_service(db: Session = Depends(get_db)) -> DocumentVerificationService:
    return DocumentVerificationService(db)


def _resolve_verification_id(
    document_id: uuid.UUID,
    service: DocumentVerificationService,
) -> uuid.UUID:
    """Helper to resolve the active verification session id for a document."""
    verification = service.get_or_create_verification(document_id)
    return verification.id


@router.get(
    "/{document_id}/verification",
    response_model=VerificationResponse,
    summary="Get or initialize verification session for a document",
)
def get_or_create_verification(
    document_id: uuid.UUID,
    extraction_id: Optional[uuid.UUID] = Query(None, description="Optional specific extraction run ID"),
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        return service.get_or_create_verification(document_id, extraction_id=extraction_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except VerificationServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/{document_id}/verification/claim",
    response_model=VerificationResponse,
    summary="Claim a verification task for review",
)
def claim_verification(
    document_id: uuid.UUID,
    payload: ClaimReviewRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.claim_review(
            verification_id=v_id,
            reviewer_id=payload.reviewer_id,
            expected_version=payload.expected_version,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except VerificationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/release",
    response_model=VerificationResponse,
    summary="Release claimed review back to pending pool",
)
def release_verification_claim(
    document_id: uuid.UUID,
    payload: ReleaseReviewRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.release_claim(
            verification_id=v_id,
            reviewer_id=payload.reviewer_id,
            expected_version=payload.expected_version,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/correct-field",
    response_model=VerificationResponse,
    summary="Correct a specific field value on the verification session",
)
def correct_verification_field(
    document_id: uuid.UUID,
    payload: CorrectFieldRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.correct_field(
            verification_id=v_id,
            field_name=payload.field_name,
            corrected_value=payload.corrected_value,
            corrected_by=payload.corrected_by,
            expected_version=payload.expected_version,
            reason=payload.reason,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/correct-line-item",
    response_model=VerificationResponse,
    summary="Correct fields on an existing line item",
)
def correct_verification_line_item(
    document_id: uuid.UUID,
    payload: CorrectLineItemRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.correct_line_item(
            verification_id=v_id,
            line_item_index=payload.line_item_index,
            item_updates=payload.item_updates,
            corrected_by=payload.corrected_by,
            expected_version=payload.expected_version,
            reason=payload.reason,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/add-line-item",
    response_model=VerificationResponse,
    summary="Add a new line item to verified data",
)
def add_verification_line_item(
    document_id: uuid.UUID,
    payload: AddLineItemRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.add_line_item(
            verification_id=v_id,
            line_item=payload.line_item,
            corrected_by=payload.corrected_by,
            expected_version=payload.expected_version,
            reason=payload.reason,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/delete-line-item",
    response_model=VerificationResponse,
    summary="Delete a line item from verified data",
)
def delete_verification_line_item(
    document_id: uuid.UUID,
    payload: DeleteLineItemRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.delete_line_item(
            verification_id=v_id,
            line_item_index=payload.line_item_index,
            corrected_by=payload.corrected_by,
            expected_version=payload.expected_version,
            reason=payload.reason,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/approve",
    response_model=VerificationResponse,
    summary="Approve verification session and synchronize verified fields",
)
def approve_verification(
    document_id: uuid.UUID,
    payload: ApproveVerificationRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.approve_verification(
            verification_id=v_id,
            reviewer_id=payload.reviewer_id,
            expected_version=payload.expected_version,
            notes=payload.notes,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/reject",
    response_model=VerificationResponse,
    summary="Reject document verification with reason",
)
def reject_verification(
    document_id: uuid.UUID,
    payload: RejectVerificationRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.reject_verification(
            verification_id=v_id,
            reviewer_id=payload.reviewer_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            notes=payload.notes,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post(
    "/{document_id}/verification/resubmit",
    response_model=VerificationResponse,
    summary="Resubmit rejected verification for re-review",
)
def resubmit_verification(
    document_id: uuid.UUID,
    payload: ResubmitVerificationRequest,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.resubmit_verification(
            verification_id=v_id,
            actor_id=payload.actor_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
        )
    except (DocumentNotFoundError, VerificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ConcurrencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get(
    "/{document_id}/verification/corrections",
    response_model=List[CorrectionResponse],
    summary="Get complete immutable correction audit trail",
)
def get_verification_corrections(
    document_id: uuid.UUID,
    service: DocumentVerificationService = Depends(get_verification_service),
):
    try:
        v_id = _resolve_verification_id(document_id, service)
        return service.get_corrections(v_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ExtractionMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except VerificationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
