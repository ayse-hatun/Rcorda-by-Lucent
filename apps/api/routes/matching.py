"""FastAPI route handlers for document matching and relationship management."""

from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.schemas import (
    DocumentMatchAuditResponse,
    DocumentRelationshipResponse,
    DocumentRelationshipsChainResponse,
    ManualLinkRequest,
    MatchSummaryResponse,
    ResolveRelationshipRequest,
)
from apps.api.services.matching_service import (
    AmbiguousMatchError,
    DocumentMatchingService,
    DocumentNotVerifiedError,
    IncompatibleRelationshipError,
    MatchingServiceError,
    RelationshipNotFoundError,
)
from apps.api.services.ocr_service import DocumentNotFoundError

router = APIRouter()


def get_matching_service(db: Session = Depends(get_db)) -> DocumentMatchingService:
    return DocumentMatchingService(db)


@router.post(
    "/{document_id}/match",
    response_model=MatchSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Run deterministic document-type matching for a verified document",
)
def match_document(
    document_id: uuid.UUID,
    actor: Optional[str] = Query("SYSTEM", description="Actor triggering matching"),
    service: DocumentMatchingService = Depends(get_matching_service),
):
    try:
        return service.match_document(document_id, actor=actor or "SYSTEM")
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except DocumentNotVerifiedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except IncompatibleRelationshipError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except MatchingServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get(
    "/{document_id}/relationships",
    response_model=DocumentRelationshipsChainResponse,
    summary="Get all incoming and outgoing relationships and complete transaction chain for a document",
)
def get_document_relationships(
    document_id: uuid.UUID,
    service: DocumentMatchingService = Depends(get_matching_service),
):
    try:
        return service.get_document_relationships(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post(
    "/{document_id}/relationships/manual",
    response_model=DocumentRelationshipResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a manual link between two verified documents with audit tracking",
)
def manual_link_document(
    document_id: uuid.UUID,
    payload: ManualLinkRequest,
    service: DocumentMatchingService = Depends(get_matching_service),
):
    try:
        return service.manual_link(
            source_document_id=document_id,
            target_document_id=payload.target_document_id,
            relationship_type=payload.relationship_type,
            actor=payload.actor,
            reason=payload.reason,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except DocumentNotVerifiedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except IncompatibleRelationshipError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except MatchingServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/{document_id}/relationships/{relationship_id}/resolve",
    response_model=DocumentRelationshipResponse,
    summary="Resolve an ambiguous or proposed document relationship",
)
def resolve_document_relationship(
    document_id: uuid.UUID,
    relationship_id: uuid.UUID,
    payload: ResolveRelationshipRequest,
    service: DocumentMatchingService = Depends(get_matching_service),
):
    try:
        # Validate that document exists
        service._get_document(document_id)
        return service.resolve_relationship(
            relationship_id=relationship_id,
            action=payload.action,
            actor=payload.actor,
            reason=payload.reason,
        )
    except (DocumentNotFoundError, RelationshipNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except IncompatibleRelationshipError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except MatchingServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.delete(
    "/{document_id}/relationships/{relationship_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink/delete a document relationship with an audit trail entry",
)
def unlink_document_relationship(
    document_id: uuid.UUID,
    relationship_id: uuid.UUID,
    actor: str = Query(..., description="User unlinking relationship"),
    reason: Optional[str] = Query(None, description="Reason for unlinking"),
    service: DocumentMatchingService = Depends(get_matching_service),
):
    try:
        service._get_document(document_id)
        service.remove_relationship(relationship_id=relationship_id, actor=actor, reason=reason)
    except (DocumentNotFoundError, RelationshipNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/{document_id}/relationships/audit",
    response_model=List[DocumentMatchAuditResponse],
    summary="Get complete match evaluation and decision audit trail for a document",
)
def get_document_match_audit(
    document_id: uuid.UUID,
    service: DocumentMatchingService = Depends(get_matching_service),
):
    try:
        service._get_document(document_id)
        return service.get_match_audit_trail(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
