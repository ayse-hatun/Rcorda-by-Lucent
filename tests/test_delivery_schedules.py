import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import hashlib
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from packages.domain.models import (
    DeliveryFindingType,
    DeliveryScheduleStatus,
    DocumentType,
    EvidenceDocument,
    EvidenceDocumentLine,
    QuantityAllocation,
    QuantityAllocationType,
    ScheduleAmendmentStatus,
)
from packages.reconciliation.delivery_schedule_store import (
    DeliveryScheduleStore,
    EvidenceNotFoundError,
)
from apps.api.main import app
from apps.api.routes import delivery_schedules as delivery_schedule_routes


@pytest.fixture
def store(tmp_path):
    return DeliveryScheduleStore(str(tmp_path / "delivery-schedules.sqlite3"))


def create_item(store, quantity="100"):
    return store.create_po_item(
        po_reference="PO-100",
        line_item_id="line-1",
        description="Steel plates",
        ordered_quantity=Decimal(quantity),
    )


def create_schedule(store, item, quantity, due_date):
    return store.create_schedule(
        po_item_id=item.po_item_id,
        planned_quantity=Decimal(quantity),
        expected_delivery_date=date.fromisoformat(due_date),
    )


def add_allocation(
    store,
    item,
    *,
    allocation_type,
    document_type,
    document_id,
    line_id="line-1",
    source_quantity,
    quantity,
    evidence_date,
    schedule=None,
    matched_dispatch_allocation_id=None,
):
    try:
        store.get_evidence_document(document_type, document_id)
    except EvidenceNotFoundError:
        store.register_evidence_document(
            EvidenceDocument(
                document_id=document_id,
                document_type=document_type,
                document_number=document_id,
                po_reference=item.po_reference,
                sha256=hashlib.sha256(
                    f"{document_type.value}:{document_id}".encode()
                ).hexdigest(),
                lines=[
                    EvidenceDocumentLine(
                        line_item_id=line_id,
                        quantity=Decimal(source_quantity),
                        evidence_date=date.fromisoformat(evidence_date),
                        po_item_ids=[item.po_item_id],
                    )
                ],
            )
        )
    return store.allocate(
        QuantityAllocation(
            allocation_id=f"{document_id}-{line_id}-{schedule.schedule_id if schedule else 'unscheduled'}",
            allocation_type=allocation_type,
            source_document_type=document_type,
            source_document_id=document_id,
            source_line_item_id=line_id,
            source_line_quantity=Decimal(source_quantity),
            evidence_date=date.fromisoformat(evidence_date),
            po_item_id=item.po_item_id,
            schedule_id=schedule.schedule_id if schedule else None,
            quantity=Decimal(quantity),
            matched_dispatch_allocation_id=matched_dispatch_allocation_id,
        )
    )


def test_multiple_scheduled_deliveries_calculate_due_quantity(store):
    item = create_item(store)
    create_schedule(store, item, "40", "2026-11-10")
    create_schedule(store, item, "60", "2026-11-20")

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 15))

    assert run.summary.ordered_quantity == Decimal("100")
    assert run.summary.scheduled_quantity == Decimal("100")
    assert run.summary.quantity_due_by_evaluation_date == Decimal("40")
    assert run.summary.outstanding_quantity == Decimal("100")
    assert run.summary.overdue_quantity == Decimal("40")


def test_partial_fulfilment_reduces_overdue_quantity(store):
    item = create_item(store, "50")
    schedule = create_schedule(store, item, "50", "2026-11-10")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-1",
        source_quantity="20",
        quantity="20",
        evidence_date="2026-11-09",
        schedule=schedule,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 11))

    assert run.summary.accepted_quantity == Decimal("20")
    assert run.summary.outstanding_quantity == Decimal("30")
    assert run.summary.overdue_quantity == Decimal("30")
    assert store.list_schedules(item.po_item_id)[0].status == DeliveryScheduleStatus.PARTIALLY_FULFILLED


