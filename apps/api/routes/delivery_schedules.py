import uuid
from datetime import date
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apps.api.database import get_db
from packages.domain.models import (
    DeliverySchedule,
    DeliveryValidationRun,
    DocumentType,
    EvidenceDocument,
    PurchaseOrderItem,
    QuantityAllocation,
    QuantityAllocationType,
    ScheduleAmendment,
)
from packages.reconciliation.pg_delivery_schedule_store import (
    EvidenceNotFoundError,
    PgDeliveryScheduleStore,
    ScheduleNotFoundError,
)


router = APIRouter()


def get_schedule_store(db: Session = Depends(get_db)) -> PgDeliveryScheduleStore:
    """FastAPI dependency: PostgreSQL-backed delivery schedule store."""
    return PgDeliveryScheduleStore(db)


# ---------------------------------------------------------------------------
# Request schemas (unchanged from original)
# ---------------------------------------------------------------------------


class CreatePOItemRequest(BaseModel):
    po_reference: str
    line_item_id: str
    item_code: Optional[str] = None
    description: str
    ordered_quantity: Decimal = Field(gt=0)
    unit: str = "pcs"
    po_item_id: Optional[str] = None


class CreateScheduleRequest(BaseModel):
    planned_quantity: Decimal = Field(gt=0)
    expected_delivery_date: date


class ProposeAmendmentRequest(BaseModel):
    proposed_quantity: Decimal = Field(gt=0)
    proposed_expected_date: date
    requested_by: str = Field(min_length=1)


class ApproveAmendmentRequest(BaseModel):
    approved_by: str = Field(min_length=1)


class CreateAllocationRequest(BaseModel):
    allocation_type: QuantityAllocationType
    source_document_type: DocumentType
    source_document_id: str
    source_line_item_id: str
    source_line_quantity: Decimal = Field(gt=0)
    evidence_date: date
    po_item_id: str
    schedule_id: Optional[str] = None
    quantity: Decimal = Field(gt=0)
    matched_dispatch_allocation_id: Optional[str] = None


class ReconcileRequest(BaseModel):
    evaluation_date: date


class POItemScheduleBundle(BaseModel):
    po_item: PurchaseOrderItem
    schedules: List[DeliverySchedule]


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


def _raise_http_error(error: Exception) -> None:
    if isinstance(error, (ScheduleNotFoundError, EvidenceNotFoundError)):
        raise HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        raise HTTPException(status_code=422, detail=str(error))
    raise error


# ---------------------------------------------------------------------------
# Endpoints (paths and status codes unchanged)
# ---------------------------------------------------------------------------


@router.post(
    "/source-documents",
    response_model=EvidenceDocument,
    status_code=status.HTTP_201_CREATED,
)
def register_source_document(
    document: EvidenceDocument,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        result = store.register_evidence_document(document)
        store._session.commit()
        return result
    except (ScheduleNotFoundError, EvidenceNotFoundError, ValueError) as error:
        store._session.rollback()
        _raise_http_error(error)


@router.post(
    "/po-items",
    response_model=PurchaseOrderItem,
    status_code=status.HTTP_201_CREATED,
)
def create_po_item(
    request: CreatePOItemRequest,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        result = store.create_po_item(**request.model_dump())
        store._session.commit()
        return result
    except (ScheduleNotFoundError, ValueError) as error:
        store._session.rollback()
        _raise_http_error(error)


@router.get("/po-items/{po_item_id}", response_model=POItemScheduleBundle)
def get_po_item_schedules(
    po_item_id: str,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        return POItemScheduleBundle(
            po_item=store.get_po_item(po_item_id),
            schedules=store.list_schedules(po_item_id),
        )
    except ScheduleNotFoundError as error:
        _raise_http_error(error)


@router.post(
    "/po-items/{po_item_id}/schedules",
    response_model=DeliverySchedule,
    status_code=status.HTTP_201_CREATED,
)
def create_delivery_schedule(
    po_item_id: str,
    request: CreateScheduleRequest,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        result = store.create_schedule(po_item_id=po_item_id, **request.model_dump())
        store._session.commit()
        return result
    except (ScheduleNotFoundError, ValueError) as error:
        store._session.rollback()
        _raise_http_error(error)


@router.post(
    "/schedules/{schedule_id}/amendments",
    response_model=ScheduleAmendment,
    status_code=status.HTTP_201_CREATED,
)
def propose_schedule_amendment(
    schedule_id: str,
    request: ProposeAmendmentRequest,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        result = store.propose_amendment(schedule_id=schedule_id, **request.model_dump())
        store._session.commit()
        return result
    except (ScheduleNotFoundError, ValueError) as error:
        store._session.rollback()
        _raise_http_error(error)


@router.post(
    "/amendments/{amendment_id}/approve",
    response_model=ScheduleAmendment,
)
def approve_schedule_amendment(
    amendment_id: str,
    request: ApproveAmendmentRequest,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        result = store.approve_amendment(amendment_id, request.approved_by)
        store._session.commit()
        return result
    except (ScheduleNotFoundError, ValueError) as error:
        store._session.rollback()
        _raise_http_error(error)


@router.post(
    "/allocations",
    response_model=QuantityAllocation,
    status_code=status.HTTP_201_CREATED,
)
def create_quantity_allocation(
    request: CreateAllocationRequest,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        allocation = QuantityAllocation(
            allocation_id=uuid.uuid4().hex,
            **request.model_dump(),
        )
        result = store.allocate(allocation)
        store._session.commit()
        return result
    except (ScheduleNotFoundError, EvidenceNotFoundError, ValueError) as error:
        store._session.rollback()
        _raise_http_error(error)


@router.post(
    "/po-items/{po_item_id}/reconcile",
    response_model=DeliveryValidationRun,
)
def reconcile_delivery_schedules(
    po_item_id: str,
    request: ReconcileRequest,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        result = store.run_reconciliation(po_item_id, request.evaluation_date)
        store._session.commit()
        return result
    except (ScheduleNotFoundError, ValueError) as error:
        store._session.rollback()
        _raise_http_error(error)


@router.get(
    "/po-items/{po_item_id}/validation-runs",
    response_model=List[DeliveryValidationRun],
)
def list_delivery_validation_runs(
    po_item_id: str,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        return store.list_validation_runs(po_item_id)
    except ScheduleNotFoundError as error:
        _raise_http_error(error)


@router.get(
    "/po-items/{po_item_id}/allocations",
    response_model=List[QuantityAllocation],
)
def list_quantity_allocations(
    po_item_id: str,
    store: PgDeliveryScheduleStore = Depends(get_schedule_store),
):
    try:
        return store.list_allocations(po_item_id)
    except ScheduleNotFoundError as error:
        _raise_http_error(error)
