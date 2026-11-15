"""Delivery Challan structured data extractor."""

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


class DeliveryChallanExtractor(BaseExtractor):
    """Deterministic, rule-based extractor for Delivery Challans."""

    @property
    def document_type(self) -> str:
        return "DELIVERY_CHALLAN"

    def extract(self, pages: List[Dict[str, Any]]) -> ExtractionOutput:
        fields: Dict[str, Any] = {
            "dc_number": None,
            "po_reference": None,
            "supplier_name": None,
            "dispatch_date": None,
            "currency": None,
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

        # 1. Extract DC Number (must not be confused with PO Reference)
        dc_pat = re.compile(
            r"\b(?:delivery\s+challan\s*(?:no\.?\b|number\b|#)|challan\s*(?:no\.?\b|number\b|#)|dc\s*(?:no\.?\b|number\b|#)|dc\s*#|dc\s*:)\s*[:\-]?\s*([A-Za-z0-9_\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = dc_pat.search(raw)
            if m:
                extracted_val = m.group(1).strip()
                fields["dc_number"] = extracted_val
                provenance["dc_number"] = FieldProvenance(
                    field_name="dc_number",
                    extracted_value=extracted_val,
                    raw_source_text=m.group(0),
                    page_number=p_num,
                    confidence=0.95,
                    extraction_method="REGEX_ANCHOR",
                ).to_dict()
                break

        if not fields["dc_number"]:
            warnings.append(
                ExtractionWarning(
                    field_name="dc_number",
                    warning_code="MISSING_IDENTIFIER",
                    message="Delivery challan number could not be found.",
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
                # Do not confuse with DC number
                if val != fields["dc_number"]:
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

        # 3. Extract Supplier Name / Transporter
        supp_pat = re.compile(
            r"(?:supplier|consignor|from|vendor)\s*[:\-]?\s*([A-Za-z0-9\s,\.\-&]+?)(?=\n|$|to|consignee|date|vehicle)",
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

        # 4. Extract Dispatch Date
        date_pat = re.compile(
            r"(?:dispatch\s+date|challan\s+date|date)\s*[:\-]?\s*([A-Za-z0-9\s,\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = date_pat.search(raw)
            if m:
                date_str = m.group(1).strip()
                parsed_date, date_err = parse_date_value(date_str)
                if parsed_date:
                    fields["dispatch_date"] = str(parsed_date)
                    provenance["dispatch_date"] = FieldProvenance(
                        field_name="dispatch_date",
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
                            field_name="dispatch_date",
                            warning_code="INVALID_DATE",
                            message=date_err,
                            raw_value=date_str,
                        ).to_dict()
                    )
                    needs_human_review = True

        # 5. Extract Optional Currency
        curr_pat = re.compile(r"\b(USD|\$|EUR|GBP|PKR|INR)\b", re.IGNORECASE)
        for p_num, raw, _ in full_text_by_page:
            m = curr_pat.search(raw)
            if m:
                c_code, c_warn = normalize_currency_code(m.group(1))
                fields["currency"] = c_code
                if c_warn:
                    warnings.append(
                        ExtractionWarning(
                            field_name="currency",
                            warning_code="UNSUPPORTED_CURRENCY",
                            message=c_warn,
                            raw_value=m.group(1),
                        ).to_dict()
                    )
                break

        # 6. Extract Line Items (Dispatched quantity)
        dc_line_pat = re.compile(
            r"(?:^|\n)\s*(?:(?P<code>[A-Za-z0-9_\-\.]+)\s+)?(?P<desc>[A-Za-z0-9\s,\.\-&]+?)\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<uom>pcs|units|kg|box|boxes|ea|m|ltr|packs)?",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            for m in dc_line_pat.finditer(raw):
                desc_text = m.group("desc").strip()
                if any(w in desc_text.lower() for w in ["description", "challan", "item", "qty", "vehicle", "driver"]):
                    continue
                if len(desc_text) < 2:
                    continue

                qty_dec, _ = parse_decimal_value(m.group("qty"))
                if qty_dec is not None:
                    if qty_dec <= 0:
                        warnings.append(
                            ExtractionWarning(
                                field_name="line_items.dispatched_quantity",
                                warning_code="NON_POSITIVE_QUANTITY",
                                message=f"Line item '{desc_text}' has non-positive quantity: {qty_dec}",
                            ).to_dict()
                        )
                        needs_human_review = True

                    line_items.append({
                        "item_description": desc_text,
                        "item_code": m.group("code") or None,
                        "dispatched_quantity": str(qty_dec),
                        "unit_of_measure": (m.group("uom") or "pcs").lower(),
                        "page_number": p_num,
                    })

        if not line_items:
            warnings.append(
                ExtractionWarning(
                    field_name="line_items",
                    warning_code="NO_LINE_ITEMS_EXTRACTED",
                    message="No dispatched line items could be extracted.",
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