def test_one_document_delivery_can_fulfil_multiple_schedules(store):
    item = create_item(store, "50")
    first = create_schedule(store, item, "20", "2026-11-10")
    second = create_schedule(store, item, "30", "2026-11-12")
    dispatches = {}
    for schedule, quantity in ((first, "20"), (second, "30")):
        dispatches[schedule.schedule_id] = add_allocation(
            store,
            item,
            allocation_type=QuantityAllocationType.DISPATCH,
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_id="DC-1",
            source_quantity="50",
            quantity=quantity,
            evidence_date="2026-11-10",
            schedule=schedule,
        )
    for schedule, quantity in ((first, "20"), (second, "30")):
        add_allocation(
            store,
            item,
            allocation_type=QuantityAllocationType.RECEIPT,
            document_type=DocumentType.GOODS_RECEIVED_NOTE,
            document_id="GRN-1",
            source_quantity="50",
            quantity=quantity,
            evidence_date="2026-11-10",
            schedule=schedule,
            matched_dispatch_allocation_id=dispatches[schedule.schedule_id].allocation_id,
        )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 15))

    assert run.summary.dispatched_quantity == Decimal("50")
    assert run.summary.accepted_quantity == Decimal("50")
    assert run.summary.dispatched_not_received_quantity == Decimal("0")
    assert run.summary.overdue_quantity == Decimal("0")


def test_late_delivery_is_reported_without_remaining_overdue_quantity(store):
    item = create_item(store, "10")
    schedule = create_schedule(store, item, "10", "2026-11-10")
    dispatch = add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-LATE",
        source_quantity="10",
        quantity="10",
        evidence_date="2026-11-12",
        schedule=schedule,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-LATE",
        source_quantity="10",
        quantity="10",
        evidence_date="2026-11-12",
        schedule=schedule,
        matched_dispatch_allocation_id=dispatch.allocation_id,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 13))

    assert run.summary.overdue_quantity == Decimal("0")
    assert any(f.type == DeliveryFindingType.LATE_DELIVERY for f in run.findings)


def test_early_delivery_is_reported(store):
    item = create_item(store, "10")
    schedule = create_schedule(store, item, "10", "2026-11-20")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-EARLY",
        source_quantity="10",
        quantity="10",
        evidence_date="2026-11-18",
        schedule=schedule,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 21))

    assert any(f.type == DeliveryFindingType.EARLY_DELIVERY for f in run.findings)


def test_approved_schedule_amendment_keeps_actor_and_validation_history(
    store, tmp_path
):
    item = create_item(store)
    schedule = create_schedule(store, item, "50", "2026-11-10")
    store.run_reconciliation(item.po_item_id, date(2026, 11, 11))
    amendment = store.propose_amendment(
        schedule.schedule_id,
        Decimal("40"),
        date(2026, 11, 20),
        requested_by="buyer-1",
    )
    approved = store.approve_amendment(amendment.amendment_id, "manager-7")
    store.run_reconciliation(item.po_item_id, date(2026, 11, 21))

    reopened = DeliveryScheduleStore(str(tmp_path / "delivery-schedules.sqlite3"))
    persisted_schedule = reopened.list_schedules(item.po_item_id)[0]
    runs = reopened.list_validation_runs(item.po_item_id)

    assert approved.status == ScheduleAmendmentStatus.APPROVED
    assert approved.approved_by == "manager-7"
    assert persisted_schedule.planned_quantity == Decimal("40")
    assert len(persisted_schedule.amendment_history) == 1
    assert persisted_schedule.amendment_history[0].requested_by == "buyer-1"
    assert persisted_schedule.amendment_history[0].approved_by == "manager-7"
    assert len(runs) == 2
    assert runs[0].summary.overdue_quantity == Decimal("50")
    assert runs[1].summary.overdue_quantity == Decimal("40")


def test_allocations_and_validation_runs_are_immutable(store):
    item = create_item(store, "10")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-IMMUTABLE",
        source_quantity="5",
        quantity="5",
        evidence_date="2026-11-10",
    )
    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 11))

    with pytest.raises(sqlite3.IntegrityError, match="Quantity allocations are immutable"):
        with store._connection(write=True) as connection:
            connection.execute(
                "UPDATE quantity_allocations SET quantity = '1' WHERE allocation_id = ?",
                ("DC-IMMUTABLE-line-1-unscheduled",),
            )
    with pytest.raises(sqlite3.IntegrityError, match="Validation runs are immutable"):
        with store._connection(write=True) as connection:
            connection.execute(
                "DELETE FROM validation_runs WHERE run_id = ?", (run.run_id,)
            )

    assert store.list_validation_runs(item.po_item_id)[0].run_id == run.run_id


