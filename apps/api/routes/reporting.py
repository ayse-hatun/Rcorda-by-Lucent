"""FastAPI routes for read-only dashboard reporting."""

from datetime import date
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.schemas import (
    ReconciliationExceptionListResponse,
    ReconciliationReportResponse,
)
from apps.api.services.reconciliation_exception_service import (
    ReconciliationRunNotFoundError,
)
from apps.api.services.reporting_service import ReportingService

router = APIRouter()


def get_reporting_service(db: Session = Depends(get_db)) -> ReportingService:
    return ReportingService(db)


@router.get(
    "/exceptions",
    response_model=ReconciliationExceptionListResponse,
    summary="List persisted exceptions with filters and pagination",
)
def list_reporting_exceptions(
    exception_type: Optional[str] = Query(None, description="Filter by exception type"),
    type_filter: Optional[str] = Query(
        None, alias="type", description="Alias for exception_type"
    ),
    severity: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    supplier: Optional[str] = Query(None, description="Filter by supplier name"),
    supplier_name: Optional[str] = Query(None, description="Alias for supplier"),
    po_number: Optional[str] = Query(None, description="Filter by PO reference"),
    po_reference: Optional[str] = Query(None, description="Alias for po_number"),
    date_from: Optional[date] = Query(None, description="Created on or after this date"),
    date_to: Optional[date] = Query(None, description="Created on or before this date"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service: ReportingService = Depends(get_reporting_service),
):
    items, total = service.list_exceptions(
        exception_type=exception_type or type_filter,
        severity=severity,
        status=status_filter,
        supplier=supplier or supplier_name,
        po_number=po_number or po_reference,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return ReconciliationExceptionListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/reconciliation-runs/{run_id}",
    response_model=ReconciliationReportResponse,
    summary="Get a detailed report for a persisted reconciliation run",
)
@router.get(
    "/reconciliation/{run_id}",
    response_model=ReconciliationReportResponse,
    include_in_schema=False,
)
def get_reconciliation_report(
    run_id: uuid.UUID,
    service: ReportingService = Depends(get_reporting_service),
):
    try:
        return service.get_reconciliation_report(run_id)
    except ReconciliationRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        )
