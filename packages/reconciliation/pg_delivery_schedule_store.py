"""PostgreSQL-backed delivery schedule store.

PgDeliveryScheduleStore implements the same public interface as the SQLite-based
DeliveryScheduleStore using SQLAlchemy ORM models and a PostgreSQL database.

Business rules previously enforced via SQLite triggers are re-implemented here
in Python:
  - SHA-256 uniqueness for evidence documents.
  - Immutability of evidence documents, source lines, source line mappings, and
    quantity allocations (raises ValueError on any attempted mutation after first
    registration).
  - DISPATCH allocations require DELIVERY_CHALLAN evidence.
  - RECEIPT allocations require GRN evidence.
  - DISPATCH allocations cannot reference another dispatch as matched dispatch.
  - Idempotent allocation: identical (type, source, po_item, schedule, qty) →
    return existing; conflicting → raise ValueError.
  - Source line quantity limits span all allocations for that line.
  - Schedule quantity caps for DISPATCH and RECEIPT.
  - Matched dispatch validation for RECEIPT allocations.
  - Schedule quantity limits when creating/amending schedules.
  - Cancelled schedule guard on allocation and amendment.

Usage:
    from sqlalchemy.orm import Session
    from packages.reconciliation.pg_delivery_schedule_store import PgDeliveryScheduleStore

    with session_factory() as session:
        store = PgDeliveryScheduleStore(session)
        item = store.create_po_item(...)
        schedule = store.create_schedule(item.po_item_id, ...)
"""

import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.domain.models import (
    DeliverySchedule,
    DeliveryScheduleStatus,
    DeliveryValidationRun,
    DocumentType,
    EvidenceDocument,
    EvidenceDocumentLine,
    PurchaseOrderItem,
    QuantityAllocation,
    QuantityAllocationType,
    ScheduleAmendment,
    ScheduleAmendmentStatus,
)
from packages.reconciliation.delivery_schedules import DeliveryScheduleReconciliationEngine

from apps.api.schedule_models import (
    PgDeliverySchedule,
    PgDeliveryValidationRun,
    PgEvidenceDocument,
    PgQuantityAllocation,
    PgScheduleAmendment,
    PgSourceDocumentLine,
    PgSourceDocumentLinePoItem,
    SchedulePoItem,
)

# Re-export error types so callers can use either store interchangeably.
from packages.reconciliation.delivery_schedule_store import (
    EvidenceNotFoundError,
    ScheduleNotFoundError,
)


