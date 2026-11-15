"""Read-only reporting and dashboard aggregation service."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, selectinload

from apps.api.models import (
    Document,
    DocumentExtraction,
    DocumentVerification,
    PurchaseOrder,
    ReconciliationDiscrepancy,
    ReconciliationException,
    ReconciliationRun,
    Supplier,
)
from apps.api.schedule_models import (
    PgDeliverySchedule,
    PgQuantityAllocation,
    PgSourceDocumentLine,
)
from apps.api.services.reconciliation_exception_service import (
    ReconciliationRunNotFoundError,
)

AWAITING_REVIEW_STATUSES = ("PENDING_REVIEW", "IN_REVIEW", "RESUBMITTED")
OPEN_EXCEPTION_STATUSES = ("OPEN", "UNDER_REVIEW", "REOPENED")
RESOLVED_EXCEPTION_STATUSES = ("RESOLVED",)


class ReportingService:
    """Aggregate persisted records for dashboards and reconciliation reports."""

    def __init__(self, db: Session):
        self.db = db

    def get_dashboard_metrics(self) -> Dict[str, Any]:
        """Return dashboard counts derived exclusively from persisted records."""
        return {
            "total_suppliers": self._count(select(func.count()).select_from(Supplier)),
            "total_purchase_orders": self._count(
                select(func.count()).select_from(PurchaseOrder)
            ),
            "total_documents": self._count(
                select(func.count()).select_from(Document)
            ),
            "documents_awaiting_review": self._count(
                select(func.count(func.distinct(DocumentVerification.document_id))).where(
                    DocumentVerification.status.in_(AWAITING_REVIEW_STATUSES)
                )
            ),
            "open_exceptions": self._count(
                select(func.count()).select_from(ReconciliationException).where(
                    ReconciliationException.status.in_(OPEN_EXCEPTION_STATUSES)
                )
            ),
            "resolved_exceptions": self._count(
                select(func.count()).select_from(ReconciliationException).where(
                    ReconciliationException.status.in_(RESOLVED_EXCEPTION_STATUSES)
                )
            ),
            "late_deliveries": self._count(
                select(
                    func.count(func.distinct(PgDeliverySchedule.schedule_id))
                )
                .select_from(PgQuantityAllocation)
                .join(
                    PgSourceDocumentLine,
                    and_(
                        PgSourceDocumentLine.document_type
                        == PgQuantityAllocation.source_document_type,
                        PgSourceDocumentLine.document_id
                        == PgQuantityAllocation.source_document_id,
                        PgSourceDocumentLine.line_item_id
                        == PgQuantityAllocation.source_line_item_id,
                    ),
                )
                .join(
                    PgDeliverySchedule,
                    PgQuantityAllocation.schedule_id
                    == PgDeliverySchedule.schedule_id,
                )
                .where(
                    PgQuantityAllocation.allocation_type == "RECEIPT",
                    PgSourceDocumentLine.evidence_date
                    > PgDeliverySchedule.expected_delivery_date,
                )
            ),
            "partial_deliveries": self._count(
                select(func.count()).select_from(PgDeliverySchedule).where(
                    PgDeliverySchedule.status == "PARTIALLY_FULFILLED"
                )
            ),
            "financial_discrepancies": self._count(
                select(func.count())
                .select_from(ReconciliationDiscrepancy)
                .where(ReconciliationDiscrepancy.amount_at_risk > 0)
            ),
        }

    def list_exceptions(
        self,
        exception_type: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        supplier: Optional[str] = None,
        po_number: Optional[str] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[ReconciliationException], int]:
        """Return a page of persisted exceptions and the pre-pagination total."""
        query = select(ReconciliationException).options(
            selectinload(ReconciliationException.history)
        )

        if exception_type:
            query = query.where(
                ReconciliationException.exception_type
                == exception_type.strip().upper()
            )
        if severity:
            query = query.where(
                ReconciliationException.severity == severity.strip().upper()
            )
        if status:
            query = query.where(
                ReconciliationException.status == status.strip().upper()
            )
        if supplier:
            query = query.where(
                ReconciliationException.supplier_name.ilike(
                    f"%{supplier.strip()}%"
                )
            )
        if po_number:
            query = query.where(
                ReconciliationException.po_reference.ilike(
                    f"%{po_number.strip()}%"
                )
            )
        if date_from:
            query = query.where(
                ReconciliationException.created_at
                >= datetime.combine(date_from, time.min, tzinfo=timezone.utc)
            )
        if date_to:
            end_exclusive = datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=timezone.utc
            )
            query = query.where(
                ReconciliationException.created_at < end_exclusive
            )

        total = self._count(select(func.count()).select_from(query.subquery()))

        query = (
            query.order_by(
                ReconciliationException.created_at.desc(),
                ReconciliationException.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        items = list(self.db.execute(query).scalars().all())
        return items, total

    def get_reconciliation_report(self, run_id: uuid.UUID) -> Dict[str, Any]:
        """Build a read-only report for a persisted reconciliation run."""
        run = self.db.get(ReconciliationRun, run_id)
        if run is None:
            raise ReconciliationRunNotFoundError(
                f"Reconciliation run '{run_id}' was not found."
            )

        documents = self._report_documents(run)
        po_document = next(
            (doc for doc in documents if doc["is_purchase_order"]), None
        )
        financial_impact: Dict[str, Decimal] = {}
        total_amount_at_risk = Decimal("0.0000")
        for discrepancy in run.discrepancies:
            amount = discrepancy.amount_at_risk or Decimal("0.0000")
            currency = discrepancy.currency or "USD"
            financial_impact[currency] = (
                financial_impact.get(currency, Decimal("0.0000")) + amount
            )
            total_amount_at_risk += amount

        return {
            "run_id": run.id,
            "root_document_id": run.root_document_id,
            "po_document_id": run.po_document_id,
            "status": run.status,
            "summary": run.summary,
            "created_by": run.created_by,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "po_reference": po_document["po_reference"] if po_document else None,
            "supplier_name": (
                po_document["supplier_name"] if po_document else None
            ),
            "documents": documents,
            "discrepancies": list(run.discrepancies),
            "financial_impact_by_currency": financial_impact,
            "total_amount_at_risk": total_amount_at_risk,
        }

    def _report_documents(self, run: ReconciliationRun) -> List[Dict[str, Any]]:
        ordered_ids: List[uuid.UUID] = []

        def add_document_id(raw_id: Any) -> None:
            if raw_id is None:
                return
            try:
                document_id = uuid.UUID(str(raw_id))
            except (ValueError, AttributeError, TypeError):
                return
            if document_id not in ordered_ids:
                ordered_ids.append(document_id)

        add_document_id(run.root_document_id)
        add_document_id(run.po_document_id)
        for raw_id in run.reconciled_document_ids or []:
            add_document_id(raw_id)

        documents: List[Dict[str, Any]] = []
        for document_id in ordered_ids:
            document = self.db.get(Document, document_id)
            if document is None:
                continue

            extraction = (
                self.db.execute(
                    select(DocumentExtraction)
                    .where(DocumentExtraction.document_id == document.id)
                    .order_by(DocumentExtraction.run_number.desc())
                )
                .scalars()
                .first()
            )
            verification = (
                self.db.execute(
                    select(DocumentVerification)
                    .where(
                        DocumentVerification.document_id == document.id,
                        DocumentVerification.status == "APPROVED",
                    )
                    .order_by(DocumentVerification.version.desc())
                )
                .scalars()
                .first()
            )

            documents.append(
                {
                    "document_id": document.id,
                    "document_type": document.document_type,
                    "document_number": document.document_number,
                    "po_reference": document.po_reference,
                    "supplier_name": (
                        document.supplier.name if document.supplier else None
                    ),
                    "status": document.status,
                    "is_root": document.id == run.root_document_id,
                    "is_purchase_order": document.id == run.po_document_id,
                    "ai_extraction": (
                        {
                            "extraction_id": extraction.id,
                            "run_number": extraction.run_number,
                            "status": extraction.status,
                            "fields": extraction.fields,
                            "line_items": extraction.line_items,
                            "field_provenance": extraction.field_provenance,
                            "validation_warnings": extraction.validation_warnings,
                        }
                        if extraction
                        else None
                    ),
                    "human_verification": (
                        {
                            "verification_id": verification.id,
                            "status": verification.status,
                            "version": verification.version,
                            "verified_fields": verification.verified_fields,
                            "verified_line_items": verification.verified_line_items,
                            "reviewed_by": verification.reviewed_by,
                            "reviewed_at": verification.reviewed_at,
                        }
                        if verification
                        else None
                    ),
                }
            )

        return documents

    def _count(self, statement) -> int:
        return int(self.db.scalar(statement) or 0)