def test_dispatch_without_grn_remains_in_transit_not_lost(store):
    item = create_item(store, "10")
    schedule = create_schedule(store, item, "10", "2026-11-20")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-1",
        source_quantity="7",
        quantity="7",
        evidence_date="2026-11-12",
        schedule=schedule,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 15))
    finding = next(
        finding
        for finding in run.findings
        if finding.type == DeliveryFindingType.DISPATCH_NOT_RECEIVED
    )

    assert run.summary.dispatched_not_received_quantity == Decimal("7")
    assert run.summary.outstanding_quantity == Decimal("10")
    assert run.summary.overdue_quantity == Decimal("0")
    assert "not evidence that the goods are lost" in finding.explanation


def test_grn_without_challan_is_reported_separately(store):
    item = create_item(store, "10")
    schedule = create_schedule(store, item, "10", "2026-11-20")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-UNMATCHED",
        source_quantity="5",
        quantity="5",
        evidence_date="2026-11-12",
        schedule=schedule,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-ORPHAN",
        source_quantity="5",
        quantity="5",
        evidence_date="2026-11-12",
        schedule=schedule,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 15))

    assert run.summary.accepted_quantity == Decimal("5")
    assert run.summary.dispatched_not_received_quantity == Decimal("5")
    assert any(
        finding.type == DeliveryFindingType.RECEIPT_WITHOUT_CHALLAN
        for finding in run.findings
    )
    assert any(
        finding.type == DeliveryFindingType.DISPATCH_NOT_RECEIVED
        for finding in run.findings
    )


def test_over_delivery_is_detected(store):
    item = create_item(store, "10")
    schedule = create_schedule(store, item, "10", "2026-11-10")
    dispatch = add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-OVER",
        source_quantity="12",
        quantity="10",
        evidence_date="2026-11-10",
        schedule=schedule,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-OVER",
        source_quantity="12",
        quantity="2",
        evidence_date="2026-11-10",
        schedule=None,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-OVER",
        source_quantity="12",
        quantity="10",
        evidence_date="2026-11-10",
        schedule=schedule,
        matched_dispatch_allocation_id=dispatch.allocation_id,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-OVER",
        source_quantity="12",
        quantity="2",
        evidence_date="2026-11-10",
        schedule=None,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 11))

    finding = next(
        finding
        for finding in run.findings
        if finding.type == DeliveryFindingType.OVER_DELIVERY
    )
    assert finding.quantity == Decimal("2")
    assert run.summary.outstanding_quantity == Decimal("0")


def test_missing_schedule_is_not_misclassified_as_overdue(store):
    item = create_item(store, "10")

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 20))

    assert run.summary.scheduled_quantity == Decimal("0")
    assert run.summary.outstanding_quantity == Decimal("10")
    assert run.summary.overdue_quantity == Decimal("0")
    assert any(
        finding.type == DeliveryFindingType.MISSING_DELIVERY_SCHEDULE
        for finding in run.findings
    )
    assert not any(
        finding.type == DeliveryFindingType.OVERDUE_DELIVERY
        for finding in run.findings
    )


def test_allocations_reject_duplicate_and_overallocated_source_quantity(store):
    item = create_item(store, "10")
    first_schedule = create_schedule(store, item, "5", "2026-11-20")
    second_schedule = create_schedule(store, item, "5", "2026-11-21")
    store.register_evidence_document(
        EvidenceDocument(
            document_id="DC-SPLIT",
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_number="DC-SPLIT",
            po_reference=item.po_reference,
            sha256=hashlib.sha256(b"DC-SPLIT").hexdigest(),
            lines=[
                EvidenceDocumentLine(
                    line_item_id="line-1",
                    quantity=Decimal("8"),
                    evidence_date=date(2026, 11, 10),
                    po_item_ids=[item.po_item_id],
                )
            ],
        )
    )
    allocation = QuantityAllocation(
        allocation_id="allocation-1",
        allocation_type=QuantityAllocationType.DISPATCH,
        source_document_type=DocumentType.DELIVERY_CHALLAN,
        source_document_id="DC-SPLIT",
        source_line_item_id="line-1",
        source_line_quantity=Decimal("8"),
        evidence_date=date(2026, 11, 10),
        po_item_id=item.po_item_id,
        schedule_id=first_schedule.schedule_id,
        quantity=Decimal("5"),
    )
    store.allocate(allocation)

    repeated = store.allocate(allocation.model_copy(update={"allocation_id": "duplicate"}))
    assert repeated.allocation_id == allocation.allocation_id
    assert len(store.list_allocations(item.po_item_id)) == 1
    with pytest.raises(ValueError, match="cannot exceed"):
        store.allocate(
            allocation.model_copy(
                update={
                    "allocation_id": "allocation-2",
                    "schedule_id": second_schedule.schedule_id,
                    "quantity": Decimal("4"),
                }
            )
        )


