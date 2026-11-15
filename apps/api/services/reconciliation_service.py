"""
Deterministic Reconciliation Engine & Service for Recorda.

Implements 2-way, 3-way, and 4-way matching between Purchase Orders,
Delivery Challans, Goods Receipt Notes, and Supplier Invoices.
Operates exclusively on APPROVED human verification data and CONFIRMED
document relationships using exact Decimal arithmetic (zero float policy).
"""

from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import uuid
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import (
    Document,
    DocumentVerification,
    ReconciliationRun,
    ReconciliationDiscrepancy,
)
from apps.api.services.matching_service import DocumentMatchingService


class ReconciliationError(Exception):
    """Base exception for reconciliation operations."""
    pass


class DocumentNotFoundError(ReconciliationError):
    """Raised when requested root document is not found."""
    pass


class UnapprovedDocumentError(ReconciliationError):
    """Raised when an unapproved document is provided for reconciliation."""
    pass


class MissingPOReferenceError(ReconciliationError):
    """Raised when no approved purchase order is found in the transaction chain."""
    pass


class ToleranceConfig:
    """Configurable tolerance thresholds for monetary and quantity variances."""

    def __init__(
        self,
        price_tolerance_pct: Decimal = Decimal("0.0"),
        price_tolerance_flat_usd: Decimal = Decimal("0.0"),
        qty_tolerance_pct: Decimal = Decimal("0.0"),
        allow_missing_dc: bool = False,
    ):
        self.price_tolerance_pct = Decimal(str(price_tolerance_pct))
        self.price_tolerance_flat_usd = Decimal(str(price_tolerance_flat_usd))
        self.qty_tolerance_pct = Decimal(str(qty_tolerance_pct))
        self.allow_missing_dc = allow_missing_dc


