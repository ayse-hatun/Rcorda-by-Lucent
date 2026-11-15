"""PostgreSQL integration tests for PgDeliveryScheduleStore.

These tests require a live PostgreSQL database (configured via DATABASE_URL).
They use unique test-data identifiers to avoid colliding with existing project
data, and clean up after themselves.

Run from ayse-hatun-super-sniffle/:
    python -m pytest tests/test_pg_schedule_store.py -v
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.api.database import get_engine, get_session_factory
from apps.api.schedule_models import (
    PgDeliveryValidationRun,
    PgEvidenceDocument,
    PgQuantityAllocation,
    PgScheduleAmendment,
    PgDeliverySchedule,
    SchedulePoItem,
)
from packages.domain.models import (
    DocumentType,
    EvidenceDocument,
    EvidenceDocumentLine,
    QuantityAllocation,
    QuantityAllocationType,
)
from packages.reconciliation.pg_delivery_schedule_store import (
    EvidenceNotFoundError,
    PgDeliveryScheduleStore,
    ScheduleNotFoundError,
)


# ---------------------------------------------------------------------------
# Shared session fixture with rollback for test isolation.
# Each test runs inside a savepoint; rollback restores state.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_session_factory():
    """Module-scoped session factory bound to the shared PostgreSQL engine."""
    engine = get_engine()
    return get_session_factory(engine)


@pytest.fixture()
def session(pg_session_factory):
    """Test-scoped session that rolls back all changes after each test."""
    session = pg_session_factory()
    session.begin_nested()  # SAVEPOINT
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def store(session):
    return PgDeliveryScheduleStore(session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    """Short unique suffix for test data."""
    return uuid.uuid4().hex[:12]


def _make_evidence_doc(
    po_item_id: str,
    po_reference: str,
    doc_type: DocumentType = DocumentType.DELIVERY_CHALLAN,
    quantity: Decimal = Decimal("100"),
    evidence_date: date = date(2024, 6, 1),
) -> EvidenceDocument:
    uid = _uid()
    sha256 = uuid.uuid4().hex * 2  # 64-hex chars
    return EvidenceDocument(
        document_id=f"doc-{uid}",
        document_type=doc_type,
        document_number=f"NUM-{uid}",
        po_reference=po_reference,
        sha256=sha256,
        lines=[
            EvidenceDocumentLine(
                line_item_id=f"line-{uid}",
                quantity=quantity,
                evidence_date=evidence_date,
                po_item_ids=[po_item_id],
            )
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPgPersistenceAcrossSessions:
    """Data written in one session must be readable in a new session."""

    def test_pg_persistence_across_sessions(self, pg_session_factory):
        """Create a PO item + schedule in one session; read them back in another."""
        uid = _uid()
        po_ref = f"PO-PERSIST-{uid}"
        item_id = f"item-persist-{uid}"

        # Session 1: write.
        session1 = pg_session_factory()
        try:
            store1 = PgDeliveryScheduleStore(session1)
            item = store1.create_po_item(
                po_reference=po_ref,
                line_item_id=f"li-{uid}",
                description="Persistence test item",
                ordered_quantity=Decimal("500"),
                po_item_id=item_id,
            )
            store1.create_schedule(
                po_item_id=item_id,
                planned_quantity=Decimal("200"),
                expected_delivery_date=date(2024, 9, 1),
            )
            session1.commit()
        finally:
            session1.close()

        # Session 2: read.
        session2 = pg_session_factory()
        try:
            store2 = PgDeliveryScheduleStore(session2)
            read_item = store2.get_po_item(item_id)
            assert read_item.po_reference == po_ref
            assert read_item.ordered_quantity == Decimal("500")

            schedules = store2.list_schedules(item_id)
            assert len(schedules) == 1
            assert schedules[0].planned_quantity == Decimal("200")
        finally:
            # Clean up.
            session2.query(SchedulePoItem).filter(
                SchedulePoItem.po_item_id == item_id
            ).delete(synchronize_session=False)
            session2.commit()
            session2.close()


class TestMultipleSchedulesPerPoItem:
    def test_multiple_schedules_per_po_item(self, store, session):
        uid = _uid()
        item = store.create_po_item(
            po_reference=f"PO-MULTI-{uid}",
            line_item_id=f"li-{uid}",
            description="Multi-schedule item",
            ordered_quantity=Decimal("900"),
        )
        s1 = store.create_schedule(item.po_item_id, Decimal("300"), date(2024, 7, 1))
        s2 = store.create_schedule(item.po_item_id, Decimal("300"), date(2024, 8, 1))
        s3 = store.create_schedule(item.po_item_id, Decimal("300"), date(2024, 9, 1))
        session.flush()

        schedules = store.list_schedules(item.po_item_id)
        assert len(schedules) == 3
        ids = {s.schedule_id for s in schedules}
        assert s1.schedule_id in ids
        assert s2.schedule_id in ids
        assert s3.schedule_id in ids
        total = sum(s.planned_quantity for s in schedules)
        assert total == Decimal("900")


class TestAmendmentHistoryPreserved:
    def test_amendment_history_preserved(self, store, session):
        uid = _uid()
        item = store.create_po_item(
            po_reference=f"PO-AMEND-{uid}",
            line_item_id=f"li-{uid}",
            description="Amendment test",
            ordered_quantity=Decimal("500"),
        )
        schedule = store.create_schedule(
            item.po_item_id, Decimal("300"), date(2024, 8, 1)
        )
        # Previous values before amendment.
        orig_qty = schedule.planned_quantity
        orig_date = schedule.expected_delivery_date

        amendment = store.propose_amendment(
            schedule_id=schedule.schedule_id,
            proposed_quantity=Decimal("250"),
            proposed_expected_date=date(2024, 8, 15),
            requested_by="planner@example.com",
        )
        assert amendment.previous_quantity == orig_qty
        assert amendment.previous_expected_date == orig_date
        assert amendment.proposed_quantity == Decimal("250")

        approved = store.approve_amendment(amendment.amendment_id, "manager@example.com")
        assert approved.approved_by == "manager@example.com"

        # Reload schedules — amendment history must be preserved.
        schedules = store.list_schedules(item.po_item_id)
        assert len(schedules) == 1
        updated = schedules[0]
        assert updated.planned_quantity == Decimal("250")
        assert len(updated.amendment_history) == 1
        hist = updated.amendment_history[0]
        assert hist.previous_quantity == orig_qty
        assert hist.approved_by == "manager@example.com"


class TestDispatchAndReceiptAllocations:
    def test_dispatch_and_receipt_allocations(self, store, session):
        uid = _uid()
        po_ref = f"PO-ALLOC-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Allocation test",
            ordered_quantity=Decimal("200"),
        )
        schedule = store.create_schedule(
            item.po_item_id, Decimal("200"), date(2024, 9, 1)
        )

        # Register delivery challan.
        challan = _make_evidence_doc(
            item.po_item_id,
            po_ref,
            doc_type=DocumentType.DELIVERY_CHALLAN,
            quantity=Decimal("200"),
            evidence_date=date(2024, 8, 20),
        )
        store.register_evidence_document(challan)

        # Register GRN.
        grn = _make_evidence_doc(
            item.po_item_id,
            po_ref,
            doc_type=DocumentType.GOODS_RECEIVED_NOTE,
            quantity=Decimal("200"),
            evidence_date=date(2024, 8, 25),
        )
        store.register_evidence_document(grn)

        dispatch_id = uuid.uuid4().hex
        dispatch_alloc = store.allocate(
            QuantityAllocation(
                allocation_id=dispatch_id,
                allocation_type=QuantityAllocationType.DISPATCH,
                source_document_type=DocumentType.DELIVERY_CHALLAN,
                source_document_id=challan.document_id,
                source_line_item_id=challan.lines[0].line_item_id,
                source_line_quantity=Decimal("200"),
                evidence_date=date(2024, 8, 20),
                po_item_id=item.po_item_id,
                schedule_id=schedule.schedule_id,
                quantity=Decimal("200"),
            )
        )

        receipt_alloc = store.allocate(
            QuantityAllocation(
                allocation_id=uuid.uuid4().hex,
                allocation_type=QuantityAllocationType.RECEIPT,
                source_document_type=DocumentType.GOODS_RECEIVED_NOTE,
                source_document_id=grn.document_id,
                source_line_item_id=grn.lines[0].line_item_id,
                source_line_quantity=Decimal("200"),
                evidence_date=date(2024, 8, 25),
                po_item_id=item.po_item_id,
                schedule_id=schedule.schedule_id,
                quantity=Decimal("200"),
                matched_dispatch_allocation_id=dispatch_alloc.allocation_id,
            )
        )

        allocations = store.list_allocations(item.po_item_id)
        assert len(allocations) == 2
        types = {a.allocation_type for a in allocations}
        assert QuantityAllocationType.DISPATCH in types
        assert QuantityAllocationType.RECEIPT in types

        # Schedule should be FULFILLED.
        schedules = store.list_schedules(item.po_item_id)
        assert schedules[0].status.value == "FULFILLED"


class TestInvalidAllocationRejected:
    def test_wrong_doc_type_for_dispatch(self, store, session):
        """DISPATCH with GRN → rejected."""
        uid = _uid()
        po_ref = f"PO-INV-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Invalid alloc",
            ordered_quantity=Decimal("100"),
        )
        # Register GRN (wrong type for DISPATCH).
        grn = _make_evidence_doc(
            item.po_item_id, po_ref, doc_type=DocumentType.GOODS_RECEIVED_NOTE
        )
        store.register_evidence_document(grn)

        with pytest.raises(ValueError, match="DISPATCH allocations require DELIVERY_CHALLAN"):
            store.allocate(
                QuantityAllocation(
                    allocation_id=uuid.uuid4().hex,
                    allocation_type=QuantityAllocationType.DISPATCH,
                    source_document_type=DocumentType.GOODS_RECEIVED_NOTE,
                    source_document_id=grn.document_id,
                    source_line_item_id=grn.lines[0].line_item_id,
                    source_line_quantity=Decimal("100"),
                    evidence_date=date(2024, 6, 1),
                    po_item_id=item.po_item_id,
                    quantity=Decimal("100"),
                )
            )

    def test_unregistered_source_document_rejected(self, store, session):
        uid = _uid()
        item = store.create_po_item(
            po_reference=f"PO-UNREG-{uid}",
            line_item_id=f"li-{uid}",
            description="Unregistered doc",
            ordered_quantity=Decimal("100"),
        )
        with pytest.raises(EvidenceNotFoundError):
            store.allocate(
                QuantityAllocation(
                    allocation_id=uuid.uuid4().hex,
                    allocation_type=QuantityAllocationType.DISPATCH,
                    source_document_type=DocumentType.DELIVERY_CHALLAN,
                    source_document_id=f"GHOST-{uid}",
                    source_line_item_id=f"line-{uid}",
                    source_line_quantity=Decimal("100"),
                    evidence_date=date(2024, 6, 1),
                    po_item_id=item.po_item_id,
                    quantity=Decimal("100"),
                )
            )

    def test_over_source_quantity_rejected(self, store, session):
        uid = _uid()
        po_ref = f"PO-OVER-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Over-alloc test",
            ordered_quantity=Decimal("100"),
        )
        challan = _make_evidence_doc(
            item.po_item_id, po_ref, quantity=Decimal("100"), evidence_date=date(2024, 6, 1)
        )
        store.register_evidence_document(challan)

        with pytest.raises(ValueError, match="source document line quantity"):
            store.allocate(
                QuantityAllocation(
                    allocation_id=uuid.uuid4().hex,
                    allocation_type=QuantityAllocationType.DISPATCH,
                    source_document_type=DocumentType.DELIVERY_CHALLAN,
                    source_document_id=challan.document_id,
                    source_line_item_id=challan.lines[0].line_item_id,
                    source_line_quantity=Decimal("100"),
                    evidence_date=date(2024, 6, 1),
                    po_item_id=item.po_item_id,
                    quantity=Decimal("150"),  # > 100
                )
            )


class TestDuplicateEvidenceDocumentRejected:
    def test_duplicate_sha256_rejected(self, store, session):
        uid = _uid()
        po_ref = f"PO-DUP-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Duplicate sha test",
            ordered_quantity=Decimal("100"),
        )
        sha = uuid.uuid4().hex * 2
        doc1 = EvidenceDocument(
            document_id=f"doc-first-{uid}",
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_number=f"NUM-first-{uid}",
            po_reference=po_ref,
            sha256=sha,
            lines=[
                EvidenceDocumentLine(
                    line_item_id=f"line-{uid}",
                    quantity=Decimal("50"),
                    evidence_date=date(2024, 6, 1),
                    po_item_ids=[item.po_item_id],
                )
            ],
        )
        store.register_evidence_document(doc1)

        doc2 = EvidenceDocument(
            document_id=f"doc-second-{uid}",
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_number=f"NUM-second-{uid}",
            po_reference=po_ref,
            sha256=sha,  # Same fingerprint!
            lines=[
                EvidenceDocumentLine(
                    line_item_id=f"line2-{uid}",
                    quantity=Decimal("50"),
                    evidence_date=date(2024, 6, 2),
                    po_item_ids=[item.po_item_id],
                )
            ],
        )
        with pytest.raises(ValueError, match="fingerprint is already registered"):
            store.register_evidence_document(doc2)


class TestIdempotentAllocation:
    def test_identical_allocation_returns_existing(self, store, session):
        uid = _uid()
        po_ref = f"PO-IDEMP-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Idempotency test",
            ordered_quantity=Decimal("200"),
        )
        challan = _make_evidence_doc(
            item.po_item_id, po_ref, quantity=Decimal("100"), evidence_date=date(2024, 6, 1)
        )
        store.register_evidence_document(challan)

        alloc_kwargs = dict(
            allocation_id=uuid.uuid4().hex,
            allocation_type=QuantityAllocationType.DISPATCH,
            source_document_type=DocumentType.DELIVERY_CHALLAN,
            source_document_id=challan.document_id,
            source_line_item_id=challan.lines[0].line_item_id,
            source_line_quantity=Decimal("100"),
            evidence_date=date(2024, 6, 1),
            po_item_id=item.po_item_id,
            quantity=Decimal("100"),
        )
        first = store.allocate(QuantityAllocation(**alloc_kwargs))
        # Submit identical allocation — must return the existing, not error.
        second = store.allocate(QuantityAllocation(**alloc_kwargs))

        assert first.allocation_id == second.allocation_id
        # Only one record should exist.
        allocations = store.list_allocations(item.po_item_id)
        assert len(allocations) == 1


class TestValidationRunSnapshot:
    def test_validation_run_creates_immutable_snapshot(self, store, session):
        uid = _uid()
        po_ref = f"PO-SNAP-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Snapshot test",
            ordered_quantity=Decimal("300"),
        )
        store.create_schedule(item.po_item_id, Decimal("300"), date(2024, 8, 1))

        run = store.run_reconciliation(item.po_item_id, date(2024, 7, 1))
        assert run.run_id is not None
        assert run.po_item_id == item.po_item_id
        assert run.summary.ordered_quantity == Decimal("300")

        # Retrieve runs — snapshot must be persisted.
        runs = store.list_validation_runs(item.po_item_id)
        assert len(runs) == 1
        retrieved = runs[0]
        assert retrieved.run_id == run.run_id
        assert retrieved.summary.ordered_quantity == Decimal("300")


class TestScheduleQuantityCapEnforced:
    def test_schedule_quantity_cannot_exceed_ordered(self, store, session):
        uid = _uid()
        item = store.create_po_item(
            po_reference=f"PO-CAP-{uid}",
            line_item_id=f"li-{uid}",
            description="Cap test",
            ordered_quantity=Decimal("100"),
        )
        store.create_schedule(item.po_item_id, Decimal("80"), date(2024, 9, 1))
        with pytest.raises(ValueError, match="cannot exceed the PO item quantity"):
            store.create_schedule(item.po_item_id, Decimal("30"), date(2024, 10, 1))


class TestReceiptRequiresGrn:
    def test_receipt_allocation_requires_grn(self, store, session):
        uid = _uid()
        po_ref = f"PO-GRN-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="GRN test",
            ordered_quantity=Decimal("100"),
        )
        # Register a Delivery Challan — wrong for RECEIPT.
        challan = _make_evidence_doc(
            item.po_item_id, po_ref, doc_type=DocumentType.DELIVERY_CHALLAN
        )
        store.register_evidence_document(challan)

        with pytest.raises(ValueError, match="RECEIPT allocations require GRN evidence"):
            store.allocate(
                QuantityAllocation(
                    allocation_id=uuid.uuid4().hex,
                    allocation_type=QuantityAllocationType.RECEIPT,
                    source_document_type=DocumentType.DELIVERY_CHALLAN,
                    source_document_id=challan.document_id,
                    source_line_item_id=challan.lines[0].line_item_id,
                    source_line_quantity=Decimal("100"),
                    evidence_date=date(2024, 6, 1),
                    po_item_id=item.po_item_id,
                    quantity=Decimal("100"),
                )
            )


class TestAmendmentCannotReduceBelowDispatched:
    def test_amendment_cannot_reduce_below_dispatched(self, store, session):
        uid = _uid()
        po_ref = f"PO-BELOW-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Reduce below dispatched",
            ordered_quantity=Decimal("200"),
        )
        schedule = store.create_schedule(item.po_item_id, Decimal("200"), date(2024, 9, 1))

        challan = _make_evidence_doc(
            item.po_item_id, po_ref, quantity=Decimal("150"), evidence_date=date(2024, 8, 1)
        )
        store.register_evidence_document(challan)
        store.allocate(
            QuantityAllocation(
                allocation_id=uuid.uuid4().hex,
                allocation_type=QuantityAllocationType.DISPATCH,
                source_document_type=DocumentType.DELIVERY_CHALLAN,
                source_document_id=challan.document_id,
                source_line_item_id=challan.lines[0].line_item_id,
                source_line_quantity=Decimal("150"),
                evidence_date=date(2024, 8, 1),
                po_item_id=item.po_item_id,
                schedule_id=schedule.schedule_id,
                quantity=Decimal("150"),
            )
        )
        amendment = store.propose_amendment(
            schedule_id=schedule.schedule_id,
            proposed_quantity=Decimal("100"),  # Below 150 already dispatched
            proposed_expected_date=date(2024, 9, 15),
            requested_by="planner@example.com",
        )
        with pytest.raises(ValueError, match="cannot reduce the schedule below quantities"):
            store.approve_amendment(amendment.amendment_id, "manager@example.com")


class TestMatchedReceiptCannotExceedDispatch:
    def test_matched_receipts_cannot_overallocate_dispatch(self, store, session):
        uid = _uid()
        po_ref = f"PO-OVERR-{uid}"
        item = store.create_po_item(
            po_reference=po_ref,
            line_item_id=f"li-{uid}",
            description="Over receipt",
            ordered_quantity=Decimal("200"),
        )
        challan = _make_evidence_doc(
            item.po_item_id, po_ref, quantity=Decimal("100"), evidence_date=date(2024, 8, 1)
        )
        store.register_evidence_document(challan)
        grn = _make_evidence_doc(
            item.po_item_id, po_ref,
            doc_type=DocumentType.GOODS_RECEIVED_NOTE,
            quantity=Decimal("200"),
            evidence_date=date(2024, 8, 10),
        )
        store.register_evidence_document(grn)

        dispatch = store.allocate(
            QuantityAllocation(
                allocation_id=uuid.uuid4().hex,
                allocation_type=QuantityAllocationType.DISPATCH,
                source_document_type=DocumentType.DELIVERY_CHALLAN,
                source_document_id=challan.document_id,
                source_line_item_id=challan.lines[0].line_item_id,
                source_line_quantity=Decimal("100"),
                evidence_date=date(2024, 8, 1),
                po_item_id=item.po_item_id,
                quantity=Decimal("100"),
            )
        )
        with pytest.raises(ValueError, match="Matched receipts cannot exceed the dispatch"):
            store.allocate(
                QuantityAllocation(
                    allocation_id=uuid.uuid4().hex,
                    allocation_type=QuantityAllocationType.RECEIPT,
                    source_document_type=DocumentType.GOODS_RECEIVED_NOTE,
                    source_document_id=grn.document_id,
                    source_line_item_id=grn.lines[0].line_item_id,
                    source_line_quantity=Decimal("200"),
                    evidence_date=date(2024, 8, 10),
                    po_item_id=item.po_item_id,
                    quantity=Decimal("150"),  # > 100 dispatched
                    matched_dispatch_allocation_id=dispatch.allocation_id,
                )
            )
