"""FastAPI route handlers for document reconciliation."""

import logging
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.schemas import (
    ReconcileTransactionRequest,
    ReconciliationRunResponse,
)
from apps.api.services.reconciliation_exception_service import (
    ExceptionManagementError,
    ReconciliationExceptionService,
)
from apps.api.services.reconciliation_service import (
    DocumentNotFoundError,
    ReconciliationError,
    ReconciliationService,
    ToleranceConfig,
    UnapprovedDocumentError,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def get_reconciliation_service(db: Session = Depends(get_db)) -> ReconciliationService:
    return ReconciliationService(db)


@router.post(
    "/{document_id}/reconcile",
    response_model=ReconciliationRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute deterministic 3-way/4-way reconciliation on a transaction chain",
)
def reconcile_document(
    document_id: uuid.UUID,
    payload: Optional[ReconcileTransactionRequest] = None,
    service: ReconciliationService = Depends(get_reconciliation_service),
):
    tolerance = None
    actor = "SYSTEM"
    if payload:
        actor = payload.actor or "SYSTEM"
        tolerance = ToleranceConfig(
            price_tolerance_pct=payload.tolerance_price_pct or 0,
            price_tolerance_flat_usd=payload.tolerance_price_usd or 0,
            qty_tolerance_pct=payload.tolerance_qty_pct or 0,
            allow_missing_dc=payload.allow_missing_dc,
        )

    try:
        run = service.reconcile_transaction(
            document_id=document_id,
            tolerance=tolerance,
            actor=actor,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except UnapprovedDocumentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ReconciliationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        ReconciliationExceptionService(service.db).create_from_reconciliation_run(
            reconciliation_run_id=run.id,
            actor=actor,
        )
    except ExceptionManagementError:
        logger.exception(
            "Failed to project reconciliation run %s into exceptions.", run.id
        )
    except SQLAlchemyError:
        service.db.rollback()
        logger.exception(
            "Database failure while projecting reconciliation run %s into exceptions.",
            run.id,
        )

    return run


@router.get(
    "/{document_id}/reconciliations",
    response_model=List[ReconciliationRunResponse],
    summary="List all historical reconciliation runs for a root document",
)
def list_document_reconciliations(
    document_id: uuid.UUID,
    service: ReconciliationService = Depends(get_reconciliation_service),
):
    try:
        return service.get_reconciliations_for_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/{document_id}/reconciliations/latest",
    response_model=ReconciliationRunResponse,
    summary="Retrieve the most recent reconciliation run for a document",
)
def get_latest_document_reconciliation(
    document_id: uuid.UUID,
    service: ReconciliationService = Depends(get_reconciliation_service),
):
    run = service.get_latest_reconciliation(document_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No reconciliation run found for document '{document_id}'.",
        )
    return run