class PgDeliveryScheduleStore:
    """PostgreSQL persistence for PO delivery schedules, allocations, and validation runs.

    Pass a live SQLAlchemy Session.  The session's transaction lifecycle is
    managed externally (the caller commits / rolls back).  All writes within a
    single method call are executed in the caller's active transaction.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._engine = DeliveryScheduleReconciliationEngine()

    # ------------------------------------------------------------------
    # Evidence Documents
    # ------------------------------------------------------------------

    def register_evidence_document(self, document: EvidenceDocument) -> EvidenceDocument:
        """Register an evidence document and its source lines.

        Idempotent: if the identical document (same fingerprint, same lines) has
        already been registered this returns the existing record.  Any attempt
        to change an already-registered document raises ValueError.
        """
        if document.document_type not in (
            DocumentType.DELIVERY_CHALLAN,
            DocumentType.GOODS_RECEIVED_NOTE,
        ):
            raise ValueError("Only Delivery Challan and GRN evidence can be registered.")

        fingerprint = document.sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Evidence document SHA-256 must be 64 hexadecimal characters.")

        document = document.model_copy(update={"sha256": fingerprint})

        line_ids = [line.line_item_id for line in document.lines]
        if len(set(line_ids)) != len(line_ids):
            raise ValueError("Evidence document line IDs must be unique.")
        if any(len(set(line.po_item_ids)) != len(line.po_item_ids) for line in document.lines):
            raise ValueError("Evidence line PO item IDs must be unique.")

        # Validate all referenced PO items belong to this PO reference.
        for line in document.lines:
            for po_item_id in line.po_item_ids:
                po_item = self._get_po_item(po_item_id)
                if po_item.po_reference != document.po_reference:
                    raise ValueError(
                        "Evidence line PO items must belong to the document's PO."
                    )

        # Check if the document header already exists.
        existing_doc = self._session.get(
            PgEvidenceDocument,
            (document.document_type.value, document.document_id),
        )

        if existing_doc is not None:
            # Immutability check on the header.
            if (
                existing_doc.document_number != document.document_number
                or existing_doc.po_reference != document.po_reference
                or existing_doc.sha256 != document.sha256
            ):
                raise ValueError("Registered evidence document identity is immutable.")

            # Immutability check on existing lines.
            existing_lines = self._fetch_pg_source_lines(
                document.document_type.value, document.document_id
            )
            self._assert_lines_immutable(existing_lines, document)
        else:
            # Duplicate fingerprint under a different document ID.
            sha_owner = self._session.scalar(
                select(PgEvidenceDocument.document_id).where(
                    PgEvidenceDocument.sha256 == document.sha256
                )
            )
            if sha_owner is not None:
                raise ValueError(
                    f"This document fingerprint is already registered as {sha_owner}."
                )

            pg_doc = PgEvidenceDocument(
                document_type=document.document_type.value,
                document_id=document.document_id,
                document_number=document.document_number,
                po_reference=document.po_reference,
                sha256=document.sha256,
            )
            self._session.add(pg_doc)
            self._session.flush()  # Ensure PK exists before inserting lines.

        # Upsert each source line and its PO-item mappings.
        for line in document.lines:
            existing_line = self._session.get(
                PgSourceDocumentLine,
                (document.document_type.value, document.document_id, line.line_item_id),
            )
            if existing_line is None:
                pg_line = PgSourceDocumentLine(
                    document_type=document.document_type.value,
                    document_id=document.document_id,
                    line_item_id=line.line_item_id,
                    line_quantity=line.quantity,
                    evidence_date=line.evidence_date,
                )
                self._session.add(pg_line)
                self._session.flush()
            else:
                if (
                    existing_line.line_quantity != line.quantity
                    or existing_line.evidence_date != line.evidence_date
                ):
                    raise ValueError("Registered evidence document lines are immutable.")

            # Existing mappings for this line.
            existing_mapping_ids = {
                row.po_item_id
                for row in self._session.scalars(
                    select(PgSourceDocumentLinePoItem).where(
                        and_(
                            PgSourceDocumentLinePoItem.document_type == document.document_type.value,
                            PgSourceDocumentLinePoItem.document_id == document.document_id,
                            PgSourceDocumentLinePoItem.line_item_id == line.line_item_id,
                        )
                    )
                ).all()
            }
            if existing_mapping_ids and existing_mapping_ids != set(line.po_item_ids):
                raise ValueError("Registered evidence line PO item mappings are immutable.")

            for po_item_id in line.po_item_ids:
                if po_item_id not in existing_mapping_ids:
                    pg_mapping = PgSourceDocumentLinePoItem(
                        document_type=document.document_type.value,
                        document_id=document.document_id,
                        line_item_id=line.line_item_id,
                        po_item_id=po_item_id,
                    )
                    self._session.add(pg_mapping)

        self._session.flush()
        return document

    def get_evidence_document(
        self, document_type: DocumentType, document_id: str
    ) -> EvidenceDocument:
        pg_doc = self._session.get(
            PgEvidenceDocument, (document_type.value, document_id)
        )
        if pg_doc is None:
            raise EvidenceNotFoundError(
                f"Evidence document {document_id} is not registered."
            )
        lines = self._build_evidence_lines(document_type.value, document_id)
        return EvidenceDocument(
            document_id=pg_doc.document_id,
            document_type=pg_doc.document_type,
            document_number=pg_doc.document_number,
            po_reference=pg_doc.po_reference,
            sha256=pg_doc.sha256,
            lines=lines,
        )

    # ------------------------------------------------------------------
    # PO Items
    # ------------------------------------------------------------------

    def create_po_item(
        self,
        po_reference: str,
        line_item_id: str,
        description: str,
        ordered_quantity: Decimal,
        item_code: Optional[str] = None,
        unit: str = "pcs",
        po_item_id: Optional[str] = None,
    ) -> PurchaseOrderItem:
        item_id = po_item_id or str(uuid.uuid4())
        pg_item = SchedulePoItem(
            po_item_id=item_id,
            po_reference=po_reference,
            line_item_id=line_item_id,
            item_code=item_code,
            description=description,
            ordered_quantity=ordered_quantity,
            unit=unit,
        )
        self._session.add(pg_item)
        self._session.flush()
        return PurchaseOrderItem(
            po_item_id=pg_item.po_item_id,
            po_reference=pg_item.po_reference,
            line_item_id=pg_item.line_item_id,
            item_code=pg_item.item_code,
            description=pg_item.description,
            ordered_quantity=Decimal(str(pg_item.ordered_quantity)),
            unit=pg_item.unit,
        )

    def get_po_item(self, po_item_id: str) -> PurchaseOrderItem:
        return self._get_po_item(po_item_id)

    # ------------------------------------------------------------------
    # Delivery Schedules
    # ------------------------------------------------------------------

    def create_schedule(
        self,
        po_item_id: str,
        planned_quantity: Decimal,
        expected_delivery_date: date,
    ) -> DeliverySchedule:
        item = self._get_po_item(po_item_id)
        allocated = self._scheduled_quantity(po_item_id)
        if allocated + planned_quantity > item.ordered_quantity:
            raise ValueError("Scheduled quantity cannot exceed the PO item quantity.")

        pg_schedule = PgDeliverySchedule(
            schedule_id=str(uuid.uuid4()),
            po_item_id=po_item_id,
            planned_quantity=planned_quantity,
            expected_delivery_date=expected_delivery_date,
            status=DeliveryScheduleStatus.SCHEDULED.value,
        )
        self._session.add(pg_schedule)
        self._session.flush()
        return self._domain_schedule(pg_schedule)

    def list_schedules(self, po_item_id: str) -> List[DeliverySchedule]:
        self._get_po_item(po_item_id)  # Raise ScheduleNotFoundError if missing.
        rows = self._session.scalars(
            select(PgDeliverySchedule)
            .where(PgDeliverySchedule.po_item_id == po_item_id)
            .order_by(PgDeliverySchedule.created_at)
        ).all()
        return [self._domain_schedule(row) for row in rows]

    # ------------------------------------------------------------------
    # Amendments
    # ------------------------------------------------------------------

    def propose_amendment(
        self,
        schedule_id: str,
        proposed_quantity: Decimal,
        proposed_expected_date: date,
        requested_by: str,
    ) -> ScheduleAmendment:
        pg_schedule = self._get_pg_schedule(schedule_id)
        if pg_schedule.status == DeliveryScheduleStatus.CANCELLED.value:
            raise ValueError("A cancelled schedule cannot be amended.")

        pg_amendment = PgScheduleAmendment(
            amendment_id=str(uuid.uuid4()),
            schedule_id=schedule_id,
            previous_quantity=pg_schedule.planned_quantity,
            previous_expected_date=pg_schedule.expected_delivery_date,
            proposed_quantity=proposed_quantity,
            proposed_expected_date=proposed_expected_date,
            status=ScheduleAmendmentStatus.PENDING_APPROVAL.value,
            requested_by=requested_by,
            requested_at=datetime.now(timezone.utc),
        )
        self._session.add(pg_amendment)
        self._session.flush()
        return self._domain_amendment(pg_amendment)

    def approve_amendment(self, amendment_id: str, approved_by: str) -> ScheduleAmendment:
        pg_amendment = self._session.scalar(
            select(PgScheduleAmendment).where(
                PgScheduleAmendment.amendment_id == amendment_id
            )
        )
        if pg_amendment is None:
            raise ScheduleNotFoundError(f"Amendment {amendment_id} was not found.")
        if pg_amendment.status != ScheduleAmendmentStatus.PENDING_APPROVAL.value:
            raise ValueError("Only pending schedule amendments can be approved.")

        pg_schedule = self._get_pg_schedule(pg_amendment.schedule_id)
        if pg_schedule.status == DeliveryScheduleStatus.CANCELLED.value:
            raise ValueError("A cancelled schedule cannot be amended.")
        if (
            pg_schedule.planned_quantity != pg_amendment.previous_quantity
            or pg_schedule.expected_delivery_date != pg_amendment.previous_expected_date
        ):
            raise ValueError(
                "This amendment is based on an outdated schedule; submit a new amendment."
            )

        item = self._get_po_item(pg_schedule.po_item_id)
        current_total = self._scheduled_quantity(pg_schedule.po_item_id)
        proposed = Decimal(str(pg_amendment.proposed_quantity))
        previous = Decimal(str(pg_amendment.previous_quantity))
        if current_total - previous + proposed > item.ordered_quantity:
            raise ValueError("Amended schedules cannot exceed the PO item quantity.")

        already_dispatched = self._schedule_allocation_quantity(
            pg_schedule.schedule_id, QuantityAllocationType.DISPATCH
        )
        accepted = self._schedule_receipt_quantity(pg_schedule.schedule_id)
        if max(already_dispatched, accepted) > proposed:
            raise ValueError(
                "An amendment cannot reduce the schedule below quantities already evidenced."
            )

        if accepted >= proposed:
            new_status = DeliveryScheduleStatus.FULFILLED.value
        elif accepted > 0:
            new_status = DeliveryScheduleStatus.PARTIALLY_FULFILLED.value
        else:
            new_status = DeliveryScheduleStatus.SCHEDULED.value

        approved_at = datetime.now(timezone.utc)
        pg_schedule.planned_quantity = proposed
        pg_schedule.expected_delivery_date = pg_amendment.proposed_expected_date
        pg_schedule.status = new_status

        pg_amendment.status = ScheduleAmendmentStatus.APPROVED.value
        pg_amendment.approved_by = approved_by
        pg_amendment.approved_at = approved_at
        self._session.flush()

        return self._domain_amendment(pg_amendment)

    # ------------------------------------------------------------------
    # Quantity Allocations
    # ------------------------------------------------------------------

    def allocate(self, allocation: QuantityAllocation) -> QuantityAllocation:
        expected_document_type = (
            DocumentType.DELIVERY_CHALLAN
            if allocation.allocation_type == QuantityAllocationType.DISPATCH
            else DocumentType.GOODS_RECEIVED_NOTE
        )
        if allocation.source_document_type != expected_document_type:
            raise ValueError(
                f"{allocation.allocation_type.value} allocations require "
                f"{expected_document_type.value} evidence."
            )
        if (
            allocation.allocation_type == QuantityAllocationType.DISPATCH
            and allocation.matched_dispatch_allocation_id
        ):
            raise ValueError(
                "Dispatch allocations cannot link to another dispatch allocation."
            )

        # Verify po_item exists.
        po_item = self._get_po_item(allocation.po_item_id)

        # Look up evidence with source line + PO-item mapping.
        evidence_row = self._session.execute(
            select(
                PgEvidenceDocument.po_reference,
                PgSourceDocumentLine.line_quantity,
                PgSourceDocumentLine.evidence_date,
                PgSourceDocumentLinePoItem.po_item_id,
            )
            .join(
                PgSourceDocumentLine,
                and_(
                    PgSourceDocumentLine.document_type == PgEvidenceDocument.document_type,
                    PgSourceDocumentLine.document_id == PgEvidenceDocument.document_id,
                ),
            )
            .join(
                PgSourceDocumentLinePoItem,
                and_(
                    PgSourceDocumentLinePoItem.document_type == PgSourceDocumentLine.document_type,
                    PgSourceDocumentLinePoItem.document_id == PgSourceDocumentLine.document_id,
                    PgSourceDocumentLinePoItem.line_item_id == PgSourceDocumentLine.line_item_id,
                ),
            )
            .where(
                and_(
                    PgEvidenceDocument.document_type == allocation.source_document_type.value,
                    PgEvidenceDocument.document_id == allocation.source_document_id,
                    PgSourceDocumentLine.line_item_id == allocation.source_line_item_id,
                    PgSourceDocumentLinePoItem.po_item_id == allocation.po_item_id,
                )
            )
        ).first()

        if evidence_row is None:
            self._raise_missing_evidence_error(allocation)

        if evidence_row.po_reference != po_item.po_reference:
            raise ValueError("Evidence document belongs to a different purchase order.")

        if (
            Decimal(str(evidence_row.line_quantity)) != allocation.source_line_quantity
            or evidence_row.evidence_date != allocation.evidence_date
        ):
            raise ValueError(
                "Allocation quantity and date must match registered evidence."
            )

        # Validate schedule membership and status.
        if allocation.schedule_id:
            pg_schedule = self._session.scalar(
                select(PgDeliverySchedule).where(
                    and_(
                        PgDeliverySchedule.schedule_id == allocation.schedule_id,
                        PgDeliverySchedule.po_item_id == allocation.po_item_id,
                    )
                )
            )
            if pg_schedule is None:
                raise ValueError(
                    "The delivery schedule does not belong to this PO item."
                )
            if pg_schedule.status == DeliveryScheduleStatus.CANCELLED.value:
                raise ValueError("Allocations cannot be made to a cancelled schedule.")

        # Idempotency: check for an existing identical allocation.
        duplicate = self._find_duplicate_allocation(allocation)
        if duplicate is not None:
            if (
                Decimal(str(duplicate.quantity)) == allocation.quantity
                and duplicate.matched_dispatch_allocation_id
                == allocation.matched_dispatch_allocation_id
            ):
                return self._domain_allocation(duplicate)
            raise ValueError(
                "This source line already has a different allocation to that PO item and schedule."
            )

        # Validate matched dispatch (RECEIPT only).
        if allocation.matched_dispatch_allocation_id:
            self._validate_matched_dispatch(allocation)

        # Source line total cap.
        line_allocated = self._source_line_allocated_quantity(
            allocation.source_document_type.value,
            allocation.source_document_id,
            allocation.source_line_item_id,
        )
        if line_allocated + allocation.quantity > allocation.source_line_quantity:
            raise ValueError(
                "Allocations cannot exceed the source document line quantity."
            )

        # Schedule cap (per allocation type).
        if allocation.schedule_id:
            sched_allocated = self._schedule_allocation_quantity(
                allocation.schedule_id, allocation.allocation_type
            )
            pg_schedule = self._session.get(
                PgDeliverySchedule, allocation.schedule_id
            )
            if sched_allocated + allocation.quantity > Decimal(
                str(pg_schedule.planned_quantity)
            ):
                raise ValueError(
                    "Scheduled allocations cannot exceed the planned schedule quantity; "
                    "allocate the remaining evidence as unscheduled."
                )

        pg_alloc = PgQuantityAllocation(
            allocation_id=allocation.allocation_id,
            allocation_type=allocation.allocation_type.value,
            source_document_type=allocation.source_document_type.value,
            source_document_id=allocation.source_document_id,
            source_line_item_id=allocation.source_line_item_id,
            po_item_id=allocation.po_item_id,
            schedule_id=allocation.schedule_id,
            quantity=allocation.quantity,
            matched_dispatch_allocation_id=allocation.matched_dispatch_allocation_id,
        )
        self._session.add(pg_alloc)

        # Update schedule status after RECEIPT allocation.
        if (
            allocation.allocation_type == QuantityAllocationType.RECEIPT
            and allocation.schedule_id
        ):
            self._session.flush()  # So new allocation is visible to the query below.
            pg_sched = self._session.get(PgDeliverySchedule, allocation.schedule_id)
            accepted = self._schedule_receipt_quantity(allocation.schedule_id)
            planned = Decimal(str(pg_sched.planned_quantity))
            if accepted >= planned:
                pg_sched.status = DeliveryScheduleStatus.FULFILLED.value
            else:
                pg_sched.status = DeliveryScheduleStatus.PARTIALLY_FULFILLED.value

        self._session.flush()
        return allocation

    def list_allocations(self, po_item_id: str) -> List[QuantityAllocation]:
        self._get_po_item(po_item_id)
        rows = self._session.scalars(
            select(PgQuantityAllocation)
            .where(PgQuantityAllocation.po_item_id == po_item_id)
            .order_by(PgQuantityAllocation.created_at)
        ).all()
        return [self._domain_allocation(row) for row in rows]

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def run_reconciliation(
        self, po_item_id: str, evaluation_date: date
    ) -> DeliveryValidationRun:
        item = self._get_po_item(po_item_id)
        schedules = self.list_schedules(po_item_id)
        allocations = self.list_allocations(po_item_id)

        summary, findings = self._engine.reconcile(
            item, schedules, allocations, evaluation_date
        )
        run = DeliveryValidationRun(
            run_id=str(uuid.uuid4()),
            po_item_id=po_item_id,
            evaluation_date=evaluation_date,
            summary=summary,
            findings=findings,
        )
        pg_run = PgDeliveryValidationRun(
            run_id=run.run_id,
            po_item_id=po_item_id,
            evaluation_date=evaluation_date,
            created_at=run.created_at,
            result_json=run.model_dump(mode="json"),
        )
        self._session.add(pg_run)
        self._session.flush()
        return run

    def list_validation_runs(self, po_item_id: str) -> List[DeliveryValidationRun]:
        self._get_po_item(po_item_id)
        rows = self._session.scalars(
            select(PgDeliveryValidationRun)
            .where(PgDeliveryValidationRun.po_item_id == po_item_id)
            .order_by(PgDeliveryValidationRun.created_at)
        ).all()
        return [
            DeliveryValidationRun.model_validate(row.result_json) for row in rows
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_po_item(self, po_item_id: str) -> PurchaseOrderItem:
        pg_item = self._session.get(SchedulePoItem, po_item_id)
        if pg_item is None:
            raise ScheduleNotFoundError(f"PO item {po_item_id} was not found.")
        return PurchaseOrderItem(
            po_item_id=pg_item.po_item_id,
            po_reference=pg_item.po_reference,
            line_item_id=pg_item.line_item_id,
            item_code=pg_item.item_code,
            description=pg_item.description,
            ordered_quantity=Decimal(str(pg_item.ordered_quantity)),
            unit=pg_item.unit,
        )

    def _get_pg_schedule(self, schedule_id: str) -> PgDeliverySchedule:
        row = self._session.get(PgDeliverySchedule, schedule_id)
        if row is None:
            raise ScheduleNotFoundError(f"Schedule {schedule_id} was not found.")
        return row

    def _scheduled_quantity(self, po_item_id: str) -> Decimal:
        """Sum of active (non-cancelled) planned quantities for a PO item."""
        result = self._session.scalar(
            select(func.coalesce(func.sum(PgDeliverySchedule.planned_quantity), 0))
            .where(
                and_(
                    PgDeliverySchedule.po_item_id == po_item_id,
                    PgDeliverySchedule.status != DeliveryScheduleStatus.CANCELLED.value,
                )
            )
        )
        return Decimal(str(result))

    def _schedule_allocation_quantity(
        self, schedule_id: str, allocation_type: QuantityAllocationType
    ) -> Decimal:
        result = self._session.scalar(
            select(func.coalesce(func.sum(PgQuantityAllocation.quantity), 0))
            .where(
                and_(
                    PgQuantityAllocation.schedule_id == schedule_id,
                    PgQuantityAllocation.allocation_type == allocation_type.value,
                )
            )
        )
        return Decimal(str(result))

    def _schedule_receipt_quantity(self, schedule_id: str) -> Decimal:
        return self._schedule_allocation_quantity(
            schedule_id, QuantityAllocationType.RECEIPT
        )

    def _source_line_allocated_quantity(
        self,
        source_document_type: str,
        source_document_id: str,
        source_line_item_id: str,
    ) -> Decimal:
        result = self._session.scalar(
            select(func.coalesce(func.sum(PgQuantityAllocation.quantity), 0))
            .where(
                and_(
                    PgQuantityAllocation.source_document_type == source_document_type,
                    PgQuantityAllocation.source_document_id == source_document_id,
                    PgQuantityAllocation.source_line_item_id == source_line_item_id,
                )
            )
        )
        return Decimal(str(result))

    def _find_duplicate_allocation(
        self, allocation: QuantityAllocation
    ) -> Optional[PgQuantityAllocation]:
        """Find an existing allocation with the same logical key."""
        stmt = select(PgQuantityAllocation).where(
            and_(
                PgQuantityAllocation.allocation_type == allocation.allocation_type.value,
                PgQuantityAllocation.source_document_type
                == allocation.source_document_type.value,
                PgQuantityAllocation.source_document_id == allocation.source_document_id,
                PgQuantityAllocation.source_line_item_id
                == allocation.source_line_item_id,
                PgQuantityAllocation.po_item_id == allocation.po_item_id,
            )
        )
        # schedule_id nullable comparison
        if allocation.schedule_id is None:
            stmt = stmt.where(PgQuantityAllocation.schedule_id.is_(None))
        else:
            stmt = stmt.where(
                PgQuantityAllocation.schedule_id == allocation.schedule_id
            )
        return self._session.scalar(stmt)

    def _validate_matched_dispatch(self, allocation: QuantityAllocation) -> None:
        """Validate the matched dispatch allocation for a RECEIPT."""
        dispatch = self._session.scalar(
            select(PgQuantityAllocation).where(
                PgQuantityAllocation.allocation_id
                == allocation.matched_dispatch_allocation_id
            )
        )
        if (
            dispatch is None
            or dispatch.allocation_type != QuantityAllocationType.DISPATCH.value
            or dispatch.po_item_id != allocation.po_item_id
            or dispatch.schedule_id != allocation.schedule_id
        ):
            raise ValueError(
                "A receipt must link to a dispatch allocation for the same PO item and schedule."
            )

        # Dispatch evidence date must precede receipt evidence date.
        dispatch_line = self._session.get(
            PgSourceDocumentLine,
            (
                dispatch.source_document_type,
                dispatch.source_document_id,
                dispatch.source_line_item_id,
            ),
        )
        if dispatch_line and dispatch_line.evidence_date > allocation.evidence_date:
            raise ValueError("A receipt cannot predate its matched dispatch.")

        # Matched receipts cannot exceed dispatch quantity.
        already_matched = self._session.scalar(
            select(
                func.coalesce(func.sum(PgQuantityAllocation.quantity), 0)
            ).where(
                PgQuantityAllocation.matched_dispatch_allocation_id
                == allocation.matched_dispatch_allocation_id
            )
        )
        if Decimal(str(already_matched)) + allocation.quantity > Decimal(
            str(dispatch.quantity)
        ):
            raise ValueError(
                "Matched receipts cannot exceed the dispatch allocation quantity."
            )

    def _raise_missing_evidence_error(self, allocation: QuantityAllocation) -> None:
        """Raise an informative error when evidence is not found."""
        doc_exists = self._session.scalar(
            select(PgEvidenceDocument.document_id).where(
                and_(
                    PgEvidenceDocument.document_type
                    == allocation.source_document_type.value,
                    PgEvidenceDocument.document_id == allocation.source_document_id,
                )
            )
        )
        if doc_exists is None:
            raise EvidenceNotFoundError(
                f"Evidence document {allocation.source_document_id} is not registered."
            )
        line_exists = self._session.scalar(
            select(PgSourceDocumentLine.line_item_id).where(
                and_(
                    PgSourceDocumentLine.document_type
                    == allocation.source_document_type.value,
                    PgSourceDocumentLine.document_id == allocation.source_document_id,
                    PgSourceDocumentLine.line_item_id == allocation.source_line_item_id,
                )
            )
        )
        if line_exists is None:
            raise EvidenceNotFoundError(
                f"Evidence line {allocation.source_line_item_id} is not registered on "
                f"document {allocation.source_document_id}."
            )
        raise ValueError("Evidence line is not mapped to this PO item.")

    def _fetch_pg_source_lines(
        self, document_type: str, document_id: str
    ) -> List[PgSourceDocumentLine]:
        return list(
            self._session.scalars(
                select(PgSourceDocumentLine).where(
                    and_(
                        PgSourceDocumentLine.document_type == document_type,
                        PgSourceDocumentLine.document_id == document_id,
                    )
                )
            ).all()
        )

    def _assert_lines_immutable(
        self,
        existing_lines: List[PgSourceDocumentLine],
        document: EvidenceDocument,
    ) -> None:
        """Verify that existing registered lines match the submitted document lines."""
        existing_map = {
            line.line_item_id: line for line in existing_lines
        }
        new_map = {line.line_item_id: line for line in document.lines}
        if set(existing_map) != set(new_map):
            raise ValueError("Registered evidence document lines are immutable.")
        for lid, existing_line in existing_map.items():
            new_line = new_map[lid]
            if (
                existing_line.line_quantity != new_line.quantity
                or existing_line.evidence_date != new_line.evidence_date
            ):
                raise ValueError("Registered evidence document lines are immutable.")

    def _build_evidence_lines(
        self, document_type: str, document_id: str
    ) -> List[EvidenceDocumentLine]:
        pg_lines = self._session.scalars(
            select(PgSourceDocumentLine)
            .where(
                and_(
                    PgSourceDocumentLine.document_type == document_type,
                    PgSourceDocumentLine.document_id == document_id,
                )
            )
            .order_by(PgSourceDocumentLine.line_item_id)
        ).all()
        lines = []
        for pg_line in pg_lines:
            mappings = list(
                self._session.scalars(
                    select(PgSourceDocumentLinePoItem.po_item_id).where(
                        and_(
                            PgSourceDocumentLinePoItem.document_type == document_type,
                            PgSourceDocumentLinePoItem.document_id == document_id,
                            PgSourceDocumentLinePoItem.line_item_id == pg_line.line_item_id,
                        )
                    ).order_by(PgSourceDocumentLinePoItem.po_item_id)
                ).all()
            )
            lines.append(
                EvidenceDocumentLine(
                    line_item_id=pg_line.line_item_id,
                    quantity=Decimal(str(pg_line.line_quantity)),
                    evidence_date=pg_line.evidence_date,
                    po_item_ids=mappings,
                )
            )
        return lines

    def _domain_schedule(self, row: PgDeliverySchedule) -> DeliverySchedule:
        amendments = self._session.scalars(
            select(PgScheduleAmendment)
            .where(PgScheduleAmendment.schedule_id == row.schedule_id)
            .order_by(PgScheduleAmendment.requested_at)
        ).all()
        return DeliverySchedule(
            schedule_id=row.schedule_id,
            po_item_id=row.po_item_id,
            planned_quantity=Decimal(str(row.planned_quantity)),
            expected_delivery_date=row.expected_delivery_date,
            status=row.status,
            amendment_history=[self._domain_amendment(a) for a in amendments],
        )

    @staticmethod
    def _domain_amendment(row: PgScheduleAmendment) -> ScheduleAmendment:
        return ScheduleAmendment(
            amendment_id=row.amendment_id,
            schedule_id=row.schedule_id,
            previous_quantity=Decimal(str(row.previous_quantity)),
            previous_expected_date=row.previous_expected_date,
            proposed_quantity=Decimal(str(row.proposed_quantity)),
            proposed_expected_date=row.proposed_expected_date,
            status=row.status,
            requested_by=row.requested_by,
            requested_at=row.requested_at,
            approved_by=row.approved_by,
            approved_at=row.approved_at,
        )

    def _domain_allocation(self, row: PgQuantityAllocation) -> QuantityAllocation:
        source_line = self._session.get(
            PgSourceDocumentLine,
            (row.source_document_type, row.source_document_id, row.source_line_item_id),
        )
        return QuantityAllocation(
            allocation_id=row.allocation_id,
            allocation_type=row.allocation_type,
            source_document_type=row.source_document_type,
            source_document_id=row.source_document_id,
            source_line_item_id=row.source_line_item_id,
            source_line_quantity=Decimal(str(source_line.line_quantity)),
            evidence_date=source_line.evidence_date,
            po_item_id=row.po_item_id,
            schedule_id=row.schedule_id,
            quantity=Decimal(str(row.quantity)),
            matched_dispatch_allocation_id=row.matched_dispatch_allocation_id,
        )
