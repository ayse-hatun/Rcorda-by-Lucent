from decimal import Decimal
from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from apps.api.database import get_db
from apps.api.models import PurchaseOrder, PurchaseOrderItem, Supplier
from apps.api.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderItemResponse,
    PurchaseOrderResponse,
    PurchaseOrderUpdate,
)

router = APIRouter()


@router.post(
    "",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new Purchase Order with line items",
)
def create_purchase_order(
    payload: PurchaseOrderCreate, db: Session = Depends(get_db)
):
    """
    Create a new Purchase Order with multiple line items.
    Validates supplier existence, unique PO number, positive quantities,
    non-negative prices, explicit currency, and calculates Decimal totals.
    """
    # 1. Check duplicate PO number
    existing_po = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.po_number == payload.po_number)
    ).scalar_one_or_none()
    if existing_po:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Purchase order with number '{payload.po_number}' already exists",
        )

    # 2. Check supplier exists if supplier_id is provided
    if payload.supplier_id is not None:
        supplier = db.execute(
            select(Supplier).where(Supplier.id == payload.supplier_id)
        ).scalar_one_or_none()
        if not supplier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Supplier with ID '{payload.supplier_id}' does not exist",
            )

    # 3. Process line items and calculate Decimal totals
    po_items: List[PurchaseOrderItem] = []
    calculated_total = Decimal("0.0000")

    for item_in in payload.items:
        # Calculate line item total price if not explicitly provided
        line_total = (
            item_in.total_price
            if item_in.total_price is not None
            else (item_in.ordered_quantity * item_in.unit_price)
        )
        calculated_total += line_total

        # Set default po_item_id if omitted for delivery schedule compatibility
        po_item_id = item_in.po_item_id or f"{payload.po_number}-{item_in.line_item_id}"

        po_item = PurchaseOrderItem(
            po_item_id=po_item_id,
            line_item_id=item_in.line_item_id,
            item_code=item_in.item_code,
            description=item_in.description,
            ordered_quantity=item_in.ordered_quantity,
            unit=item_in.unit,
            unit_price=item_in.unit_price,
            total_price=line_total,
        )
        po_items.append(po_item)

    # 4. Construct Purchase Order
    po = PurchaseOrder(
        po_number=payload.po_number,
        supplier_id=payload.supplier_id,
        order_date=payload.order_date,
        total_amount=calculated_total,
        currency=payload.currency,
        status=payload.status,
        items=po_items,
    )

    try:
        db.add(po)
        db.commit()
        # Reload with relationships
        saved_po = db.execute(
            select(PurchaseOrder)
            .options(
                selectinload(PurchaseOrder.supplier),
                selectinload(PurchaseOrder.items),
            )
            .where(PurchaseOrder.id == po.id)
        ).scalar_one()
        return saved_po
    except IntegrityError as e:
        db.rollback()
        if "po_number" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Purchase order with number '{payload.po_number}' already exists",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database integrity constraint violation",
        )


@router.get(
    "",
    response_model=List[PurchaseOrderResponse],
    status_code=status.HTTP_200_OK,
    summary="List Purchase Orders with pagination and filters",
)
def list_purchase_orders(
    limit: int = Query(50, ge=1, le=100, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    status: Optional[str] = Query(None, description="Filter by status"),
    supplier_id: Optional[uuid.UUID] = Query(None, description="Filter by supplier ID"),
    search: Optional[str] = Query(None, description="Search by PO number"),
    db: Session = Depends(get_db),
):
    """List purchase orders with pagination, status/supplier filtering, and PO number search."""
    stmt = (
        select(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.items),
        )
        .order_by(PurchaseOrder.created_at.desc())
    )

    if status:
        stmt = stmt.where(PurchaseOrder.status == status.strip())
    if supplier_id:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
    if search:
        search_term = f"%{search.strip()}%"
        stmt = stmt.where(PurchaseOrder.po_number.ilike(search_term))

    stmt = stmt.offset(offset).limit(limit)
    results = db.execute(stmt).scalars().all()
    return results


@router.get(
    "/{po_id}",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Purchase Order by ID with items and supplier",
)
def get_purchase_order(po_id: uuid.UUID, db: Session = Depends(get_db)):
    """Retrieve a single Purchase Order with its line items and supplier details."""
    po = db.execute(
        select(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.items),
        )
        .where(PurchaseOrder.id == po_id)
    ).scalar_one_or_none()

    if not po:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Purchase order with ID '{po_id}' not found",
        )

    return po


@router.get(
    "/{po_id}/items",
    response_model=List[PurchaseOrderItemResponse],
    status_code=status.HTTP_200_OK,
    summary="Get line items for a Purchase Order",
)
def get_purchase_order_items(po_id: uuid.UUID, db: Session = Depends(get_db)):
    """Retrieve all line items for a specific Purchase Order."""
    # Ensure PO exists
    po_exists = db.execute(
        select(PurchaseOrder.id).where(PurchaseOrder.id == po_id)
    ).scalar_one_or_none()

    if not po_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Purchase order with ID '{po_id}' not found",
        )

    items = db.execute(
        select(PurchaseOrderItem)
        .where(PurchaseOrderItem.purchase_order_id == po_id)
        .order_by(PurchaseOrderItem.line_item_id.asc())
    ).scalars().all()

    return items


@router.patch(
    "/{po_id}",
    response_model=PurchaseOrderResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Purchase Order details",
)
def update_purchase_order(
    po_id: uuid.UUID,
    payload: PurchaseOrderUpdate,
    db: Session = Depends(get_db),
):
    """Update purchase order status, order date, currency, or supplier."""
    po = db.execute(
        select(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.supplier),
            selectinload(PurchaseOrder.items),
        )
        .where(PurchaseOrder.id == po_id)
    ).scalar_one_or_none()

    if not po:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Purchase order with ID '{po_id}' not found",
        )

    # Check unique PO number if updating po_number
    if payload.po_number is not None and payload.po_number != po.po_number:
        existing = db.execute(
            select(PurchaseOrder).where(PurchaseOrder.po_number == payload.po_number)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Purchase order with number '{payload.po_number}' already exists",
            )
        po.po_number = payload.po_number

    # Check supplier exists if updating supplier_id
    if payload.supplier_id is not None and payload.supplier_id != po.supplier_id:
        supplier = db.execute(
            select(Supplier).where(Supplier.id == payload.supplier_id)
        ).scalar_one_or_none()
        if not supplier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Supplier with ID '{payload.supplier_id}' does not exist",
            )
        po.supplier_id = payload.supplier_id

    if payload.order_date is not None:
        po.order_date = payload.order_date
    if payload.currency is not None:
        po.currency = payload.currency
    if payload.status is not None:
        po.status = payload.status

    try:
        db.commit()
        db.refresh(po)
        return po
    except IntegrityError as e:
        db.rollback()
        if "po_number" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Purchase order with number '{payload.po_number}' already exists",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database integrity constraint violation",
        )
