"""Document matching and relationship management domain service.

Connects verified Purchase Orders, Delivery Challans, Goods Receipt Notes,
and Supplier Invoices belonging to the same purchasing transaction.
"""

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from apps.api.models import (
    Document,
    DocumentMatchAudit,
    DocumentRelationship,
    DocumentVerification,
    PurchaseOrder,
    Supplier,
)
from apps.api.services.ocr_service import DocumentNotFoundError


class MatchingServiceError(Exception):
    """Base exception for document matching."""
    pass


class DocumentNotVerifiedError(MatchingServiceError):
    """Raised when matching is attempted on a document without approved human verification."""
    pass


class AmbiguousMatchError(MatchingServiceError):
    """Raised when multiple valid match candidates exist and manual intervention is required."""
    pass


class IncompatibleRelationshipError(MatchingServiceError):
    """Raised when an invalid or prohibited document relationship is attempted."""
    pass


class RelationshipNotFoundError(MatchingServiceError):
    """Raised when a document relationship does not exist."""
    pass


# Supported document types
DOCUMENT_TYPES = {
    "PURCHASE_ORDER",
    "DELIVERY_CHALLAN",
    "GOODS_RECEIPT_NOTE",
    "SUPPLIER_INVOICE",
}

# Standard relationship types
RELATIONSHIP_TYPES = {
    ("DELIVERY_CHALLAN", "PURCHASE_ORDER"): "DELIVERY_CHALLAN_TO_PURCHASE_ORDER",
    ("GOODS_RECEIPT_NOTE", "PURCHASE_ORDER"): "GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER",
    ("GOODS_RECEIPT_NOTE", "DELIVERY_CHALLAN"): "GOODS_RECEIPT_NOTE_TO_DELIVERY_CHALLAN",
    ("SUPPLIER_INVOICE", "PURCHASE_ORDER"): "SUPPLIER_INVOICE_TO_PURCHASE_ORDER",
    ("SUPPLIER_INVOICE", "GOODS_RECEIPT_NOTE"): "SUPPLIER_INVOICE_TO_GOODS_RECEIPT_NOTE",
    ("SUPPLIER_INVOICE", "DELIVERY_CHALLAN"): "SUPPLIER_INVOICE_TO_DELIVERY_CHALLAN",
}


def normalize_supplier_name(name: Optional[str]) -> str:
    """Normalize company/supplier name for strict comparison."""
    if not name:
        return ""
    cleaned = name.lower().strip()
    # Remove punctuation
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    # Remove common business entity suffixes
    suffixes = [r"\binc\b", r"\bincorporated\b", r"\bcorp\b", r"\bcorporation\b",
                r"\bllc\b", r"\bltd\b", r"\blimited\b", r"\bpvt\b", r"\bco\b", r"\bcompany\b"]
    for s in suffixes:
        cleaned = re.sub(s, "", cleaned)
    # Normalize whitespaces
    return " ".join(cleaned.split())