class ReconciliationService:
    """Deterministic 3-way/4-way reconciliation service."""

    def __init__(self, db: Session):
        self.db = db
        self.matching_service = DocumentMatchingService(db)

    def reconcile_transaction(
        self,
        document_id: uuid.UUID,
        tolerance: Optional[ToleranceConfig] = None,
        actor: str = "SYSTEM",
    ) -> ReconciliationRun:
        """
        Execute deterministic reconciliation across the transaction chain of document_id.
        Persists and returns a ReconciliationRun with detailed findings.
        """
        tol = tolerance or ToleranceConfig()

        root_doc = self.db.get(Document, document_id)
        if not root_doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        # 1. Resolve transaction chain via matching service (only CONFIRMED relationships)
        chain = self.matching_service.get_transaction_chain(document_id)

        all_doc_ids: List[uuid.UUID] = []
        for cat in ("purchase_orders", "delivery_challans", "goods_receipt_notes", "supplier_invoices"):
            for item in chain.get(cat, []):
                all_doc_ids.append(item["id"])

        if document_id not in all_doc_ids:
            all_doc_ids.append(document_id)

        # 2. Verify all connected documents have APPROVED human verification
        docs_map: Dict[uuid.UUID, Document] = {}
        verifications_map: Dict[uuid.UUID, DocumentVerification] = {}

        for doc_uuid in all_doc_ids:
            doc = self.db.get(Document, doc_uuid)
            if not doc:
                continue
            docs_map[doc_uuid] = doc

            approved_ver = self.db.execute(
                select(DocumentVerification)
                .where(
                    DocumentVerification.document_id == doc_uuid,
                    DocumentVerification.status == "APPROVED",
                )
                .order_by(DocumentVerification.version.desc())
            ).scalars().first()

            if not approved_ver:
                raise UnapprovedDocumentError(
                    f"Document '{doc.id}' ({doc.document_type}) does not have an APPROVED verification record. "
                    "Reconciliation strictly requires APPROVED human-verified data."
                )
            verifications_map[doc_uuid] = approved_ver

        # 3. Categorize verified documents
        po_docs: List[Tuple[Document, DocumentVerification]] = []
        dc_docs: List[Tuple[Document, DocumentVerification]] = []
        grn_docs: List[Tuple[Document, DocumentVerification]] = []
        inv_docs: List[Tuple[Document, DocumentVerification]] = []

        for doc_uuid, doc in docs_map.items():
            dtype = doc.document_type.upper()
            ver = verifications_map[doc_uuid]
            if dtype == "PURCHASE_ORDER":
                po_docs.append((doc, ver))
            elif dtype == "DELIVERY_CHALLAN":
                dc_docs.append((doc, ver))
            elif dtype == "GOODS_RECEIPT_NOTE":
                grn_docs.append((doc, ver))
            elif dtype == "SUPPLIER_INVOICE":
                inv_docs.append((doc, ver))

        # 4. Require an approved PO as primary reference
        discrepancies: List[ReconciliationDiscrepancy] = []

        if not po_docs:
            # Missing PO in transaction
            finding = ReconciliationDiscrepancy(
                discrepancy_type="MISSING_PO",
                severity="CRITICAL",
                currency="USD",
                amount_at_risk=Decimal("0.0000"),
                explanation="No approved Purchase Order found in confirmed transaction chain.",
                source_evidence={"connected_documents": [str(d) for d in all_doc_ids]},
            )
            discrepancies.append(finding)

            run = ReconciliationRun(
                root_document_id=document_id,
                po_document_id=None,
                status="INCOMPLETE",
                reconciled_document_ids=[str(d) for d in all_doc_ids],
                summary={
                    "item_count": 0,
                    "discrepancy_count": 1,
                    "critical_count": 1,
                    "warning_count": 0,
                    "info_count": 0,
                    "total_po_amount": "0.00",
                    "total_billed_amount": "0.00",
                    "total_amount_at_risk": "0.00",
                    "status_breakdown": {"MISSING_PO": 1},
                },
                created_by=actor,
            )
            run.discrepancies = discrepancies
            self.db.add(run)
            self.db.commit()
            self.db.refresh(run)
            return run

        primary_po_doc, primary_po_ver = po_docs[0]
        po_fields = primary_po_ver.verified_fields or {}
        po_line_items = primary_po_ver.verified_line_items or []

        # 5. Currency Check (USD only)
        non_usd_detected = False
        all_ver_tuples = po_docs + dc_docs + grn_docs + inv_docs
        for doc, ver in all_ver_tuples:
            fields = ver.verified_fields or {}
            curr = str(fields.get("currency") or "USD").strip().upper()
            if curr != "USD":
                non_usd_detected = True
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type="NON_USD_CURRENCY",
                        severity="CRITICAL",
                        currency=curr,
                        amount_at_risk=Decimal("0.0000"),
                        explanation=(
                            f"Document '{doc.document_number or doc.id}' has non-USD currency '{curr}'. "
                            "Recorda operates exclusively in USD."
                        ),
                        source_evidence={"document_id": str(doc.id), "currency": curr},
                    )
                )

        # 6. Supplier Consistency Check
        po_supplier = self._normalize_text(po_fields.get("supplier_name"))
        for doc, ver in (inv_docs + dc_docs + grn_docs):
            fields = ver.verified_fields or {}
            supp = self._normalize_text(fields.get("supplier_name"))
            if po_supplier and supp and po_supplier != supp:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type="SUPPLIER_MISMATCH",
                        severity="CRITICAL",
                        currency="USD",
                        amount_at_risk=self._to_decimal(fields.get("grand_total") or fields.get("total_amount") or 0),
                        explanation=(
                            f"Supplier mismatch: Purchase Order specifies '{po_fields.get('supplier_name')}', "
                            f"but {doc.document_type} '{doc.document_number}' specifies '{fields.get('supplier_name')}'."
                        ),
                        source_evidence={
                            "po_supplier": po_fields.get("supplier_name"),
                            "doc_supplier": fields.get("supplier_name"),
                            "document_id": str(doc.id),
                        },
                    )
                )

        # 7. Check Missing Downstream Documents
        has_invoices = len(inv_docs) > 0
        has_grn = len(grn_docs) > 0
        has_dc = len(dc_docs) > 0

        if has_invoices and not has_grn:
            total_inv_amt = sum(
                self._to_decimal((v.verified_fields or {}).get("grand_total") or (v.verified_fields or {}).get("total_amount") or 0)
                for _, v in inv_docs
            )
            discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type="MISSING_GRN",
                    severity="CRITICAL",
                    currency="USD",
                    amount_at_risk=total_inv_amt,
                    explanation="Supplier Invoice received and verified, but no verified Goods Receipt Note (GRN) exists on record.",
                    source_evidence={"invoice_ids": [str(d.id) for d, _ in inv_docs]},
                )
            )

        if (has_invoices or has_grn) and not has_dc and not tol.allow_missing_dc:
            discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type="MISSING_DC",
                    severity="WARNING",
                    currency="USD",
                    amount_at_risk=Decimal("0.0000"),
                    explanation="Downstream receipt/billing documents exist without a confirmed Delivery Challan.",
                    source_evidence={"root_document_id": str(document_id)},
                )
            )

        # 8. Index Line Items
        po_items_map: Dict[str, Dict[str, Any]] = {}
        po_key_counts: Dict[str, int] = {}
        po_ambiguous_keys: Set[str] = set()

        for item in po_line_items:
            key = self._extract_item_key(item)
            if not key:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type="UNMATCHED_LINE_ITEM",
                        severity="CRITICAL",
                        item_code=None,
                        line_item_description="(Missing Item Description)",
                        currency="USD",
                        amount_at_risk=self._to_decimal(item.get("line_total") or (self._to_decimal(item.get("quantity") or 0) * self._to_decimal(item.get("unit_price") or 0))),
                        explanation="Purchase Order contains a line item with neither item_code nor description; ambiguous item cannot be matched.",
                        source_evidence={"line_item": item},
                    )
                )
                continue

            po_key_counts[key] = po_key_counts.get(key, 0) + 1
            if po_key_counts[key] > 1:
                po_ambiguous_keys.add(key)
            else:
                po_items_map[key] = item

        for amb_key in po_ambiguous_keys:
            po_items_map.pop(amb_key, None)
            discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type="AMBIGUOUS_LINE_ITEM",
                    severity="CRITICAL",
                    item_code=amb_key,
                    line_item_description=amb_key,
                    currency="USD",
                    amount_at_risk=Decimal("0.0000"),
                    explanation=(
                        f"Ambiguous line items on Purchase Order: multiple line items ({po_key_counts[amb_key]}) "
                        f"share the matching key '{amb_key}'. Disambiguation is required to prevent silent mismatch."
                    ),
                    source_evidence={"matching_key": amb_key, "count": po_key_counts[amb_key]},
                )
            )

        dc_items_map: Dict[str, List[Dict[str, Any]]] = {}
        for _, dc_ver in dc_docs:
            for item in (dc_ver.verified_line_items or []):
                key = self._extract_item_key(item)
                if key:
                    dc_items_map.setdefault(key, []).append(item)
                else:
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="UNMATCHED_LINE_ITEM",
                            severity="WARNING",
                            item_code=None,
                            line_item_description="(Missing Item Description)",
                            currency="USD",
                            amount_at_risk=Decimal("0.0000"),
                            explanation="Delivery Challan contains an unidentifiable line item with no item code or description.",
                            source_evidence={"line_item": item},
                        )
                    )

        grn_items_map: Dict[str, List[Dict[str, Any]]] = {}
        for _, grn_ver in grn_docs:
            for item in (grn_ver.verified_line_items or []):
                key = self._extract_item_key(item)
                if key:
                    grn_items_map.setdefault(key, []).append(item)
                else:
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="UNMATCHED_LINE_ITEM",
                            severity="WARNING",
                            item_code=None,
                            line_item_description="(Missing Item Description)",
                            currency="USD",
                            amount_at_risk=Decimal("0.0000"),
                            explanation="Goods Receipt Note contains an unidentifiable line item with no item code or description.",
                            source_evidence={"line_item": item},
                        )
                    )

        inv_items_map: Dict[str, List[Dict[str, Any]]] = {}
        for _, inv_ver in inv_docs:
            for item in (inv_ver.verified_line_items or []):
                key = self._extract_item_key(item)
                if key:
                    inv_items_map.setdefault(key, []).append(item)
                else:
                    qty = self._to_decimal(item.get("quantity") or 0)
                    price = self._to_decimal(item.get("unit_price") or 0)
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="UNMATCHED_LINE_ITEM",
                            severity="CRITICAL",
                            item_code=None,
                            line_item_description="(Missing Item Description)",
                            currency="USD",
                            amount_at_risk=qty * price,
                            explanation="Supplier Invoice contains an unidentifiable line item with no item code or description.",
                            source_evidence={"line_item": item},
                        )
                    )

        # 9. Detect Unmatched Line Items on Downstream Documents
        for key, items in inv_items_map.items():
            if key not in po_items_map and key not in po_ambiguous_keys:
                for itm in items:
                    item_desc = itm.get("item_description") or itm.get("description") or key
                    qty = self._to_decimal(itm.get("quantity") or 0)
                    price = self._to_decimal(itm.get("unit_price") or 0)
                    line_risk = qty * price
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="UNMATCHED_LINE_ITEM",
                            severity="CRITICAL",
                            item_code=itm.get("item_code"),
                            line_item_description=item_desc,
                            currency="USD",
                            amount_at_risk=line_risk,
                            explanation=f"Supplier Invoice contains line item '{item_desc}' which is not in the Purchase Order.",
                            source_evidence={"line_item": itm},
                        )
                    )

        for key, items in grn_items_map.items():
            if key not in po_items_map and key not in po_ambiguous_keys:
                for itm in items:
                    item_desc = itm.get("item_description") or itm.get("description") or key
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="UNMATCHED_LINE_ITEM",
                            severity="WARNING",
                            item_code=itm.get("item_code"),
                            line_item_description=item_desc,
                            currency="USD",
                            amount_at_risk=Decimal("0.0000"),
                            explanation=f"Goods Receipt Note contains unreferenced item '{item_desc}' not present on Purchase Order.",
                            source_evidence={"line_item": itm},
                        )
                    )

        # 10. Reconcile Matched Line Items (Quantities & Prices)
        for key, po_item in po_items_map.items():
            po_desc = po_item.get("item_description") or po_item.get("description") or key
            po_code = po_item.get("item_code")
            ordered_qty = self._to_decimal(po_item.get("quantity") or 0)
            po_unit_price = self._to_decimal(po_item.get("unit_price") or 0)

            # Sum DC dispatched quantities
            dispatched_qty = Decimal("0.0000")
            if has_dc and key in dc_items_map:
                for d in dc_items_map[key]:
                    dispatched_qty += self._to_decimal(d.get("dispatched_quantity") or d.get("quantity") or 0)

            # Sum GRN received, accepted, rejected quantities
            received_qty = Decimal("0.0000")
            accepted_qty = Decimal("0.0000")
            rejected_qty = Decimal("0.0000")
            if has_grn and key in grn_items_map:
                for g in grn_items_map[key]:
                    g_rec = self._to_decimal(g.get("received_quantity") or g.get("quantity") or 0)
                    g_rej = self._to_decimal(g.get("rejected_quantity") or 0)
                    if g.get("accepted_quantity") is not None:
                        g_acc = self._to_decimal(g.get("accepted_quantity"))
                    else:
                        g_acc = max(Decimal("0.0000"), g_rec - g_rej)
                    received_qty += g_rec
                    accepted_qty += g_acc
                    rejected_qty += g_rej

            # Sum Invoice quantities and rates
            billed_qty = Decimal("0.0000")
            priced_qty = Decimal("0.0000")
            invoiced_value = Decimal("0.0000")
            if has_invoices and key in inv_items_map:
                for i in inv_items_map[key]:
                    line_qty = self._to_decimal(i.get("quantity") or i.get("billed_quantity") or 0)
                    billed_qty += line_qty
                    if i.get("unit_price") is not None:
                        line_price = self._to_decimal(i.get("unit_price"))
                        priced_qty += line_qty
                        invoiced_value += line_qty * line_price
            inv_unit_price = (
                invoiced_value / priced_qty if priced_qty > 0 else po_unit_price
            )

            # --- A. Quantity: Under-Delivery / Shortage ---
            if has_grn:
                if accepted_qty < ordered_qty:
                    delta_under = ordered_qty - accepted_qty
                    under_pct = (delta_under / ordered_qty * Decimal("100")) if ordered_qty > 0 else Decimal("100")
                    if under_pct > tol.qty_tolerance_pct:
                        at_risk = delta_under * po_unit_price
                        discrepancies.append(
                            ReconciliationDiscrepancy(
                                discrepancy_type="UNDER_DELIVERED",
                                severity="WARNING",
                                item_code=po_code,
                                line_item_description=po_desc,
                                expected_value=ordered_qty,
                                actual_value=accepted_qty,
                                delta_value=delta_under,
                                currency="USD",
                                amount_at_risk=at_risk,
                                explanation=(
                                    f"Short delivery on '{po_desc}': PO ordered {ordered_qty}, "
                                    f"but Stores accepted only {accepted_qty} (shortage: {delta_under})."
                                ),
                                source_evidence={"ordered_qty": str(ordered_qty), "accepted_qty": str(accepted_qty)},
                            )
                        )
                elif accepted_qty > ordered_qty:
                    delta_over = accepted_qty - ordered_qty
                    over_pct = (delta_over / ordered_qty * Decimal("100")) if ordered_qty > 0 else Decimal("100")
                    if over_pct > tol.qty_tolerance_pct:
                        discrepancies.append(
                            ReconciliationDiscrepancy(
                                discrepancy_type="OVER_DELIVERED",
                                severity="WARNING",
                                item_code=po_code,
                                line_item_description=po_desc,
                                expected_value=ordered_qty,
                                actual_value=accepted_qty,
                                delta_value=delta_over,
                                currency="USD",
                                amount_at_risk=Decimal("0.0000"),
                                explanation=(
                                    f"Over-delivery on '{po_desc}': Accepted {accepted_qty} units "
                                    f"against ordered {ordered_qty} units (+{delta_over} excess)."
                                ),
                                source_evidence={"ordered_qty": str(ordered_qty), "accepted_qty": str(accepted_qty)},
                            )
                        )
            elif has_dc and not has_grn:
                # DC partial delivery check
                if dispatched_qty < ordered_qty:
                    delta_partial = ordered_qty - dispatched_qty
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="PARTIAL_DELIVERY",
                            severity="INFO",
                            item_code=po_code,
                            line_item_description=po_desc,
                            expected_value=ordered_qty,
                            actual_value=dispatched_qty,
                            delta_value=delta_partial,
                            currency="USD",
                            amount_at_risk=delta_partial * po_unit_price,
                            explanation=(
                                f"Partial dispatch on '{po_desc}': Delivery Challan records {dispatched_qty} "
                                f"dispatched units against ordered {ordered_qty}."
                            ),
                            source_evidence={"ordered_qty": str(ordered_qty), "dispatched_qty": str(dispatched_qty)},
                        )
                    )

            # --- B. Quantity: Overbilling (Invoice billed > GRN accepted) ---
            if has_invoices and has_grn:
                if billed_qty > accepted_qty:
                    excess_units = billed_qty - accepted_qty
                    at_risk = excess_units * inv_unit_price
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="OVER_BILLED_QUANTITY",
                            severity="CRITICAL",
                            item_code=po_code,
                            line_item_description=po_desc,
                            expected_value=accepted_qty,
                            actual_value=billed_qty,
                            delta_value=excess_units,
                            currency="USD",
                            amount_at_risk=at_risk,
                            explanation=(
                                f"Overbilling on '{po_desc}': Invoiced for {billed_qty} units, "
                                f"but Stores accepted only {accepted_qty} units. Excess billed: {excess_units}."
                            ),
                            source_evidence={"accepted_qty": str(accepted_qty), "billed_qty": str(billed_qty)},
                        )
                    )

                # --- C. Rejected Goods Billed ---
                if rejected_qty > 0 and billed_qty > accepted_qty:
                    # Overlap between rejected quantity and the excess units billed
                    rejected_billed = min(rejected_qty, excess_units)
                    if rejected_billed > 0:
                        discrepancies.append(
                            ReconciliationDiscrepancy(
                                discrepancy_type="REJECTED_GOODS_BILLED",
                                severity="CRITICAL",
                                item_code=po_code,
                                line_item_description=po_desc,
                                expected_value=Decimal("0.0000"),
                                actual_value=rejected_billed,
                                delta_value=rejected_billed,
                                currency="USD",
                                amount_at_risk=rejected_billed * inv_unit_price,
                                explanation=(
                                    f"Rejected goods billed on '{po_desc}': Stores rejected {rejected_qty} units, "
                                    f"and invoice billed for {rejected_billed} unaccepted/rejected units."
                                ),
                                source_evidence={
                                    "rejected_qty": str(rejected_qty),
                                    "billed_qty": str(billed_qty),
                                    "accepted_qty": str(accepted_qty),
                                },
                            )
                        )

            # --- D. Financial: Unit Price Variance ---
            if has_invoices and key in inv_items_map:
                if inv_unit_price > po_unit_price:
                    diff_rate = inv_unit_price - po_unit_price
                    pct_diff = (diff_rate / po_unit_price * Decimal("100")) if po_unit_price > 0 else Decimal("100")
                    if pct_diff > tol.price_tolerance_pct and diff_rate > tol.price_tolerance_flat_usd:
                        effective_qty = priced_qty if priced_qty > 0 else (billed_qty if billed_qty > 0 else ordered_qty)
                        at_risk = diff_rate * effective_qty
                        discrepancies.append(
                            ReconciliationDiscrepancy(
                                discrepancy_type="PRICE_VARIANCE",
                                severity="CRITICAL",
                                item_code=po_code,
                                line_item_description=po_desc,
                                expected_value=po_unit_price,
                                actual_value=inv_unit_price,
                                delta_value=diff_rate,
                                currency="USD",
                                amount_at_risk=at_risk,
                                explanation=(
                                    f"Price variance on '{po_desc}': Invoiced at ${inv_unit_price:,.2f} "
                                    f"vs PO rate ${po_unit_price:,.2f} (+{pct_diff:.2f}% variance)."
                                ),
                                source_evidence={"po_price": str(po_unit_price), "invoice_price": str(inv_unit_price)},
                            )
                        )

        # 11. Header Financial Checks
        po_total = self._to_decimal(po_fields.get("grand_total") or po_fields.get("total_amount") or 0)
        po_subtotal = self._to_decimal(po_fields.get("subtotal") or 0)
        po_tax = self._to_decimal(po_fields.get("tax_amount") or 0)

        # Validate PO internal math
        calc_po_subtotal = sum(
            self._to_decimal(it.get("quantity") or 0) * self._to_decimal(it.get("unit_price") or 0)
            for it in po_line_items
        )
        if po_subtotal > 0 and abs(calc_po_subtotal - po_subtotal) > Decimal("0.05"):
            discrepancies.append(
                ReconciliationDiscrepancy(
                    discrepancy_type="CALCULATION_DISCREPANCY",
                    severity="WARNING",
                    currency="USD",
                    expected_value=calc_po_subtotal,
                    actual_value=po_subtotal,
                    delta_value=abs(calc_po_subtotal - po_subtotal),
                    amount_at_risk=abs(calc_po_subtotal - po_subtotal),
                    explanation=f"PO line items sum (${calc_po_subtotal:,.2f}) does not match stated subtotal (${po_subtotal:,.2f}).",
                    source_evidence={"line_sum": str(calc_po_subtotal), "stated_subtotal": str(po_subtotal)},
                )
            )

        if po_total > 0 and po_subtotal > 0 and po_tax > 0:
            if abs((po_subtotal + po_tax) - po_total) > Decimal("0.05"):
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type="TAX_DISCREPANCY",
                        severity="WARNING",
                        currency="USD",
                        expected_value=po_subtotal + po_tax,
                        actual_value=po_total,
                        delta_value=abs((po_subtotal + po_tax) - po_total),
                        amount_at_risk=Decimal("0.0000"),
                        explanation=f"PO subtotal (${po_subtotal:,.2f}) + tax (${po_tax:,.2f}) != grand total (${po_total:,.2f}).",
                    )
                )

        # Validate Invoices Header Financials
        total_billed_grand = Decimal("0.0000")
        for inv_doc, inv_ver in inv_docs:
            inv_fields = inv_ver.verified_fields or {}
            inv_lines = inv_ver.verified_line_items or []
            inv_grand = self._to_decimal(inv_fields.get("grand_total") or inv_fields.get("total_amount") or 0)
            inv_sub = self._to_decimal(inv_fields.get("subtotal") or 0)
            inv_tax = self._to_decimal(inv_fields.get("tax_amount") or 0)
            total_billed_grand += inv_grand

            calc_inv_sub = sum(
                self._to_decimal(it.get("quantity") or 0) * self._to_decimal(it.get("unit_price") or 0)
                for it in inv_lines
            )
            if inv_sub > 0 and abs(calc_inv_sub - inv_sub) > Decimal("0.05"):
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        discrepancy_type="CALCULATION_DISCREPANCY",
                        severity="CRITICAL",
                        currency="USD",
                        expected_value=calc_inv_sub,
                        actual_value=inv_sub,
                        delta_value=abs(calc_inv_sub - inv_sub),
                        amount_at_risk=abs(calc_inv_sub - inv_sub),
                        explanation=(
                            f"Invoice '{inv_doc.document_number}' line items sum (${calc_inv_sub:,.2f}) "
                            f"differs from stated subtotal (${inv_sub:,.2f})."
                        ),
                    )
                )

            if inv_grand > 0 and inv_sub > 0 and inv_tax > 0:
                if abs((inv_sub + inv_tax) - inv_grand) > Decimal("0.05"):
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="TAX_DISCREPANCY",
                            severity="WARNING",
                            currency="USD",
                            expected_value=inv_sub + inv_tax,
                            actual_value=inv_grand,
                            delta_value=abs((inv_sub + inv_tax) - inv_grand),
                            amount_at_risk=Decimal("0.0000"),
                            explanation=(
                                f"Invoice '{inv_doc.document_number}' subtotal (${inv_sub:,.2f}) "
                                f"+ tax (${inv_tax:,.2f}) != grand total (${inv_grand:,.2f})."
                            ),
                        )
                    )

        # Header Total Mismatch: Billed Grand Total > PO Total
        if has_invoices and total_billed_grand > po_total and po_total > 0:
            diff_tot = total_billed_grand - po_total
            if diff_tot > tol.price_tolerance_flat_usd:
                # Only add if not already captured line-by-line to avoid duplicate reporting
                has_line_critical = any(
                    d.discrepancy_type in ("OVER_BILLED_QUANTITY", "PRICE_VARIANCE")
                    for d in discrepancies
                )
                if not has_line_critical:
                    discrepancies.append(
                        ReconciliationDiscrepancy(
                            discrepancy_type="TOTAL_MISMATCH",
                            severity="CRITICAL",
                            currency="USD",
                            expected_value=po_total,
                            actual_value=total_billed_grand,
                            delta_value=diff_tot,
                            amount_at_risk=diff_tot,
                            explanation=(
                                f"Total billed amount (${total_billed_grand:,.2f}) exceeds "
                                f"authorized Purchase Order total (${po_total:,.2f}) by ${diff_tot:,.2f}."
                            ),
                            source_evidence={"po_total": str(po_total), "billed_total": str(total_billed_grand)},
                        )
                    )

        # 12. Determine Final Run Status
        critical_count = sum(1 for d in discrepancies if d.severity == "CRITICAL")
        warning_count = sum(1 for d in discrepancies if d.severity == "WARNING")
        info_count = sum(1 for d in discrepancies if d.severity == "INFO")

        if critical_count > 0:
            status = "FAIL"
        elif warning_count > 0:
            status = "WARNING"
        elif not po_docs:
            status = "INCOMPLETE"
        else:
            status = "PASS"

        # Unique line items count
        all_unique_items = set(po_items_map.keys()) | set(inv_items_map.keys()) | set(grn_items_map.keys())

        # Total amount at risk (deduplicated or sum of financial exposure)
        total_at_risk = sum((d.amount_at_risk for d in discrepancies), Decimal("0.0000"))

        status_breakdown: Dict[str, int] = {}
        for d in discrepancies:
            status_breakdown[d.discrepancy_type] = status_breakdown.get(d.discrepancy_type, 0) + 1

        summary = {
            "item_count": len(all_unique_items),
            "discrepancy_count": len(discrepancies),
            "critical_count": critical_count,
            "warning_count": warning_count,
            "info_count": info_count,
            "total_po_amount": str(po_total),
            "total_billed_amount": str(total_billed_grand),
            "total_amount_at_risk": str(total_at_risk),
            "status_breakdown": status_breakdown,
        }

        run = ReconciliationRun(
            root_document_id=document_id,
            po_document_id=primary_po_doc.id if primary_po_doc else None,
            status=status,
            reconciled_document_ids=[str(d) for d in all_doc_ids],
            summary=summary,
            created_by=actor,
        )
        run.discrepancies = discrepancies
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        return run

    def get_reconciliations_for_document(self, document_id: uuid.UUID) -> List[ReconciliationRun]:
        """Fetch all reconciliation runs for a given root document."""
        return list(
            self.db.execute(
                select(ReconciliationRun)
                .where(ReconciliationRun.root_document_id == document_id)
                .order_by(ReconciliationRun.created_at.desc())
            ).scalars().all()
        )

    def get_latest_reconciliation(self, document_id: uuid.UUID) -> Optional[ReconciliationRun]:
        """Fetch the most recent reconciliation run for a given root document."""
        return self.db.execute(
            select(ReconciliationRun)
            .where(ReconciliationRun.root_document_id == document_id)
            .order_by(ReconciliationRun.created_at.desc())
        ).scalars().first()

    @staticmethod
    def _extract_item_key(item: Dict[str, Any]) -> Optional[str]:
        """Extract a stable matching key using item_code, falling back to normalized description. Returns None if both are blank."""
        code = item.get("item_code") or item.get("code") or item.get("sku") or item.get("product_code")
        if code and str(code).strip():
            return str(code).strip().upper()
        desc = item.get("item_description") or item.get("description") or ""
        norm = re.sub(r"\s+", " ", str(desc).strip().lower())
        return norm if norm else None

    @staticmethod
    def _normalize_text(val: Optional[str]) -> str:
        if not val:
            return ""
        return re.sub(r"[\s\-_]+", "", str(val).strip().lower())

    @staticmethod
    def _to_decimal(val: Any) -> Decimal:
        """Safely convert any numeric, string or float value into Decimal."""
        if val is None:
            return Decimal("0.0000")
        if isinstance(val, Decimal):
            return val
        s = str(val).strip().replace(",", "")
        try:
            return Decimal(s)
        except InvalidOperation:
            return Decimal("0.0000")
