"""Supplier Invoice structured data extractor."""

from decimal import Decimal
import re
from typing import Any, Dict, List, Optional

from packages.extraction.base import (
    BaseExtractor,
    ExtractionOutput,
    ExtractionWarning,
    FieldProvenance,
)
from packages.extraction.normalizers import (
    normalize_currency_code,
    parse_date_value,
    parse_decimal_value,
)


class SupplierInvoiceExtractor(BaseExtractor):
    """Deterministic, rule-based extractor for Supplier Invoices."""

    @property
    def document_type(self) -> str:
        return "SUPPLIER_INVOICE"

    def extract(self, pages: List[Dict[str, Any]]) -> ExtractionOutput:
        fields: Dict[str, Any] = {
            "invoice_number": None,
            "po_reference": None,
            "supplier_name": None,
            "invoice_date": None,
            "due_date": None,
            "currency": "USD",
            "subtotal": None,
            "tax_amount": None,
            "grand_total": None,
        }
        line_items: List[Dict[str, Any]] = []
        provenance: Dict[str, Dict[str, Any]] = {}
        warnings: List[Dict[str, Any]] = []
        needs_human_review = False

        if not pages:
            warnings.append(
                ExtractionWarning(
                    field_name="document",
                    warning_code="EMPTY_OCR_INPUT",
                    message="Document contains no OCR pages.",
                ).to_dict()
            )
            return ExtractionOutput(
                document_type=self.document_type,
                extractor_version=self.extractor_version,
                fields=fields,
                line_items=line_items,
                provenance=provenance,
                warnings=warnings,
                needs_human_review=True,
            )

        full_text_by_page = []
        for p in pages:
            p_num = p.get("page_number", 1)
            raw = p.get("raw_text", "")
            full_text_by_page.append((p_num, raw, p.get("text_blocks") or []))

        # 1. Extract Invoice Number
        inv_pat = re.compile(
            r"\b(?:tax\s+invoice\s*(?:no\.?\b|number\b|#)|commercial\s+invoice\s*(?:no\.?\b|number\b|#)|invoice\s*(?:no\.?\b|number\b|#)|inv\s*(?:no\.?\b|number\b|#)|invoice\s*#|invoice\s*:)\s*[:\-]?\s*([A-Za-z0-9_\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = inv_pat.search(raw)
            if m:
                extracted_val = m.group(1).strip()
                fields["invoice_number"] = extracted_val
                provenance["invoice_number"] = FieldProvenance(
                    field_name="invoice_number",
                    extracted_value=extracted_val,
                    raw_source_text=m.group(0),
                    page_number=p_num,
                    confidence=0.95,
                    extraction_method="REGEX_ANCHOR",
                ).to_dict()
                break

        if not fields["invoice_number"]:
            warnings.append(
                ExtractionWarning(
                    field_name="invoice_number",
                    warning_code="MISSING_IDENTIFIER",
                    message="Invoice number could not be found.",
                ).to_dict()
            )
            needs_human_review = True

        # 2. Extract PO Reference
        po_ref_pat = re.compile(
            r"(?:against\s+p\.?o\.?|po\s+ref(?:erence)?|purchase\s+order\s+ref(?:erence)?|p\.?o\.?\s*(?:no\.?|number|#)?)\s*[:\-]?\s*([A-Za-z0-9_\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = po_ref_pat.search(raw)
            if m:
                val = m.group(1).strip()
                if val != fields["invoice_number"]:
                    fields["po_reference"] = val
                    provenance["po_reference"] = FieldProvenance(
                        field_name="po_reference",
                        extracted_value=val,
                        raw_source_text=m.group(0),
                        page_number=p_num,
                        confidence=0.90,
                        extraction_method="REGEX_ANCHOR",
                    ).to_dict()
                    break

        # 3. Extract Supplier Name
        supp_pat = re.compile(
            r"(?:vendor|supplier|from|seller|remit\s+to)\s*[:\-]?\s*([A-Za-z0-9\s,\.\-&]+?)(?=\n|$|bill\s+to|date|order|tax)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = supp_pat.search(raw)
            if m:
                s_name = m.group(1).strip()
                if len(s_name) > 2:
                    fields["supplier_name"] = s_name
                    provenance["supplier_name"] = FieldProvenance(
                        field_name="supplier_name",
                        extracted_value=s_name,
                        raw_source_text=m.group(0),
                        page_number=p_num,
                        confidence=0.85,
                        extraction_method="KEYWORD_ANCHOR",
                    ).to_dict()
                    break

        # 4. Extract Invoice Date
        inv_date_pat = re.compile(
            r"(?:invoice\s+date|bill\s+date|date)\s*[:\-]?\s*([A-Za-z0-9\s,\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = inv_date_pat.search(raw)
            if m:
                date_str = m.group(1).strip()
                parsed_date, date_err = parse_date_value(date_str)
                if parsed_date:
                    fields["invoice_date"] = str(parsed_date)
                    provenance["invoice_date"] = FieldProvenance(
                        field_name="invoice_date",
                        extracted_value=str(parsed_date),
                        raw_source_text=m.group(0),
                        page_number=p_num,
                        confidence=0.90,
                        extraction_method="REGEX_ANCHOR",
                    ).to_dict()
                    break
                elif date_err:
                    warnings.append(
                        ExtractionWarning(
                            field_name="invoice_date",
                            warning_code="INVALID_DATE",
                            message=date_err,
                            raw_value=date_str,
                        ).to_dict()
                    )
                    needs_human_review = True

        # 5. Extract Due Date
        due_date_pat = re.compile(
            r"(?:due\s+date|payment\s+due|pay\s+by)\s*[:\-]?\s*([A-Za-z0-9\s,\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = due_date_pat.search(raw)
            if m:
                date_str = m.group(1).strip()
                parsed_date, date_err = parse_date_value(date_str)
                if parsed_date:
                    fields["due_date"] = str(parsed_date)
                    provenance["due_date"] = FieldProvenance(
                        field_name="due_date",
                        extracted_value=str(parsed_date),
                        raw_source_text=m.group(0),
                        page_number=p_num,
                        confidence=0.85,
                        extraction_method="REGEX_ANCHOR",
                    ).to_dict()
                    break

        # 6. Extract Currency
        curr_pat = re.compile(r"\b(USD|\$|EUR|GBP|PKR|INR)\b", re.IGNORECASE)
        for p_num, raw, _ in full_text_by_page:
            m = curr_pat.search(raw)
            if m:
                c_code, c_warn = normalize_currency_code(m.group(1))
                fields["currency"] = c_code
                provenance["currency"] = FieldProvenance(
                    field_name="currency",
                    extracted_value=c_code,
                    raw_source_text=m.group(0),
                    page_number=p_num,
                    confidence=0.90,
                    extraction_method="REGEX_ANCHOR",
                ).to_dict()
                if c_warn:
                    warnings.append(
                        ExtractionWarning(
                            field_name="currency",
                            warning_code="UNSUPPORTED_CURRENCY",
                            message=c_warn,
                            raw_value=m.group(1),
                        ).to_dict()
                    )
                    needs_human_review = True
                break

        # 7. Extract Totals: Subtotal, Tax, Grand Total
        def _extract_amount(pattern: str, field_name: str):
            rx = re.compile(pattern, re.IGNORECASE)
            for p_num, raw, _ in full_text_by_page:
                m = rx.search(raw)
                if m:
                    raw_val = m.group(1)
                    val, err = parse_decimal_value(raw_val)
                    if val is not None:
                        fields[field_name] = str(val)
                        provenance[field_name] = FieldProvenance(
                            field_name=field_name,
                            extracted_value=str(val),
                            raw_source_text=m.group(0),
                            page_number=p_num,
                            confidence=0.90,
                            extraction_method="REGEX_ANCHOR",
                        ).to_dict()
                        if val < 0:
                            warnings.append(
                                ExtractionWarning(
                                    field_name=field_name,
                                    warning_code="NEGATIVE_VALUE",
                                    message=f"{field_name} cannot be negative ({val})",
                                    raw_value=raw_val,
                                ).to_dict()
                            )
                            needs_human_review = True
                        return val
                    elif err:
                        warnings.append(
                            ExtractionWarning(
                                field_name=field_name,
                                warning_code="PARSE_ERROR",
                                message=err,
                                raw_value=raw_val,
                            ).to_dict()
                        )
                        needs_human_review = True
            return None

        subtotal_val = _extract_amount(r"\b(?:subtotal|sub-total|sub\s+total|net\s+amount|taxable\s+amount)\s*[:\-]?\s*([$]?\s*[\d,]+\.?\d*)", "subtotal")
        tax_val = _extract_amount(r"\b(?:sales\s+tax|total\s+tax|tax|vat|gst)\s*[:\-]?\s*([$]?\s*[\d,]+\.?\d*)", "tax_amount")
        total_val = _extract_amount(r"(?<!sub)(?<!sub-)(?<!sub\s)\b(?:grand\s+total|total\s+amount|invoice\s+total|amount\s+due|total)\s*[:\-]?\s*([$]?\s*[\d,]+\.?\d*)", "grand_total")

        # 8. Extract Line Items
        inv_line_pat = re.compile(
            r"(?:^|\n)\s*(?:(?P<code>[A-Za-z0-9_\-\.]+)\s+)?(?P<desc>[A-Za-z0-9\s,\.\-&]+?)\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<uom>pcs|units|kg|box|boxes|ea|m|ltr|packs)?\s+[@$]?\s*(?P<price>\d+(?:\.\d+)?)\s+[$]?(?P<total>\d+(?:\.\d+)?)?",
            re.IGNORECASE,
        )

        for p_num, raw, _ in full_text_by_page:
            for m in inv_line_pat.finditer(raw):
                desc_text = m.group("desc").strip()
                if any(w in desc_text.lower() for w in ["description", "subtotal", "total", "item", "qty", "price", "amount"]):
                    continue
                if len(desc_text) < 2:
                    continue

                qty_dec, _ = parse_decimal_value(m.group("qty"))
                price_dec, _ = parse_decimal_value(m.group("price"))
                total_dec, _ = parse_decimal_value(m.group("total")) if m.group("total") else (None, None)

                if qty_dec is not None and price_dec is not None:
                    if qty_dec <= 0:
                        warnings.append(
                            ExtractionWarning(
                                field_name="line_items.quantity",
                                warning_code="NON_POSITIVE_QUANTITY",
                                message=f"Line item '{desc_text}' has non-positive quantity: {qty_dec}",
                            ).to_dict()
                        )
                        needs_human_review = True

                    calculated_total = (qty_dec * price_dec).quantize(Decimal("0.01"))
                    if total_dec is None:
                        total_dec = calculated_total
                    elif abs(total_dec - calculated_total) > Decimal("0.05"):
                        warnings.append(
                            ExtractionWarning(
                                field_name="line_items.line_total",
                                warning_code="INCONSISTENT_LINE_TOTAL",
                                message=f"Line total {total_dec} does not match qty {qty_dec} * price {price_dec} = {calculated_total}",
                                raw_value=str(total_dec),
                            ).to_dict()
                        )
                        needs_human_review = True

                    line_items.append({
                        "item_description": desc_text,
                        "item_code": m.group("code") or None,
                        "quantity": str(qty_dec),
                        "unit_of_measure": (m.group("uom") or "pcs").lower(),
                        "unit_price": str(price_dec),
                        "line_total": str(total_dec) if total_dec is not None else str(calculated_total),
                        "page_number": p_num,
                    })

        if not line_items:
            warnings.append(
                ExtractionWarning(
                    field_name="line_items",
                    warning_code="NO_LINE_ITEMS_EXTRACTED",
                    message="No line items could be extracted from invoice text.",
                ).to_dict()
            )
            needs_human_review = True

        # 9. Total Consistency Check
        if subtotal_val is not None and total_val is not None:
            expected_total = subtotal_val + (tax_val or Decimal("0.00"))
            if abs(total_val - expected_total) > Decimal("0.05"):
                warnings.append(
                    ExtractionWarning(
                        field_name="grand_total",
                        warning_code="INCONSISTENT_TOTALS",
                        message=f"Grand total {total_val} does not match subtotal {subtotal_val} + tax {tax_val or 0} = {expected_total}",
                        raw_value=str(total_val),
                    ).to_dict()
                )
                needs_human_review = True

        return ExtractionOutput(
            document_type=self.document_type,
            extractor_version=self.extractor_version,
            fields=fields,
            line_items=line_items,
            provenance=provenance,
            warnings=warnings,
            needs_human_review=needs_human_review,
        )