class DocumentMatchingService:
    """Domain service managing document matching rules, relationships, and audit history."""

    def __init__(self, db: Session):
        self.db = db

    def _get_document(self, document_id: uuid.UUID, lock: bool = False) -> Document:
        """Fetch document by ID, optionally acquiring row lock."""
        query = select(Document).where(Document.id == document_id)
        if lock:
            query = query.with_for_update()
        doc = self.db.execute(query).scalar_one_or_none()
        if not doc:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")
        return doc

    def _get_approved_verification(self, document_id: uuid.UUID) -> DocumentVerification:
        """Fetch latest APPROVED verification session. Only approved sessions are authoritative."""
        verification = self.db.execute(
            select(DocumentVerification)
            .where(
                DocumentVerification.document_id == document_id,
                DocumentVerification.status == "APPROVED",
            )
            .order_by(desc(DocumentVerification.version))
        ).scalars().first()

        if not verification:
            raise DocumentNotVerifiedError(
                f"Document '{document_id}' cannot be matched: only APPROVED human-verified "
                "data can serve as an authoritative source for matching."
            )
        return verification

    def _validate_suppliers_consistent(
        self,
        src_supplier_id: Optional[uuid.UUID],
        src_supplier_name: Optional[str],
        tgt_supplier_id: Optional[uuid.UUID],
        tgt_supplier_name: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        """Verify supplier compatibility between two matched candidate documents."""
        # 1. Exact ID check if both have persistent supplier IDs
        if src_supplier_id and tgt_supplier_id:
            if src_supplier_id != tgt_supplier_id:
                return False, f"Supplier ID mismatch: '{src_supplier_id}' vs '{tgt_supplier_id}'"
            return True, None

        # 2. Text normalization comparison if names are present
        norm_src = normalize_supplier_name(src_supplier_name)
        norm_tgt = normalize_supplier_name(tgt_supplier_name)

        if norm_src and norm_tgt:
            if norm_src != norm_tgt and norm_src not in norm_tgt and norm_tgt not in norm_src:
                return False, f"Supplier name conflict: '{src_supplier_name}' vs '{tgt_supplier_name}'"

        return True, None

    def _find_verified_candidates(
        self,
        target_doc_type: str,
        target_reference_number: str,
    ) -> List[Tuple[Document, DocumentVerification]]:
        """Search for approved documents of target type whose verified number matches reference."""
        clean_ref = target_reference_number.strip()
        if not clean_ref:
            return []

        # Find all documents of target type
        docs = self.db.execute(
            select(Document)
            .where(
                Document.document_type == target_doc_type,
                Document.status == "VERIFIED",
            )
        ).scalars().all()

        matching_candidates = []
        for doc in docs:
            # Check latest approved verification
            ver = self.db.execute(
                select(DocumentVerification)
                .where(
                    DocumentVerification.document_id == doc.id,
                    DocumentVerification.status == "APPROVED",
                )
                .order_by(desc(DocumentVerification.version))
            ).scalars().first()

            if not ver:
                continue

            fields = ver.verified_fields or {}
            # Candidate matches if doc.document_number matches or verified type-specific number matches
            candidate_numbers = {
                doc.document_number,
                fields.get("po_number"),
                fields.get("dc_number"),
                fields.get("grn_number"),
                fields.get("invoice_number"),
            }
            # Remove None / empty
            candidate_numbers = {str(n).strip() for n in candidate_numbers if n}

            if clean_ref in candidate_numbers:
                matching_candidates.append((doc, ver))

        return matching_candidates

    def match_document(
        self,
        document_id: uuid.UUID,
        actor: str = "SYSTEM",
    ) -> Dict[str, Any]:
        """Execute document-type-aware deterministic matching rules for a document."""
        # 1. Row-lock source document and require APPROVED human verification
        doc = self._get_document(document_id, lock=True)
        verification = self._get_approved_verification(document_id)
        fields = verification.verified_fields or {}

        doc_type = doc.document_type.upper()
        results: List[DocumentRelationship] = []
        audits: List[DocumentMatchAudit] = []

        overall_status = "MATCHED"

        if doc_type == "DELIVERY_CHALLAN":
            res, aud, st = self._match_delivery_challan(doc, verification, fields, actor)
            results.extend(res)
            audits.extend(aud)
            overall_status = st

        elif doc_type == "GOODS_RECEIPT_NOTE":
            res, aud, st = self._match_goods_receipt_note(doc, verification, fields, actor)
            results.extend(res)
            audits.extend(aud)
            overall_status = st

        elif doc_type == "SUPPLIER_INVOICE":
            res, aud, st = self._match_supplier_invoice(doc, verification, fields, actor)
            results.extend(res)
            audits.extend(aud)
            overall_status = st

        elif doc_type == "PURCHASE_ORDER":
            res, aud, st = self._match_purchase_order(doc, verification, fields, actor)
            results.extend(res)
            audits.extend(aud)
            overall_status = st

        else:
            raise IncompatibleRelationshipError(f"Unsupported document type '{doc_type}' for matching.")

        self.db.commit()

        # Build complete transaction relationship chain
        chain = self.get_transaction_chain(document_id)

        return {
            "document_id": doc.id,
            "document_type": doc_type,
            "match_status": overall_status,
            "relationships": results,
            "audits": audits,
            "transaction_chain": chain,
        }

    def _match_delivery_challan(
        self,
        dc_doc: Document,
        dc_ver: DocumentVerification,
        fields: Dict[str, Any],
        actor: str,
    ) -> Tuple[List[DocumentRelationship], List[DocumentMatchAudit], str]:
        """Delivery Challan -> Purchase Order matching using explicit PO references."""
        po_ref = fields.get("po_reference") or dc_doc.po_reference
        if not po_ref or not str(po_ref).strip():
            audit = DocumentMatchAudit(
                source_document_id=dc_doc.id,
                action="NO_REFERENCE_FOUND",
                actor=actor,
                reason="Delivery Challan contains no explicit Purchase Order reference.",
                evidence={"document_type": "DELIVERY_CHALLAN"},
            )
            self.db.add(audit)
            return [], [audit], "UNMATCHED_NO_REFERENCE"

        po_ref = str(po_ref).strip()
        candidates = self._find_verified_candidates("PURCHASE_ORDER", po_ref)

        if len(candidates) == 0:
            audit = DocumentMatchAudit(
                source_document_id=dc_doc.id,
                action="TARGET_NOT_FOUND",
                actor=actor,
                reason=f"No approved Purchase Order found matching reference '{po_ref}'.",
                evidence={"po_reference": po_ref, "candidate_count": 0},
            )
            self.db.add(audit)
            return [], [audit], "UNMATCHED_TARGET_NOT_FOUND"

        if len(candidates) > 1:
            # Ambiguous: multiple POs match reference
            relationships = []
            audits = []
            for target_doc, _ in candidates:
                rel = self._create_or_update_relationship(
                    source_doc=dc_doc,
                    target_doc=target_doc,
                    rel_type="DELIVERY_CHALLAN_TO_PURCHASE_ORDER",
                    status="FLAGGED_AMBIGUOUS",
                    method="AUTOMATIC_EXPLICIT_REFERENCE",
                    references={"po_reference": po_ref},
                    evidence={"ambiguity_reason": f"Found {len(candidates)} candidate Purchase Orders", "candidates": [str(c[0].id) for c in candidates]},
                    actor=actor,
                )
                audit = DocumentMatchAudit(
                    relationship_id=rel.id,
                    source_document_id=dc_doc.id,
                    target_document_id=target_doc.id,
                    action="MATCH_AMBIGUOUS_FLAGGED",
                    new_status="FLAGGED_AMBIGUOUS",
                    actor=actor,
                    reason=f"Multiple candidate Purchase Orders ({len(candidates)}) match reference '{po_ref}'. Manual resolution required.",
                    evidence={"candidates": [str(c[0].id) for c in candidates]},
                )
                self.db.add(audit)
                relationships.append(rel)
                audits.append(audit)
            return relationships, audits, "AMBIGUOUS"

        # Exactly 1 candidate found
        target_doc, target_ver = candidates[0]
        tgt_fields = target_ver.verified_fields or {}

        # Validate supplier consistency
        src_supp_name = fields.get("supplier_name")
        tgt_supp_name = tgt_fields.get("supplier_name")
        is_consistent, mismatch_reason = self._validate_suppliers_consistent(
            dc_doc.supplier_id, src_supp_name,
            target_doc.supplier_id, tgt_supp_name
        )

        if not is_consistent:
            rel = self._create_or_update_relationship(
                source_doc=dc_doc,
                target_doc=target_doc,
                rel_type="DELIVERY_CHALLAN_TO_PURCHASE_ORDER",
                status="REJECTED",
                method="AUTOMATIC_EXPLICIT_REFERENCE",
                references={"po_reference": po_ref},
                evidence={"supplier_mismatch": mismatch_reason, "dc_supplier": src_supp_name, "po_supplier": tgt_supp_name},
                actor=actor,
            )
            audit = DocumentMatchAudit(
                relationship_id=rel.id,
                source_document_id=dc_doc.id,
                target_document_id=target_doc.id,
                action="SUPPLIER_MISMATCH",
                new_status="REJECTED",
                actor=actor,
                reason=f"Supplier mismatch between DC and PO: {mismatch_reason}",
                evidence={"dc_supplier": src_supp_name, "po_supplier": tgt_supp_name},
            )
            self.db.add(audit)
            return [rel], [audit], "SUPPLIER_MISMATCH"

        # Confirmed match!
        rel = self._create_or_update_relationship(
            source_doc=dc_doc,
            target_doc=target_doc,
            rel_type="DELIVERY_CHALLAN_TO_PURCHASE_ORDER",
            status="CONFIRMED",
            method="AUTOMATIC_EXPLICIT_REFERENCE",
            references={"po_reference": po_ref},
            evidence={"supplier_validated": True, "po_number": po_ref},
            actor=actor,
        )
        audit = DocumentMatchAudit(
            relationship_id=rel.id,
            source_document_id=dc_doc.id,
            target_document_id=target_doc.id,
            action="MATCH_CONFIRMED",
            new_status="CONFIRMED",
            actor=actor,
            reason=f"Confirmed match: Delivery Challan references approved Purchase Order '{po_ref}'.",
            evidence={"po_reference": po_ref},
        )
        self.db.add(audit)

        # Update purchase_order_id if target PO is linked
        if target_doc.purchase_order_id and not dc_doc.purchase_order_id:
            dc_doc.purchase_order_id = target_doc.purchase_order_id

        return [rel], [audit], "MATCHED"

    def _match_goods_receipt_note(
        self,
        grn_doc: Document,
        grn_ver: DocumentVerification,
        fields: Dict[str, Any],
        actor: str,
    ) -> Tuple[List[DocumentRelationship], List[DocumentMatchAudit], str]:
        """Goods Receipt Note -> Purchase Order and Delivery Challan matching."""
        po_ref = fields.get("po_reference") or grn_doc.po_reference
        dc_ref = fields.get("dc_reference") or fields.get("delivery_challan_number")

        if not po_ref and not dc_ref:
            audit = DocumentMatchAudit(
                source_document_id=grn_doc.id,
                action="NO_REFERENCE_FOUND",
                actor=actor,
                reason="Goods Receipt Note contains neither Purchase Order nor Delivery Challan references.",
                evidence={"document_type": "GOODS_RECEIPT_NOTE"},
            )
            self.db.add(audit)
            return [], [audit], "UNMATCHED_NO_REFERENCE"

        relationships = []
        audits = []
        matched_any = False
        had_ambiguity = False
        had_mismatch = False

        # 1. Match to Purchase Order if po_ref present
        if po_ref:
            po_ref_str = str(po_ref).strip()
            po_candidates = self._find_verified_candidates("PURCHASE_ORDER", po_ref_str)
            if len(po_candidates) == 0:
                audit = DocumentMatchAudit(
                    source_document_id=grn_doc.id,
                    action="TARGET_NOT_FOUND",
                    actor=actor,
                    reason=f"No approved Purchase Order found matching reference '{po_ref_str}'.",
                    evidence={"po_reference": po_ref_str},
                )
                self.db.add(audit)
                audits.append(audit)
            elif len(po_candidates) > 1:
                had_ambiguity = True
                for target_doc, _ in po_candidates:
                    rel = self._create_or_update_relationship(
                        source_doc=grn_doc,
                        target_doc=target_doc,
                        rel_type="GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER",
                        status="FLAGGED_AMBIGUOUS",
                        method="AUTOMATIC_EXPLICIT_REFERENCE",
                        references={"po_reference": po_ref_str},
                        evidence={"ambiguity_reason": f"Found {len(po_candidates)} candidate Purchase Orders"},
                        actor=actor,
                    )
                    audit = DocumentMatchAudit(
                        relationship_id=rel.id,
                        source_document_id=grn_doc.id,
                        target_document_id=target_doc.id,
                        action="MATCH_AMBIGUOUS_FLAGGED",
                        new_status="FLAGGED_AMBIGUOUS",
                        actor=actor,
                        reason=f"Multiple candidate Purchase Orders ({len(po_candidates)}) match reference '{po_ref_str}'.",
                    )
                    self.db.add(audit)
                    relationships.append(rel)
                    audits.append(audit)
            else:
                target_doc, target_ver = po_candidates[0]
                tgt_fields = target_ver.verified_fields or {}
                is_consistent, mismatch_reason = self._validate_suppliers_consistent(
                    grn_doc.supplier_id, fields.get("supplier_name"),
                    target_doc.supplier_id, tgt_fields.get("supplier_name")
                )
                if not is_consistent:
                    had_mismatch = True
                    rel = self._create_or_update_relationship(
                        source_doc=grn_doc,
                        target_doc=target_doc,
                        rel_type="GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER",
                        status="REJECTED",
                        method="AUTOMATIC_EXPLICIT_REFERENCE",
                        references={"po_reference": po_ref_str},
                        evidence={"supplier_mismatch": mismatch_reason},
                        actor=actor,
                    )
                    audit = DocumentMatchAudit(
                        relationship_id=rel.id,
                        source_document_id=grn_doc.id,
                        target_document_id=target_doc.id,
                        action="SUPPLIER_MISMATCH",
                        new_status="REJECTED",
                        actor=actor,
                        reason=f"Supplier mismatch between GRN and PO: {mismatch_reason}",
                    )
                    self.db.add(audit)
                    relationships.append(rel)
                    audits.append(audit)
                else:
                    matched_any = True
                    rel = self._create_or_update_relationship(
                        source_doc=grn_doc,
                        target_doc=target_doc,
                        rel_type="GOODS_RECEIPT_NOTE_TO_PURCHASE_ORDER",
                        status="CONFIRMED",
                        method="AUTOMATIC_EXPLICIT_REFERENCE",
                        references={"po_reference": po_ref_str},
                        evidence={"supplier_validated": True, "po_number": po_ref_str},
                        actor=actor,
                    )
                    audit = DocumentMatchAudit(
                        relationship_id=rel.id,
                        source_document_id=grn_doc.id,
                        target_document_id=target_doc.id,
                        action="MATCH_CONFIRMED",
                        new_status="CONFIRMED",
                        actor=actor,
                        reason=f"Confirmed match: GRN references approved Purchase Order '{po_ref_str}'.",
                        evidence={"po_reference": po_ref_str},
                    )
                    self.db.add(audit)
                    relationships.append(rel)
                    audits.append(audit)

        # 2. Match to Delivery Challan if dc_ref present
        if dc_ref:
            dc_ref_str = str(dc_ref).strip()
            dc_candidates = self._find_verified_candidates("DELIVERY_CHALLAN", dc_ref_str)
            if len(dc_candidates) == 0:
                audit = DocumentMatchAudit(
                    source_document_id=grn_doc.id,
                    action="TARGET_NOT_FOUND",
                    actor=actor,
                    reason=f"No approved Delivery Challan found matching reference '{dc_ref_str}'.",
                    evidence={"dc_reference": dc_ref_str},
                )
                self.db.add(audit)
                audits.append(audit)
            elif len(dc_candidates) > 1:
                had_ambiguity = True
                for target_doc, _ in dc_candidates:
                    rel = self._create_or_update_relationship(
                        source_doc=grn_doc,
                        target_doc=target_doc,
                        rel_type="GOODS_RECEIPT_NOTE_TO_DELIVERY_CHALLAN",
                        status="FLAGGED_AMBIGUOUS",
                        method="AUTOMATIC_EXPLICIT_REFERENCE",
                        references={"dc_reference": dc_ref_str},
                        evidence={"ambiguity_reason": f"Found {len(dc_candidates)} candidate Delivery Challans"},
                        actor=actor,
                    )
                    audit = DocumentMatchAudit(
                        relationship_id=rel.id,
                        source_document_id=grn_doc.id,
                        target_document_id=target_doc.id,
                        action="MATCH_AMBIGUOUS_FLAGGED",
                        new_status="FLAGGED_AMBIGUOUS",
                        actor=actor,
                        reason=f"Multiple candidate Delivery Challans ({len(dc_candidates)}) match reference '{dc_ref_str}'.",
                    )
                    self.db.add(audit)
                    relationships.append(rel)
                    audits.append(audit)
            else:
                target_doc, target_ver = dc_candidates[0]
                tgt_fields = target_ver.verified_fields or {}
                is_consistent, mismatch_reason = self._validate_suppliers_consistent(
                    grn_doc.supplier_id, fields.get("supplier_name"),
                    target_doc.supplier_id, tgt_fields.get("supplier_name")
                )
                if not is_consistent:
                    had_mismatch = True
                    rel = self._create_or_update_relationship(
                        source_doc=grn_doc,
                        target_doc=target_doc,
                        rel_type="GOODS_RECEIPT_NOTE_TO_DELIVERY_CHALLAN",
                        status="REJECTED",
                        method="AUTOMATIC_EXPLICIT_REFERENCE",
                        references={"dc_reference": dc_ref_str},
                        evidence={"supplier_mismatch": mismatch_reason},
                        actor=actor,
                    )
                    audit = DocumentMatchAudit(
                        relationship_id=rel.id,
                        source_document_id=grn_doc.id,
                        target_document_id=target_doc.id,
                        action="SUPPLIER_MISMATCH",
                        new_status="REJECTED",
                        actor=actor,
                        reason=f"Supplier mismatch between GRN and DC: {mismatch_reason}",
                    )
                    self.db.add(audit)
                    relationships.append(rel)
                    audits.append(audit)
                else:
                    matched_any = True
                    rel = self._create_or_update_relationship(
                        source_doc=grn_doc,
                        target_doc=target_doc,
                        rel_type="GOODS_RECEIPT_NOTE_TO_DELIVERY_CHALLAN",
                        status="CONFIRMED",
                        method="AUTOMATIC_EXPLICIT_REFERENCE",
                        references={"dc_reference": dc_ref_str},
                        evidence={"supplier_validated": True, "dc_number": dc_ref_str},
                        actor=actor,
                    )
                    audit = DocumentMatchAudit(
                        relationship_id=rel.id,
                        source_document_id=grn_doc.id,
                        target_document_id=target_doc.id,
                        action="MATCH_CONFIRMED",
                        new_status="CONFIRMED",
                        actor=actor,
                        reason=f"Confirmed match: GRN references approved Delivery Challan '{dc_ref_str}'.",
                        evidence={"dc_reference": dc_ref_str},
                    )
                    self.db.add(audit)
                    relationships.append(rel)
                    audits.append(audit)

        status_str = "MATCHED" if matched_any else ("AMBIGUOUS" if had_ambiguity else ("SUPPLIER_MISMATCH" if had_mismatch else "UNMATCHED_TARGET_NOT_FOUND"))
        return relationships, audits, status_str

    def _match_supplier_invoice(
        self,
        inv_doc: Document,
        inv_ver: DocumentVerification,
        fields: Dict[str, Any],
        actor: str,
    ) -> Tuple[List[DocumentRelationship], List[DocumentMatchAudit], str]:
        """Supplier Invoice -> Purchase Order matching using explicit PO references."""
        po_ref = fields.get("po_reference") or inv_doc.po_reference
        if not po_ref or not str(po_ref).strip():
            audit = DocumentMatchAudit(
                source_document_id=inv_doc.id,
                action="NO_REFERENCE_FOUND",
                actor=actor,
                reason="Supplier Invoice contains no explicit Purchase Order reference.",
                evidence={"document_type": "SUPPLIER_INVOICE"},
            )
            self.db.add(audit)
            return [], [audit], "UNMATCHED_NO_REFERENCE"

        po_ref = str(po_ref).strip()
        candidates = self._find_verified_candidates("PURCHASE_ORDER", po_ref)

        if len(candidates) == 0:
            audit = DocumentMatchAudit(
                source_document_id=inv_doc.id,
                action="TARGET_NOT_FOUND",
                actor=actor,
                reason=f"No approved Purchase Order found matching reference '{po_ref}'.",
                evidence={"po_reference": po_ref, "candidate_count": 0},
            )
            self.db.add(audit)
            return [], [audit], "UNMATCHED_TARGET_NOT_FOUND"

        if len(candidates) > 1:
            relationships = []
            audits = []
            for target_doc, _ in candidates:
                rel = self._create_or_update_relationship(
                    source_doc=inv_doc,
                    target_doc=target_doc,
                    rel_type="SUPPLIER_INVOICE_TO_PURCHASE_ORDER",
                    status="FLAGGED_AMBIGUOUS",
                    method="AUTOMATIC_EXPLICIT_REFERENCE",
                    references={"po_reference": po_ref},
                    evidence={"ambiguity_reason": f"Found {len(candidates)} candidate Purchase Orders"},
                    actor=actor,
                )
                audit = DocumentMatchAudit(
                    relationship_id=rel.id,
                    source_document_id=inv_doc.id,
                    target_document_id=target_doc.id,
                    action="MATCH_AMBIGUOUS_FLAGGED",
                    new_status="FLAGGED_AMBIGUOUS",
                    actor=actor,
                    reason=f"Multiple candidate Purchase Orders ({len(candidates)}) match reference '{po_ref}'.",
                )
                self.db.add(audit)
                relationships.append(rel)
                audits.append(audit)
            return relationships, audits, "AMBIGUOUS"

        target_doc, target_ver = candidates[0]
        tgt_fields = target_ver.verified_fields or {}

        # Validate supplier consistency
        src_supp_name = fields.get("supplier_name")
        tgt_supp_name = tgt_fields.get("supplier_name")
        is_consistent, mismatch_reason = self._validate_suppliers_consistent(
            inv_doc.supplier_id, src_supp_name,
            target_doc.supplier_id, tgt_supp_name
        )

        if not is_consistent:
            rel = self._create_or_update_relationship(
                source_doc=inv_doc,
                target_doc=target_doc,
                rel_type="SUPPLIER_INVOICE_TO_PURCHASE_ORDER",
                status="REJECTED",
                method="AUTOMATIC_EXPLICIT_REFERENCE",
                references={"po_reference": po_ref},
                evidence={"supplier_mismatch": mismatch_reason, "inv_supplier": src_supp_name, "po_supplier": tgt_supp_name},
                actor=actor,
            )
            audit = DocumentMatchAudit(
                relationship_id=rel.id,
                source_document_id=inv_doc.id,
                target_document_id=target_doc.id,
                action="SUPPLIER_MISMATCH",
                new_status="REJECTED",
                actor=actor,
                reason=f"Supplier mismatch between Invoice and PO: {mismatch_reason}",
                evidence={"inv_supplier": src_supp_name, "po_supplier": tgt_supp_name},
            )
            self.db.add(audit)
            return [rel], [audit], "SUPPLIER_MISMATCH"

        rel = self._create_or_update_relationship(
            source_doc=inv_doc,
            target_doc=target_doc,
            rel_type="SUPPLIER_INVOICE_TO_PURCHASE_ORDER",
            status="CONFIRMED",
            method="AUTOMATIC_EXPLICIT_REFERENCE",
            references={"po_reference": po_ref},
            evidence={"supplier_validated": True, "po_number": po_ref},
            actor=actor,
        )
        audit = DocumentMatchAudit(
            relationship_id=rel.id,
            source_document_id=inv_doc.id,
            target_document_id=target_doc.id,
            action="MATCH_CONFIRMED",
            new_status="CONFIRMED",
            actor=actor,
            reason=f"Confirmed match: Supplier Invoice references approved Purchase Order '{po_ref}'.",
            evidence={"po_reference": po_ref},
        )
        self.db.add(audit)

        if target_doc.purchase_order_id and not inv_doc.purchase_order_id:
            inv_doc.purchase_order_id = target_doc.purchase_order_id

        return [rel], [audit], "MATCHED"

    def _match_purchase_order(
        self,
        po_doc: Document,
        po_ver: DocumentVerification,
        fields: Dict[str, Any],
        actor: str,
    ) -> Tuple[List[DocumentRelationship], List[DocumentMatchAudit], str]:
        """Trigger matching for PO by finding approved downstream documents referencing it."""
        po_number = po_doc.document_number or fields.get("po_number")
        if not po_number:
            return [], [], "UNMATCHED_NO_REFERENCE"

        # Search for verified downstream documents
        downstream = self.db.execute(
            select(Document)
            .where(
                Document.document_type.in_(["DELIVERY_CHALLAN", "GOODS_RECEIPT_NOTE", "SUPPLIER_INVOICE"]),
                Document.status == "VERIFIED",
            )
        ).scalars().all()

        relationships = []
        audits = []
        for down_doc in downstream:
            ver = self.db.execute(
                select(DocumentVerification)
                .where(
                    DocumentVerification.document_id == down_doc.id,
                    DocumentVerification.status == "APPROVED",
                )
                .order_by(desc(DocumentVerification.version))
            ).scalars().first()

            if not ver:
                continue

            down_fields = ver.verified_fields or {}
            down_ref = down_fields.get("po_reference") or down_doc.po_reference
            if down_ref and str(down_ref).strip() == str(po_number).strip():
                # Trigger matching on that downstream doc pointing to this PO
                res, aud, _ = self.match_document(down_doc.id, actor=actor)
                relationships.extend(res["relationships"])
                audits.extend(aud)

        return relationships, audits, "MATCHED"

    def _create_or_update_relationship(
        self,
        source_doc: Document,
        target_doc: Document,
        rel_type: str,
        status: str,
        method: str,
        references: Optional[Dict[str, Any]] = None,
        evidence: Optional[Dict[str, Any]] = None,
        confidence_score: float = 1.0,
        notes: Optional[str] = None,
        actor: str = "SYSTEM",
    ) -> DocumentRelationship:
        """Create or update a document relationship, preventing duplicates and ensuring consistency."""
        existing = self.db.execute(
            select(DocumentRelationship)
            .where(
                DocumentRelationship.source_document_id == source_doc.id,
                DocumentRelationship.target_document_id == target_doc.id,
                DocumentRelationship.relationship_type == rel_type,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if existing:
            existing.status = status
            existing.matching_method = method
            existing.matched_references = references
            existing.evidence = evidence
            existing.confidence_score = confidence_score
            existing.notes = notes
            existing.updated_at = datetime.now(timezone.utc)
            return existing

        rel = DocumentRelationship(
            source_document_id=source_doc.id,
            target_document_id=target_doc.id,
            relationship_type=rel_type,
            status=status,
            matching_method=method,
            matched_references=references,
            evidence=evidence,
            confidence_score=confidence_score,
            created_by=actor,
            notes=notes,
        )
        self.db.add(rel)
        self.db.flush()
        return rel

    def manual_link(
        self,
        source_document_id: uuid.UUID,
        target_document_id: uuid.UUID,
        relationship_type: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> DocumentRelationship:
        """Create a manual link between two verified documents with complete audit trail."""
        if source_document_id == target_document_id:
            raise IncompatibleRelationshipError("Cannot link a document to itself.")

        src = self._get_document(source_document_id, lock=True)
        tgt = self._get_document(target_document_id, lock=True)

        # Both must have approved verifications
        src_ver = self._get_approved_verification(source_document_id)
        tgt_ver = self._get_approved_verification(target_document_id)

        # Validate types
        expected_pair = (src.document_type.upper(), tgt.document_type.upper())
        if expected_pair not in RELATIONSHIP_TYPES and (tgt.document_type.upper(), src.document_type.upper()) not in RELATIONSHIP_TYPES:
            raise IncompatibleRelationshipError(
                f"Cannot link '{src.document_type}' to '{tgt.document_type}'. Incompatible document types."
            )

        rel = self._create_or_update_relationship(
            source_doc=src,
            target_doc=tgt,
            rel_type=relationship_type,
            status="CONFIRMED",
            method="MANUAL_OVERRIDE",
            evidence={"manual_override": True, "reason": reason},
            confidence_score=1.0,
            notes=reason,
            actor=actor,
        )

        audit = DocumentMatchAudit(
            relationship_id=rel.id,
            source_document_id=src.id,
            target_document_id=tgt.id,
            action="MANUAL_LINK_CREATED",
            new_status="CONFIRMED",
            actor=actor,
            reason=reason or "Manual relationship confirmed by user.",
            evidence={"relationship_type": relationship_type},
        )
        self.db.add(audit)
        self.db.commit()
        self.db.refresh(rel)
        return rel

    def resolve_relationship(
        self,
        relationship_id: uuid.UUID,
        action: str,  # "CONFIRM" or "REJECT"
        actor: str,
        reason: Optional[str] = None,
    ) -> DocumentRelationship:
        """Resolve an ambiguous or proposed relationship manually."""
        rel = self.db.execute(
            select(DocumentRelationship)
            .where(DocumentRelationship.id == relationship_id)
            .with_for_update()
        ).scalar_one_or_none()

        if not rel:
            raise RelationshipNotFoundError(f"Relationship '{relationship_id}' not found.")

        act = action.strip().upper()
        if act not in ("CONFIRM", "REJECT"):
            raise IncompatibleRelationshipError(f"Invalid resolution action '{action}'. Must be CONFIRM or REJECT.")

        prev_status = rel.status
        new_status = "CONFIRMED" if act == "CONFIRM" else "REJECTED"

        rel.status = new_status
        rel.matching_method = "MANUAL_RESOLUTION"
        rel.notes = reason
        rel.updated_at = datetime.now(timezone.utc)

        audit = DocumentMatchAudit(
            relationship_id=rel.id,
            source_document_id=rel.source_document_id,
            target_document_id=rel.target_document_id,
            action="MANUAL_STATUS_UPDATED",
            previous_status=prev_status,
            new_status=new_status,
            actor=actor,
            reason=reason or f"Manually {new_status.lower()}ed by reviewer.",
            evidence={"action": act},
        )
        self.db.add(audit)
        self.db.commit()
        self.db.refresh(rel)
        return rel

    def remove_relationship(
        self,
        relationship_id: uuid.UUID,
        actor: str,
        reason: Optional[str] = None,
    ) -> None:
        """Remove/unlink a relationship with an audit trail entry."""
        rel = self.db.execute(
            select(DocumentRelationship)
            .where(DocumentRelationship.id == relationship_id)
            .with_for_update()
        ).scalar_one_or_none()

        if not rel:
            raise RelationshipNotFoundError(f"Relationship '{relationship_id}' not found.")

        audit = DocumentMatchAudit(
            relationship_id=None,
            source_document_id=rel.source_document_id,
            target_document_id=rel.target_document_id,
            action="MANUAL_LINK_REMOVED",
            previous_status=rel.status,
            new_status=None,
            actor=actor,
            reason=reason or "Relationship manually unlinked.",
            evidence={"deleted_relationship_type": rel.relationship_type},
        )
        self.db.add(audit)
        self.db.delete(rel)
        self.db.commit()

    def get_document_relationships(self, document_id: uuid.UUID) -> Dict[str, Any]:
        """Fetch all incoming and outgoing relationships for a document, along with transaction chain."""
        doc = self._get_document(document_id)

        outgoing = list(
            self.db.execute(
                select(DocumentRelationship)
                .where(DocumentRelationship.source_document_id == document_id)
                .order_by(DocumentRelationship.created_at.asc())
            ).scalars().all()
        )

        incoming = list(
            self.db.execute(
                select(DocumentRelationship)
                .where(DocumentRelationship.target_document_id == document_id)
                .order_by(DocumentRelationship.created_at.asc())
            ).scalars().all()
        )

        chain = self.get_transaction_chain(document_id)

        return {
            "document_id": doc.id,
            "outgoing_relationships": outgoing,
            "incoming_relationships": incoming,
            "transaction_chain": chain,
        }

    def get_transaction_chain(self, document_id: uuid.UUID) -> Dict[str, Any]:
        """Traverse relationship graph to find all documents in the purchasing transaction."""
        visited: Set[uuid.UUID] = set()
        queue: List[uuid.UUID] = [document_id]

        pos: List[Dict[str, Any]] = []
        dcs: List[Dict[str, Any]] = []
        grns: List[Dict[str, Any]] = []
        invoices: List[Dict[str, Any]] = []

        while queue:
            curr_id = queue.pop(0)
            if curr_id in visited:
                continue
            visited.add(curr_id)

            curr_doc = self.db.get(Document, curr_id)
            if not curr_doc:
                continue

            doc_info = {
                "id": curr_doc.id,
                "document_type": curr_doc.document_type,
                "document_number": curr_doc.document_number,
                "po_reference": curr_doc.po_reference,
                "status": curr_doc.status,
            }

            dtype = curr_doc.document_type.upper()
            if dtype == "PURCHASE_ORDER":
                pos.append(doc_info)
            elif dtype == "DELIVERY_CHALLAN":
                dcs.append(doc_info)
            elif dtype == "GOODS_RECEIPT_NOTE":
                grns.append(doc_info)
            elif dtype == "SUPPLIER_INVOICE":
                invoices.append(doc_info)

            # Find connected nodes via CONFIRMED relationships
            edges = self.db.execute(
                select(DocumentRelationship).where(
                    and_(
                        DocumentRelationship.status == "CONFIRMED",
                        or_(
                            DocumentRelationship.source_document_id == curr_id,
                            DocumentRelationship.target_document_id == curr_id,
                        ),
                    )
                )
            ).scalars().all()

            for edge in edges:
                neighbor = edge.target_document_id if edge.source_document_id == curr_id else edge.source_document_id
                if neighbor not in visited:
                    queue.append(neighbor)

        return {
            "root_document_id": document_id,
            "connected_document_count": len(visited),
            "purchase_orders": pos,
            "delivery_challans": dcs,
            "goods_receipt_notes": grns,
            "supplier_invoices": invoices,
        }

    def get_match_audit_trail(self, document_id: uuid.UUID) -> List[DocumentMatchAudit]:
        """Fetch audit log history for a document."""
        return list(
            self.db.execute(
                select(DocumentMatchAudit)
                .where(
                    or_(
                        DocumentMatchAudit.source_document_id == document_id,
                        DocumentMatchAudit.target_document_id == document_id,
                    )
                )
                .order_by(DocumentMatchAudit.created_at.asc())
            ).scalars().all()
        )
