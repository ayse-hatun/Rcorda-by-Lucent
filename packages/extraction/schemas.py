"""Document-specific extraction schemas for Recorda.

Defines the structured representations for:
- PURCHASE_ORDER
- DELIVERY_CHALLAN
- GOODS_RECEIPT_NOTE
- SUPPLIER_INVOICE
"""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class POLineItemSchema(BaseModel):
    """Line item schema for Purchase Orders."""
    item_description: str = Field(..., min_length=1)
    item_code: Optional[str] = None
    quantity: Decimal = Field(..., gt=0)
    unit_of_measure: str = Field(default="pcs")
    unit_price: Decimal = Field(..., ge=0)
    line_total: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class PurchaseOrderExtractionSchema(BaseModel):
    """Structured extraction schema for Purchase Orders."""
    po_number: Optional[str] = None
    supplier_name: Optional[str] = None
    po_date: Optional[date] = None
    currency: str = Field(default="USD")
    subtotal: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    grand_total: Optional[Decimal] = None
    line_items: List[POLineItemSchema] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class DCLineItemSchema(BaseModel):
    """Line item schema for Delivery Challans."""
    item_description: str = Field(..., min_length=1)
    item_code: Optional[str] = None
    dispatched_quantity: Decimal = Field(..., gt=0)
    unit_of_measure: str = Field(default="pcs")

    model_config = ConfigDict(from_attributes=True)


class DeliveryChallanExtractionSchema(BaseModel):
    """Structured extraction schema for Delivery Challans."""
    dc_number: Optional[str] = None
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    dispatch_date: Optional[date] = None
    currency: Optional[str] = None
    line_items: List[DCLineItemSchema] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class GRNLineItemSchema(BaseModel):
    """Line item schema for Goods Receipt Notes."""
    item_description: str = Field(..., min_length=1)
    item_code: Optional[str] = None
    received_quantity: Decimal = Field(..., ge=0)
    accepted_quantity: Optional[Decimal] = None
    rejected_quantity: Optional[Decimal] = None
    unit_of_measure: str = Field(default="pcs")

    model_config = ConfigDict(from_attributes=True)


class GoodsReceiptNoteExtractionSchema(BaseModel):
    """Structured extraction schema for Goods Receipt Notes."""
    grn_number: Optional[str] = None
    po_reference: Optional[str] = None
    dc_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    receipt_date: Optional[date] = None
    line_items: List[GRNLineItemSchema] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class InvoiceLineItemSchema(BaseModel):
    """Line item schema for Supplier Invoices."""
    item_description: str = Field(..., min_length=1)
    item_code: Optional[str] = None
    quantity: Decimal = Field(..., gt=0)
    unit_of_measure: str = Field(default="pcs")
    unit_price: Decimal = Field(..., ge=0)
    line_total: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class SupplierInvoiceExtractionSchema(BaseModel):
    """Structured extraction schema for Supplier Invoices."""
    invoice_number: Optional[str] = None
    po_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    currency: str = Field(default="USD")
    subtotal: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    grand_total: Optional[Decimal] = None
    line_items: List[InvoiceLineItemSchema] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