def test_receipt_allocations_require_grn_and_dispatch_requires_challan(store):
    item = create_item(store, "10")

    with pytest.raises(ValueError, match="DELIVERY_CHALLAN evidence"):
        add_allocation(
            store,
            item,
            allocation_type=QuantityAllocationType.DISPATCH,
            document_type=DocumentType.GOODS_RECEIVED_NOTE,
            document_id="wrong-doc",
            source_quantity="5",
            quantity="5",
            evidence_date="2026-11-10",
        )
    with pytest.raises(ValueError, match="RECEIPT allocations require GRN evidence"):
        add_allocation(
            store,
            item,
            allocation_type=QuantityAllocationType.RECEIPT,
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_id="wrong-receipt-doc",
            source_quantity="5",
            quantity="5",
            evidence_date="2026-11-10",
        )


def test_schedule_quantities_cannot_exceed_po_item_quantity(store):
    item = create_item(store, "100")
    schedule = create_schedule(store, item, "60", "2026-11-20")

    with pytest.raises(ValueError, match="cannot exceed the PO item quantity"):
        create_schedule(store, item, "41", "2026-11-25")

    amendment = store.propose_amendment(
        schedule.schedule_id,
        Decimal("101"),
        date(2026, 11, 25),
        requested_by="buyer-1",
    )
    with pytest.raises(ValueError, match="cannot exceed the PO item quantity"):
        store.approve_amendment(amendment.amendment_id, "manager-1")

    assert store.list_schedules(item.po_item_id)[0].planned_quantity == Decimal("60")


def test_dispatches_from_multiple_challans_aggregate_and_over_delivery_is_retained(store):
    item = create_item(store)
    first = create_schedule(store, item, "50", "2026-11-20")
    second = create_schedule(store, item, "50", "2026-11-21")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-AGG-1",
        source_quantity="60",
        quantity="50",
        evidence_date="2026-11-10",
        schedule=first,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-AGG-1",
        source_quantity="60",
        quantity="10",
        evidence_date="2026-11-10",
        schedule=second,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-AGG-2",
        source_quantity="40",
        quantity="40",
        evidence_date="2026-11-11",
        schedule=second,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-AGG-3",
        source_quantity="20",
        quantity="20",
        evidence_date="2026-11-12",
        schedule=None,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 22))

    assert run.summary.dispatched_quantity == Decimal("120")
    assert run.summary.unscheduled_dispatch_quantity == Decimal("20")
    assert any(
        finding.type == DeliveryFindingType.OVER_DELIVERY
        and finding.quantity == Decimal("20")
        for finding in run.findings
    )


def test_multiple_grns_aggregate_and_in_transit_is_matched_only(store):
    item = create_item(store)
    schedule = create_schedule(store, item, "100", "2026-11-20")
    first_dispatch = add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-001",
        source_quantity="60",
        quantity="60",
        evidence_date="2026-11-09",
        schedule=schedule,
    )
    second_dispatch = add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-002",
        source_quantity="40",
        quantity="40",
        evidence_date="2026-11-10",
        schedule=schedule,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-001",
        source_quantity="30",
        quantity="30",
        evidence_date="2026-11-11",
        schedule=schedule,
        matched_dispatch_allocation_id=first_dispatch.allocation_id,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-002",
        source_quantity="20",
        quantity="20",
        evidence_date="2026-11-12",
        schedule=schedule,
        matched_dispatch_allocation_id=second_dispatch.allocation_id,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 22))

    assert run.summary.ordered_quantity == Decimal("100")
    assert run.summary.dispatched_quantity == Decimal("100")
    assert run.summary.accepted_quantity == Decimal("50")
    assert run.summary.outstanding_quantity == Decimal("50")
    assert run.summary.dispatched_not_received_quantity == Decimal("50")


def test_accepted_over_delivery_is_retained_and_flagged(store):
    item = create_item(store, "100")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-OVER-1",
        source_quantity="70",
        quantity="70",
        evidence_date="2026-11-10",
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-OVER-2",
        source_quantity="50",
        quantity="50",
        evidence_date="2026-11-11",
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 12))

    assert run.summary.accepted_quantity == Decimal("120")
    assert run.summary.outstanding_quantity == Decimal("0")
    assert any(
        finding.type == DeliveryFindingType.OVER_DELIVERY
        and finding.quantity == Decimal("20")
        for finding in run.findings
    )


