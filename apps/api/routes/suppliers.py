from typing import List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.models import Supplier
from apps.api.schemas import SupplierCreate, SupplierResponse, SupplierUpdate

router = APIRouter()


@router.post(
    "",
    response_model=SupplierResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new supplier",
)
def create_supplier(payload: SupplierCreate, db: Session = Depends(get_db)):
    """Create a new supplier. Checks for unique supplier code."""
    # Check duplicate code if code is supplied
    if payload.code:
        existing = db.execute(
            select(Supplier).where(Supplier.code == payload.code)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Supplier with code '{payload.code}' already exists",
            )

    supplier = Supplier(
        name=payload.name,
        code=payload.code,
        contact_email=payload.contact_email,
        contact_phone=payload.contact_phone,
        address=payload.address,
    )

    try:
        db.add(supplier)
        db.commit()
        db.refresh(supplier)
    except IntegrityError as e:
        db.rollback()
        if "code" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Supplier with code '{payload.code}' already exists",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database integrity constraint violation",
        )

    return supplier


@router.get(
    "",
    response_model=List[SupplierResponse],
    status_code=status.HTTP_200_OK,
    summary="List suppliers with pagination and search filter",
)
def list_suppliers(
    limit: int = Query(50, ge=1, le=100, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    search: Optional[str] = Query(None, description="Search by name or code"),
    db: Session = Depends(get_db),
):
    """List suppliers with pagination and optional search filter."""
    stmt = select(Supplier)

    if search:
        search_term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Supplier.name.ilike(search_term),
                Supplier.code.ilike(search_term),
            )
        )

    stmt = stmt.order_by(Supplier.name.asc()).offset(offset).limit(limit)
    results = db.execute(stmt).scalars().all()
    return results


@router.get(
    "/{supplier_id}",
    response_model=SupplierResponse,
    status_code=status.HTTP_200_OK,
    summary="Get supplier by ID",
)
def get_supplier(supplier_id: uuid.UUID, db: Session = Depends(get_db)):
    """Retrieve a single supplier by its UUID."""
    supplier = db.execute(
        select(Supplier).where(Supplier.id == supplier_id)
    ).scalar_one_or_none()

    if not supplier:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Supplier with ID '{supplier_id}' not found",
        )

    return supplier


@router.patch(
    "/{supplier_id}",
    response_model=SupplierResponse,
    status_code=status.HTTP_200_OK,
    summary="Update supplier details",
)
def update_supplier(
    supplier_id: uuid.UUID,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
):
    """Update supplier fields. Validates code uniqueness if changed."""
    supplier = db.execute(
        select(Supplier).where(Supplier.id == supplier_id)
    ).scalar_one_or_none()

    if not supplier:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Supplier with ID '{supplier_id}' not found",
        )

    # Check unique code collision if code is updated
    if payload.code is not None and payload.code != supplier.code:
        existing = db.execute(
            select(Supplier).where(Supplier.code == payload.code)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Supplier with code '{payload.code}' already exists",
            )
        supplier.code = payload.code

    if payload.name is not None:
        supplier.name = payload.name
    if payload.contact_email is not None:
        supplier.contact_email = payload.contact_email
    if payload.contact_phone is not None:
        supplier.contact_phone = payload.contact_phone
    if payload.address is not None:
        supplier.address = payload.address

    try:
        db.commit()
        db.refresh(supplier)
    except IntegrityError as e:
        db.rollback()
        if "code" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Supplier with code '{payload.code}' already exists",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database integrity constraint violation",
        )

    return supplier
