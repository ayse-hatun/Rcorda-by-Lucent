"""FastAPI routes for persisted reconciliation exception management."""

from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.schemas import (
    CreateReconciliationExceptionRequest,
    CreateReconciliationExceptionsFromRunRequest,
    ReconciliationExceptionHistoryResponse,
    ReconciliationExceptionResponse,
    ResolveReconciliationExceptionRequest,
    UpdateReconciliationExceptionStatusRequest,
)
from apps.api.services.reconciliation_exception_service import (
    DiscrepancyNotFoundError,
    ExceptionManagementError,
    InvalidExceptionStatusError,
    InvalidExceptionTransitionError,
    InvalidResolutionError,
    ReconciliationExceptionNotFoundError,
    ReconciliationExceptionService,
    ReconciliationRunNotFoundError,
)

router = APIRouter()


def get_exception_service(
    db: Session = Depends(get_db),
) -> ReconciliationExceptionService:
    return ReconciliationExceptionService(db)


@router.post(
    "/from-run/{reconciliation_run_id}",
    response_model=List[ReconciliationExceptionResponse],
    status_code=status.HTTP_200_OK,
    summary="Create exceptions for all discrepancies in a reconciliation run",
)
def create_exceptions_from_run(
    reconciliation_run_id: uuid.UUID,
    payload: Optional[CreateReconciliationExceptionsFromRunRequest] = None,
    service: ReconciliationExceptionService = Depends(get_exception_service),
):
    actor = payload.actor if payload else "SYSTEM"
    try:
        return service.create_from_reconciliation_run(
            reconciliation_run_id=reconciliation_run_id,
            actor=actor,
        )
    except ReconciliationRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post(
    "/from-discrepancy",
    response_model=ReconciliationExceptionResponse,
    status_code=status.HTTP_200_OK,
    summary="Create (or return existing) exception for a reconciliation discrepancy",
)
def create_exception_from_discrepancy(
    payload: CreateReconciliationExceptionRequest,
    service: ReconciliationExceptionService = Depends(get_exception_service),
):
    try:
        return service.create_from_discrepancy(
            discrepancy_id=payload.discrepancy_id,
            actor=payload.actor,
        )
    except (DiscrepancyNotFoundError, ReconciliationRunNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "",
    response_model=List[ReconciliationExceptionResponse],
    summary="List reconciliation exceptions with optional filters",
)
def list_exceptions(
    status_filter: Optional[str] = Query(None, alias="status"),
    severity: Optional[str] = Query(None),
    exception_type: Optional[str] = Query(None),
    supplier_name: Optional[str] = Query(None),
    po_reference: Optional[str] = Query(None),
    reconciliation_run_id: Optional[uuid.UUID] = Query(None),
    discrepancy_id: Optional[uuid.UUID] = Query(None),
    service: ReconciliationExceptionService = Depends(get_exception_service),
):
    return service.list_exceptions(
        status=status_filter,
        severity=severity,
        exception_type=exception_type,
        supplier_name=supplier_name,
        po_reference=po_reference,
        reconciliation_run_id=reconciliation_run_id,
        discrepancy_id=discrepancy_id,
    )


@router.get(
    "/{exception_id}",
    response_model=ReconciliationExceptionResponse,
    summary="Retrieve a reconciliation exception with its audit history",
)
def get_exception(
    exception_id: uuid.UUID,
    service: ReconciliationExceptionService = Depends(get_exception_service),
):
    try:
        return service.get_exception(exception_id)
    except ReconciliationExceptionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.patch(
    "/{exception_id}/status",
    response_model=ReconciliationExceptionResponse,
    summary="Apply a validated exception lifecycle status transition",
)
def update_exception_status(
    exception_id: uuid.UUID,
    payload: UpdateReconciliationExceptionStatusRequest,
    service: ReconciliationExceptionService = Depends(get_exception_service),
):
    try:
        return service.update_status(
            exception_id=exception_id,
            new_status=payload.status,
            actor=payload.actor,
            note=payload.note,
            supporting_information=payload.supporting_information,
        )
    except ReconciliationExceptionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidExceptionStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except InvalidExceptionTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ExceptionManagementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/{exception_id}/resolve",
    response_model=ReconciliationExceptionResponse,
    summary="Resolve an exception with mandatory resolution notes",
)
def resolve_exception(
    exception_id: uuid.UUID,
    payload: ResolveReconciliationExceptionRequest,
    service: ReconciliationExceptionService = Depends(get_exception_service),
):
    try:
        return service.resolve(
            exception_id=exception_id,
            resolution_notes=payload.resolution_notes,
            actor=payload.actor,
            supporting_information=payload.supporting_information,
        )
    except ReconciliationExceptionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except InvalidExceptionTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ExceptionManagementError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get(
    "/{exception_id}/history",
    response_model=List[ReconciliationExceptionHistoryResponse],
    summary="Retrieve the immutable audit history of an exception",
)
def get_exception_history(
    exception_id: uuid.UUID,
    service: ReconciliationExceptionService = Depends(get_exception_service),
):
    try:
        return service.get_history(exception_id)
    except ReconciliationExceptionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