def test_schedule_allocation_cap_keeps_excess_as_unscheduled_evidence(store):
    item = create_item(store, "20")
    schedule = create_schedule(store, item, "5", "2026-11-20")
    with pytest.raises(ValueError, match="allocate the remaining evidence as unscheduled"):
        add_allocation(
            store,
            item,
            allocation_type=QuantityAllocationType.DISPATCH,
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_id="DC-SCHEDULE-LIMIT",
            source_quantity="7",
            quantity="6",
            evidence_date="2026-11-10",
            schedule=schedule,
        )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-SCHEDULE-LIMIT",
        source_quantity="7",
        quantity="5",
        evidence_date="2026-11-10",
        schedule=schedule,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-SCHEDULE-LIMIT",
        source_quantity="7",
        quantity="2",
        evidence_date="2026-11-10",
        schedule=None,
    )

    run = store.run_reconciliation(item.po_item_id, date(2026, 11, 21))

    assert run.summary.dispatched_quantity == Decimal("7")
    assert run.summary.unscheduled_dispatch_quantity == Decimal("2")
    assert any(f.type == DeliveryFindingType.UNSCHEDULED_EVIDENCE for f in run.findings)
    assert sum(
        allocation.quantity for allocation in store.list_allocations(item.po_item_id)
    ) == Decimal("7")


def test_unregistered_source_document_and_line_are_rejected(store):
    item = create_item(store)
    allocation = QuantityAllocation(
        allocation_id="unregistered-allocation",
        allocation_type=QuantityAllocationType.DISPATCH,
        source_document_type=DocumentType.DELIVERY_CHALLAN,
        source_document_id="DC-NOT-REGISTERED",
        source_line_item_id="missing-line",
        source_line_quantity=Decimal("3"),
        evidence_date=date(2026, 11, 10),
        po_item_id=item.po_item_id,
        quantity=Decimal("3"),
    )
    with pytest.raises(EvidenceNotFoundError, match="not registered"):
        store.allocate(allocation)

    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-REGISTERED",
        source_quantity="3",
        quantity="1",
        evidence_date="2026-11-10",
    )
    bad_line = allocation.model_copy(
        update={
            "allocation_id": "unregistered-line",
            "source_document_id": "DC-REGISTERED",
        }
    )
    with pytest.raises(EvidenceNotFoundError, match="not registered"):
        store.allocate(bad_line)


def test_allocation_requires_line_mapping_to_target_po_item(store):
    item = create_item(store)
    wrong_item = store.create_po_item(
        po_reference=item.po_reference,
        line_item_id="line-2",
        description="Different item",
        ordered_quantity=Decimal("10"),
    )
    store.register_evidence_document(
        EvidenceDocument(
            document_id="DC-MAPPED",
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_number="DC-MAPPED",
            po_reference=item.po_reference,
            sha256=hashlib.sha256(b"DC-MAPPED").hexdigest(),
            lines=[
                EvidenceDocumentLine(
                    line_item_id="line-1",
                    quantity=Decimal("5"),
                    evidence_date=date(2026, 11, 10),
                    po_item_ids=[item.po_item_id],
                )
            ],
        )
    )
    with pytest.raises(ValueError, match="not mapped to this PO item"):
        store.allocate(
            QuantityAllocation(
                allocation_id="wrong-po-item",
                allocation_type=QuantityAllocationType.DISPATCH,
                source_document_type=DocumentType.DELIVERY_CHALLAN,
                source_document_id="DC-MAPPED",
                source_line_item_id="line-1",
                source_line_quantity=Decimal("5"),
                evidence_date=date(2026, 11, 10),
                po_item_id=wrong_item.po_item_id,
                quantity=Decimal("1"),
            )
        )


def test_evidence_line_mappings_must_belong_to_document_purchase_order(store):
    item = create_item(store)
    other_item = store.create_po_item(
        po_reference="PO-OTHER",
        line_item_id="other-line",
        description="Other purchase order item",
        ordered_quantity=Decimal("10"),
    )
    with pytest.raises(ValueError, match="document's PO"):
        store.register_evidence_document(
            EvidenceDocument(
                document_id="DC-WRONG-PO",
                document_type=DocumentType.DELIVERY_CHALLAN,
                document_number="DC-WRONG-PO",
                po_reference=item.po_reference,
                sha256=hashlib.sha256(b"DC-WRONG-PO").hexdigest(),
                lines=[
                    EvidenceDocumentLine(
                        line_item_id="line-1",
                        quantity=Decimal("5"),
                        evidence_date=date(2026, 11, 10),
                        po_item_ids=[other_item.po_item_id],
                    )
                ],
            )
        )


