"""Goods Receipt Note structured data extractor."""

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
    parse_date_value,
    parse_decimal_value,
)


class GoodsReceiptNoteExtractor(BaseExtractor):
    """Deterministic, rule-based extractor for Goods Receipt Notes (GRNs)."""

    @property
    def document_type(self) -> str:
        return "GOODS_RECEIPT_NOTE"

    def extract(self, pages: List[Dict[str, Any]]) -> ExtractionOutput:
        fields: Dict[str, Any] = {
            "grn_number": None,
            "po_reference": None,
            "dc_reference": None,
            "supplier_name": None,
            "receipt_date": None,
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

        # 1. Extract GRN Number
        grn_pat = re.compile(
            r"\b(?:goods\s+receipt\s+(?:note\s+)?(?:no\.?\b|number\b|#)|receiving\s+report\s*(?:no\.?\b|number\b|#)|grn\s*(?:no\.?\b|number\b|#)|grn\s*#|grn\s*:)\s*[:\-]?\s*([A-Za-z0-9_\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = grn_pat.search(raw)
            if m:
                extracted_val = m.group(1).strip()
                fields["grn_number"] = extracted_val
                provenance["grn_number"] = FieldProvenance(
                    field_name="grn_number",
                    extracted_value=extracted_val,
                    raw_source_text=m.group(0),
                    page_number=p_num,
                    confidence=0.95,
                    extraction_method="REGEX_ANCHOR",
                ).to_dict()
                break

        if not fields["grn_number"]:
            warnings.append(
                ExtractionWarning(
                    field_name="grn_number",
                    warning_code="MISSING_IDENTIFIER",
                    message="Goods receipt note number could not be found.",
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
                if val != fields["grn_number"]:
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

        # 3. Extract DC Reference
        dc_ref_pat = re.compile(
            r"(?:dc\s+ref(?:erence)?|challan\s+ref(?:erence)?|challan\s+(?:no\.?|number|#)?|dc\s*(?:no\.?|number|#)?)\s*[:\-]?\s*([A-Za-z0-9_\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = dc_ref_pat.search(raw)
            if m:
                val = m.group(1).strip()
                if val not in (fields["grn_number"], fields["po_reference"]):
                    fields["dc_reference"] = val
                    provenance["dc_reference"] = FieldProvenance(
                        field_name="dc_reference",
                        extracted_value=val,
                        raw_source_text=m.group(0),
                        page_number=p_num,
                        confidence=0.85,
                        extraction_method="REGEX_ANCHOR",
                    ).to_dict()
                    break

        # 4. Extract Supplier Name
        supp_pat = re.compile(
            r"(?:supplier|vendor|from)\s*[:\-]?\s*([A-Za-z0-9\s,\.\-&]+?)(?=\n|$|to|store|date|received)",
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

        # 5. Extract Receipt Date
        date_pat = re.compile(
            r"(?:receipt\s+date|received\s+date|grn\s+date|date)\s*[:\-]?\s*([A-Za-z0-9\s,\-\/]+)",
            re.IGNORECASE,
        )
        for p_num, raw, _ in full_text_by_page:
            m = date_pat.search(raw)
            if m:
                date_str = m.group(1).strip()
                parsed_date, date_err = parse_date_value(date_str)
                if parsed_date:
                    fields["receipt_date"] = str(parsed_date)
                    provenance["receipt_date"] = FieldProvenance(
                        field_name="receipt_date",
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
                            field_name="receipt_date",
                            warning_code="INVALID_DATE",
                            message=date_err,
                            raw_value=date_str,
                        ).to_dict()
                    )
                    needs_human_review = True

        # 6. Extract Line Items (received, accepted, rejected quantities)
        # e.g.: ITEM-101 | Widgets 100 pcs 95 5
        grn_line_pat = re.compile(
            r"(?:^|\n)\s*(?:(?P<code>[A-Za-z0-9_\-\.]+)\s+)?(?P<desc>[A-Za-z0-9\s,\.\-&]+?)\s+(?P<rec_qty>\d+(?:\.\d+)?)\s*(?P<uom>pcs|units|kg|box|boxes|ea|m|ltr|packs)?(?:\s+(?P<acc_qty>\d+(?:\.\d+)?))?(?:\s+(?P<rej_qty>\d+(?:\.\d+)?))?",
            re.IGNORECASE,
        )

        for p_num, raw, _ in full_text_by_page:
            for m in grn_line_pat.finditer(raw):
                desc_text = m.group("desc").strip()
                if any(w in desc_text.lower() for w in ["description", "grn", "item", "qty", "received", "accepted"]):
                    continue
                if len(desc_text) < 2:
                    continue

                rec_dec, _ = parse_decimal_value(m.group("rec_qty"))
                acc_dec, _ = parse_decimal_value(m.group("acc_qty")) if m.group("acc_qty") else (None, None)
                rej_dec, _ = parse_decimal_value(m.group("rej_qty")) if m.group("rej_qty") else (None, None)

                if rec_dec is not None:
                    if rec_dec < 0:
                        warnings.append(
                            ExtractionWarning(
                                field_name="line_items.received_quantity",
                                warning_code="NEGATIVE_QUANTITY",
                                message=f"Received quantity cannot be negative: {rec_dec}",
                            ).to_dict()
                        )
                        needs_human_review = True

                    # Check accepted + rejected consistency if both given
                    if acc_dec is not None and rej_dec is not None:
                        if (acc_dec + rej_dec) != rec_dec:
                            warnings.append(
                                ExtractionWarning(
                                    field_name="line_items.quantity_mismatch",
                                    warning_code="ACCEPTED_REJECTED_MISMATCH",
                                    message=f"Accepted ({acc_dec}) + Rejected ({rej_dec}) != Received ({rec_dec}) for '{desc_text}'",
                                ).to_dict()
                            )
                            needs_human_review = True

                    line_items.append({
                        "item_description": desc_text,
                        "item_code": m.group("code") or None,
                        "received_quantity": str(rec_dec),
                        "accepted_quantity": str(acc_dec) if acc_dec is not None else str(rec_dec),
                        "rejected_quantity": str(rej_dec) if rej_dec is not None else "0",
                        "unit_of_measure": (m.group("uom") or "pcs").lower(),
                        "page_number": p_num,
                    })

        if not line_items:
            warnings.append(
                ExtractionWarning(
                    field_name="line_items",
                    warning_code="NO_LINE_ITEMS_EXTRACTED",
                    message="No received line items could be extracted from document text.",
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
