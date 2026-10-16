from decimal import Decimal
from typing import List, Optional, Dict, Any, Tuple
import uuid
import sys
import os

# Ensure packages are importable
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from packages.domain.models import (
    DocumentMetadata,
    DocumentType,
    ExceptionCase,
    ExceptionStatus,
    ExceptionType,
    DepartmentRole,
    EvidenceChecklist,
    ReconciliationFinding,
    LineItem,
)


class ToleranceConfig:
    def __init__(
        self,
        price_tolerance_pct: Decimal = Decimal("0.0"),
        price_tolerance_flat_pkr: Decimal = Decimal("0.0"),
        qty_tolerance_pct: Decimal = Decimal("0.0"),
    ):
        self.price_tolerance_pct = price_tolerance_pct
        self.price_tolerance_flat_pkr = price_tolerance_flat_pkr
        self.qty_tolerance_pct = qty_tolerance_pct


class ReconciliationEngine:
    """
    Deterministic Three-Way Reconciliation Rules Engine.
    Operates with exact Decimal arithmetic; zero hallucination.
    """

    def __init__(self, tolerance: Optional[ToleranceConfig] = None):
        self.tolerance = tolerance or ToleranceConfig()

    def reconcile_case(
        self,
        po: Optional[DocumentMetadata],
        grn: Optional[DocumentMetadata],
        invoice: Optional[DocumentMetadata],
        case_id: Optional[str] = None,
    ) -> Optional[ExceptionCase]:
        """
        Executes 3-way check on PO, GRN, and Invoice.
        Returns an ExceptionCase if discrepancies exist, or None if completely clean.
        """
        findings: List[ReconciliationFinding] = []
        po_present = po is not None
        grn_present = grn is not None
        inv_present = invoice is not None

        evidence_checklist = EvidenceChecklist(
            po_present=po_present,
            grn_present=grn_present,
            invoice_present=inv_present,
        )

        po_ref = po.po_reference if po else (grn.po_reference if grn else (invoice.po_reference if invoice else "UNKNOWN-PO"))
        supplier = po.supplier_name if po else (invoice.supplier_name if invoice else (grn.supplier_name if grn else "Unknown Supplier"))

        # Check 1: Missing Documents
        if inv_present and not grn_present:
            amount = invoice.total_amount
            findings.append(
                ReconciliationFinding(
                    finding_id=f"f-{uuid.uuid4().hex[:8]}",
                    type=ExceptionType.MISSING_GRN,
                    severity="CRITICAL",
                    expected_value=Decimal("1"),
                    actual_value=Decimal("0"),
                    delta_value=Decimal("1"),
                    amount_at_risk_pkr=amount,
                    source_evidence={"invoice_number": invoice.document_number},
                    explanation=f"Supplier Invoice {invoice.document_number} received for Rs. {amount:,.2f}, but no Goods Received Note (GRN) exists in Stores.",
                )
            )

        if inv_present and not po_present:
            amount = invoice.total_amount
            findings.append(
                ReconciliationFinding(
                    finding_id=f"f-{uuid.uuid4().hex[:8]}",
                    type=ExceptionType.MISSING_PO,
                    severity="CRITICAL",
                    expected_value=Decimal("1"),
                    actual_value=Decimal("0"),
                    delta_value=Decimal("1"),
                    amount_at_risk_pkr=amount,
                    source_evidence={"invoice_number": invoice.document_number},
                    explanation=f"Invoice {invoice.document_number} has no matching Purchase Order on record.",
                )
            )

        # Check 2: 3-Way Line Item Level Checks (when documents are present)
        if po and grn and invoice:
            line_findings = self._reconcile_line_items(po.line_items, grn.line_items, invoice.line_items)
            findings.extend(line_findings)

        # If no findings, transaction is 100% clean
        if not findings:
            return None

        # Calculate financial amount under review without double-counting shortage + overbilling on the same line item
        line_risk_map: Dict[str, Decimal] = {}
        doc_level_risk = Decimal("0.0")

        for f in findings:
            if f.type in (ExceptionType.MISSING_GRN, ExceptionType.MISSING_PO, ExceptionType.DUPLICATE_INVOICE):
                doc_level_risk += f.amount_at_risk_pkr
            elif f.line_item_desc:
                desc = f.line_item_desc
                if f.type == ExceptionType.QUANTITY_OVERBILL:
                    # Overbill is the actual payable exposure for unreceived goods
                    line_risk_map[desc] = f.amount_at_risk_pkr
                elif f.type == ExceptionType.PRICE_VARIANCE:
                    line_risk_map[desc] = line_risk_map.get(desc, Decimal("0.0")) + f.amount_at_risk_pkr
                elif f.type == ExceptionType.QUANTITY_SHORTAGE:
                    if desc not in line_risk_map:
                        line_risk_map[desc] = f.amount_at_risk_pkr

        total_at_risk = doc_level_risk + sum(line_risk_map.values())
        primary_finding = max(findings, key=lambda f: f.amount_at_risk_pkr)

        # Owner Assignment Logic:
        # - Shortage / Missing GRN -> Stores
        # - Price variance / Overbill / Duplicate -> Finance
        # - Missing PO -> Procurement
        if primary_finding.type in (ExceptionType.QUANTITY_SHORTAGE, ExceptionType.MISSING_GRN):
            owner = DepartmentRole.STORES
        elif primary_finding.type in (ExceptionType.PRICE_VARIANCE, ExceptionType.QUANTITY_OVERBILL, ExceptionType.DUPLICATE_INVOICE):
            owner = DepartmentRole.FINANCE
        elif primary_finding.type == ExceptionType.MISSING_PO:
            owner = DepartmentRole.PROCUREMENT
        else:
            owner = DepartmentRole.FINANCE

        case_id_val = case_id or f"EX-{uuid.uuid4().hex[:4].upper()}"

        return ExceptionCase(
            id=case_id_val,
            po_reference=po_ref or "PO-UNLINKED",
            supplier_name=supplier or "Unknown Vendor",
            type=primary_finding.type,
            status=ExceptionStatus.NEW,
            owner_department=owner,
            amount_under_review_pkr=total_at_risk,
            currency="PKR",
            evidence_checklist=evidence_checklist,
            findings=findings,
        )

    def _reconcile_line_items(
        self,
        po_lines: List[LineItem],
        grn_lines: List[LineItem],
        inv_lines: List[LineItem],
    ) -> List[ReconciliationFinding]:
        findings: List[ReconciliationFinding] = []

        # Index by item_code or fallback to normalized description
        grn_map: Dict[str, LineItem] = {self._item_key(l): l for l in grn_lines}
        inv_map: Dict[str, LineItem] = {self._item_key(l): l for l in inv_lines}

        for po_line in po_lines:
            key = self._item_key(po_line)
            grn_line = grn_map.get(key)
            inv_line = inv_map.get(key)

            ordered_qty = po_line.quantity
            received_qty = grn_line.quantity if grn_line else Decimal("0")
            invoiced_qty = inv_line.quantity if inv_line else Decimal("0")
            po_price = po_line.unit_price
            inv_price = inv_line.unit_price if inv_line else po_price

            # Rule A: Short Delivery (Stores received less than PO ordered)
            if received_qty < ordered_qty:
                diff_units = ordered_qty - received_qty
                findings.append(
                    ReconciliationFinding(
                        finding_id=f"f-{uuid.uuid4().hex[:8]}",
                        type=ExceptionType.QUANTITY_SHORTAGE,
                        severity="WARNING",
                        line_item_desc=po_line.description,
                        expected_value=ordered_qty,
                        actual_value=received_qty,
                        delta_value=diff_units,
                        amount_at_risk_pkr=diff_units * po_price,
                        source_evidence={"po_qty": str(ordered_qty), "grn_qty": str(received_qty)},
                        explanation=f"Short delivery on '{po_line.description}': Ordered {ordered_qty} {po_line.unit}, but Stores recorded {received_qty} {po_line.unit}.",
                    )
                )

            # Rule B: Overbilling (Invoiced quantity exceeds verified Stores receipt)
            if invoiced_qty > received_qty:
                excess_units = invoiced_qty - received_qty
                at_risk = excess_units * inv_price
                findings.append(
                    ReconciliationFinding(
                        finding_id=f"f-{uuid.uuid4().hex[:8]}",
                        type=ExceptionType.QUANTITY_OVERBILL,
                        severity="CRITICAL",
                        line_item_desc=po_line.description,
                        expected_value=received_qty,
                        actual_value=invoiced_qty,
                        delta_value=excess_units,
                        amount_at_risk_pkr=at_risk,
                        source_evidence={"grn_qty": str(received_qty), "invoiced_qty": str(invoiced_qty)},
                        explanation=f"Overbill on '{po_line.description}': Invoiced for {invoiced_qty} {po_line.unit}, but Stores only verified receipt of {received_qty} {po_line.unit}. Potential overpayment: Rs. {at_risk:,.2f}.",
                    )
                )

            # Rule C: Unit Price Variance
            if inv_line and inv_price > po_price:
                diff_price = inv_price - po_price
                pct_diff = (diff_price / po_price) * Decimal("100")
                if pct_diff > self.tolerance.price_tolerance_pct and diff_price > self.tolerance.price_tolerance_flat_pkr:
                    at_risk = diff_price * invoiced_qty
                    findings.append(
                        ReconciliationFinding(
                            finding_id=f"f-{uuid.uuid4().hex[:8]}",
                            type=ExceptionType.PRICE_VARIANCE,
                            severity="CRITICAL",
                            line_item_desc=po_line.description,
                            expected_value=po_price,
                            actual_value=inv_price,
                            delta_value=diff_price,
                            amount_at_risk_pkr=at_risk,
                            source_evidence={"po_price": str(po_price), "invoice_price": str(inv_price)},
                            explanation=f"Price variance on '{po_line.description}': Invoiced at Rs. {inv_price:,.2f} vs PO rate Rs. {po_price:,.2f} (+{pct_diff:.1f}% variance). Risk: Rs. {at_risk:,.2f}.",
                        )
                    )

        return findings

    @staticmethod
    def _item_key(item: LineItem) -> str:
        if item.item_code and item.item_code.strip():
            return item.item_code.strip().upper()
        return item.description.strip().lower()