def test_allocation_rejects_schedule_belonging_to_another_po_item(store):
    item = create_item(store, "20")
    other_item = store.create_po_item(
        po_reference=item.po_reference,
        line_item_id="other-line",
        description="Other item",
        ordered_quantity=Decimal("10"),
    )
    wrong_schedule = create_schedule(store, other_item, "10", "2026-11-20")

    with pytest.raises(ValueError, match="does not belong to this PO item"):
        add_allocation(
            store,
            item,
            allocation_type=QuantityAllocationType.DISPATCH,
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_id="DC-WRONG-SCHEDULE",
            source_quantity="5",
            quantity="5",
            evidence_date="2026-11-10",
            schedule=wrong_schedule,
        )


def test_identical_document_fingerprint_cannot_be_registered_twice(store):
    item = create_item(store)
    document = EvidenceDocument(
        document_id="DC-HASH-1",
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_number="DC-HASH-1",
        po_reference=item.po_reference,
        sha256=hashlib.sha256(b"same physical document").hexdigest(),
        lines=[
            EvidenceDocumentLine(
                line_item_id="line-1",
                quantity=Decimal("5"),
                evidence_date=date(2026, 11, 10),
                po_item_ids=[item.po_item_id],
            )
        ],
    )
    store.register_evidence_document(document)
    assert store.register_evidence_document(document).document_id == document.document_id
    with pytest.raises(ValueError, match="Registered evidence document lines are immutable"):
        store.register_evidence_document(
            document.model_copy(
                update={
                    "lines": [
                        document.lines[0].model_copy(update={"quantity": Decimal("6")})
                    ]
                }
            )
        )
    assert store.get_evidence_document(
        DocumentType.DELIVERY_CHALLAN, document.document_id
    ).lines[0].quantity == Decimal("5")

    with pytest.raises(ValueError, match="fingerprint is already registered"):
        store.register_evidence_document(
            document.model_copy(update={"document_id": "DC-HASH-2"})
        )


def test_source_line_allocation_limit_spans_multiple_po_items(store):
    first_item = create_item(store, "100")
    second_item = store.create_po_item(
        po_reference=first_item.po_reference,
        line_item_id="line-2",
        description="Other item",
        ordered_quantity=Decimal("100"),
    )
    store.register_evidence_document(
        EvidenceDocument(
            document_id="DC-MULTI-ITEM",
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_number="DC-MULTI-ITEM",
            po_reference=first_item.po_reference,
            sha256=hashlib.sha256(b"DC-MULTI-ITEM").hexdigest(),
            lines=[
                EvidenceDocumentLine(
                    line_item_id="line-1",
                    quantity=Decimal("10"),
                    evidence_date=date(2026, 11, 10),
                    po_item_ids=[first_item.po_item_id, second_item.po_item_id],
                )
            ],
        )
    )
    store.allocate(
        QuantityAllocation(
            allocation_id="first-item-allocation",
            allocation_type=QuantityAllocationType.DISPATCH,
            source_document_type=DocumentType.DELIVERY_CHALLAN,
            source_document_id="DC-MULTI-ITEM",
            source_line_item_id="line-1",
            source_line_quantity=Decimal("10"),
            evidence_date=date(2026, 11, 10),
            po_item_id=first_item.po_item_id,
            quantity=Decimal("7"),
        )
    )

    with pytest.raises(ValueError, match="cannot exceed the source document line quantity"):
        store.allocate(
            QuantityAllocation(
                allocation_id="second-item-allocation",
                allocation_type=QuantityAllocationType.DISPATCH,
                source_document_type=DocumentType.DELIVERY_CHALLAN,
                source_document_id="DC-MULTI-ITEM",
                source_line_item_id="line-1",
                source_line_quantity=Decimal("10"),
                evidence_date=date(2026, 11, 10),
                po_item_id=second_item.po_item_id,
                quantity=Decimal("4"),
            )
        )


def test_concurrent_allocations_cannot_exceed_registered_source_line(store):
    item = create_item(store, "100")
    first_schedule = create_schedule(store, item, "50", "2026-11-20")
    second_schedule = create_schedule(store, item, "50", "2026-11-21")
    store.register_evidence_document(
        EvidenceDocument(
            document_id="DC-CONCURRENT",
            document_type=DocumentType.DELIVERY_CHALLAN,
            document_number="DC-CONCURRENT",
            po_reference=item.po_reference,
            sha256=hashlib.sha256(b"DC-CONCURRENT").hexdigest(),
            lines=[
                EvidenceDocumentLine(
                    line_item_id="line-1",
                    quantity=Decimal("50"),
                    evidence_date=date(2026, 11, 10),
                    po_item_ids=[item.po_item_id],
                )
            ],
        )
    )
    barrier = Barrier(2)

    def attempt(schedule, allocation_id):
        allocation = QuantityAllocation(
            allocation_id=allocation_id,
            allocation_type=QuantityAllocationType.DISPATCH,
            source_document_type=DocumentType.DELIVERY_CHALLAN,
            source_document_id="DC-CONCURRENT",
            source_line_item_id="line-1",
            source_line_quantity=Decimal("50"),
            evidence_date=date(2026, 11, 10),
            po_item_id=item.po_item_id,
            schedule_id=schedule.schedule_id,
            quantity=Decimal("30"),
        )
        barrier.wait()
        try:
            store.allocate(allocation)
            return "accepted"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda args: attempt(*args),
                [
                    (first_schedule, "concurrent-1"),
                    (second_schedule, "concurrent-2"),
                ],
            )
        )

    assert sorted(outcomes) == ["accepted", "rejected"]
    assert sum(
        allocation.quantity for allocation in store.list_allocations(item.po_item_id)
    ) == Decimal("30")


def test_schedule_amendment_cannot_reduce_below_existing_dispatch(store):
    item = create_item(store, "20")
    schedule = create_schedule(store, item, "10", "2026-11-20")
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-AMEND-1",
        source_quantity="8",
        quantity="8",
        evidence_date="2026-11-10",
        schedule=schedule,
    )
    amendment = store.propose_amendment(
        schedule.schedule_id,
        Decimal("7"),
        date(2026, 11, 25),
        requested_by="buyer-1",
    )

    with pytest.raises(ValueError, match="below quantities already evidenced"):
        store.approve_amendment(amendment.amendment_id, "manager-1")

    assert store.list_schedules(item.po_item_id)[0].planned_quantity == Decimal("10")


def test_delivery_schedule_api_persists_amendments_allocations_and_runs(
    tmp_path,
):
    # Route now uses PostgreSQL via DI; no monkeypatching needed.
    uid = uuid.uuid4().hex[:8]
    item_id = f"api-item-{uid}"
    dc_id = f"DC-API-{uid}"
    grn_id = f"GRN-API-{uid}"
    dc_sha = hashlib.sha256(f"api-dc-{uid}".encode()).hexdigest()
    grn_sha = hashlib.sha256(f"api-grn-{uid}".encode()).hexdigest()

    client = TestClient(app)
    item_response = client.post(
        "/v1/delivery-schedules/po-items",
        json={
            "po_item_id": item_id,
            "po_reference": f"PO-API-{uid}",
            "line_item_id": "line-1",
            "description": "Valves",
            "ordered_quantity": "10",
        },
    )
    assert item_response.status_code == 201
    schedule_response = client.post(
        f"/v1/delivery-schedules/po-items/{item_id}/schedules",
        json={
            "planned_quantity": "10",
            "expected_delivery_date": "2026-11-10",
        },
    )
    assert schedule_response.status_code == 201
    schedule_id = schedule_response.json()["schedule_id"]
    amendment_response = client.post(
        f"/v1/delivery-schedules/schedules/{schedule_id}/amendments",
        json={
            "proposed_quantity": "10",
            "proposed_expected_date": "2026-11-15",
            "requested_by": "buyer-1",
        },
    )
    amendment_id = amendment_response.json()["amendment_id"]
    approved_response = client.post(
        f"/v1/delivery-schedules/amendments/{amendment_id}/approve",
        json={"approved_by": "manager-1"},
    )
    assert approved_response.status_code == 200
    document_response = client.post(
        "/v1/delivery-schedules/source-documents",
        json={
            "document_id": dc_id,
            "document_type": "DELIVERY_CHALLAN",
            "document_number": dc_id,
            "po_reference": f"PO-API-{uid}",
            "sha256": dc_sha,
            "lines": [
                {
                    "line_item_id": "line-1",
                    "quantity": "4",
                    "evidence_date": "2026-11-14",
                    "po_item_ids": [item_id],
                }
            ],
        },
    )
    assert document_response.status_code == 201
    grn_document_response = client.post(
        "/v1/delivery-schedules/source-documents",
        json={
            "document_id": grn_id,
            "document_type": "GRN",
            "document_number": grn_id,
            "po_reference": f"PO-API-{uid}",
            "sha256": grn_sha,
            "lines": [
                {
                    "line_item_id": "line-1",
                    "quantity": "4",
                    "evidence_date": "2026-11-14",
                    "po_item_ids": [item_id],
                }
            ],
        },
    )
    assert grn_document_response.status_code == 201
    dispatch_response = client.post(
        "/v1/delivery-schedules/allocations",
        json={
            "allocation_type": "DISPATCH",
            "source_document_type": "DELIVERY_CHALLAN",
            "source_document_id": dc_id,
            "source_line_item_id": "line-1",
            "source_line_quantity": "4",
            "evidence_date": "2026-11-14",
            "po_item_id": item_id,
            "schedule_id": schedule_id,
            "quantity": "4",
        },
    )
    assert dispatch_response.status_code == 201
    allocation_response = client.post(
        "/v1/delivery-schedules/allocations",
        json={
            "allocation_type": "RECEIPT",
            "source_document_type": "GRN",
            "source_document_id": grn_id,
            "source_line_item_id": "line-1",
            "source_line_quantity": "4",
            "evidence_date": "2026-11-14",
            "po_item_id": item_id,
            "schedule_id": schedule_id,
            "quantity": "4",
            "matched_dispatch_allocation_id": dispatch_response.json()[
                "allocation_id"
            ],
        },
    )
    assert allocation_response.status_code == 201
    run_response = client.post(
        f"/v1/delivery-schedules/po-items/{item_id}/reconcile",
        json={"evaluation_date": "2026-11-16"},
    )
    history_response = client.get(
        f"/v1/delivery-schedules/po-items/{item_id}/validation-runs"
    )
    schedule_history_response = client.get(
        f"/v1/delivery-schedules/po-items/{item_id}"
    )

    assert run_response.status_code == 200
    assert Decimal(run_response.json()["summary"]["accepted_quantity"]) == Decimal("4")
    assert len(history_response.json()) == 1
    history = schedule_history_response.json()["schedules"][0]["amendment_history"]
    assert len(history) == 1
    assert history[0]["approved_by"] == "manager-1"
    repeated_response = client.post(
        "/v1/delivery-schedules/allocations",
        json={
            "allocation_type": "RECEIPT",
            "source_document_type": "GRN",
            "source_document_id": grn_id,
            "source_line_item_id": "line-1",
            "source_line_quantity": "4",
            "evidence_date": "2026-11-14",
            "po_item_id": item_id,
            "schedule_id": schedule_id,
            "quantity": "4",
            "matched_dispatch_allocation_id": dispatch_response.json()[
                "allocation_id"
            ],
        },
    )
    assert repeated_response.status_code == 201
    assert repeated_response.json()["allocation_id"] == allocation_response.json()["allocation_id"]


def test_matched_receipts_cannot_overallocate_dispatch_quantity(store):
    item = create_item(store, "10")
    schedule = create_schedule(store, item, "10", "2026-11-20")
    dispatch = add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.DISPATCH,
        document_type=DocumentType.DELIVERY_CHALLAN,
        document_id="DC-PARTIAL",
        source_quantity="5",
        quantity="5",
        evidence_date="2026-11-10",
        schedule=schedule,
    )

    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-PART-1",
        source_quantity="3",
        quantity="3",
        evidence_date="2026-11-11",
        schedule=schedule,
        matched_dispatch_allocation_id=dispatch.allocation_id,
    )
    add_allocation(
        store,
        item,
        allocation_type=QuantityAllocationType.RECEIPT,
        document_type=DocumentType.GOODS_RECEIVED_NOTE,
        document_id="GRN-PART-2",
        source_quantity="2",
        quantity="2",
        evidence_date="2026-11-12",
        schedule=schedule,
        matched_dispatch_allocation_id=dispatch.allocation_id,
    )
    with pytest.raises(ValueError, match="cannot exceed the dispatch"):
        add_allocation(
            store,
            item,
            allocation_type=QuantityAllocationType.RECEIPT,
            document_type=DocumentType.GOODS_RECEIVED_NOTE,
            document_id="GRN-PART-3",
            source_quantity="1",
            quantity="1",
            evidence_date="2026-11-13",
            schedule=schedule,
            matched_dispatch_allocation_id=dispatch.allocation_id,
        )
