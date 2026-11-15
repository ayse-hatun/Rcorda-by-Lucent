from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Tuple

from packages.domain.models import (
    DeliveryFindingType,
    DeliveryReconciliationFinding,
    DeliveryReconciliationSummary,
    DeliverySchedule,
    DeliveryScheduleStatus,
    PurchaseOrderItem,
    QuantityAllocation,
    QuantityAllocationType,
)


class DeliveryScheduleReconciliationEngine:
    """Reconciles scheduled quantities against dispatch and receipt evidence."""

    def reconcile(
        self,
        po_item: PurchaseOrderItem,
        schedules: Iterable[DeliverySchedule],
        allocations: Iterable[QuantityAllocation],
        evaluation_date: date,
    ) -> Tuple[DeliveryReconciliationSummary, List[DeliveryReconciliationFinding]]:
        active_schedules = [
            schedule
            for schedule in schedules
            if schedule.status != DeliveryScheduleStatus.CANCELLED
        ]
        effective_allocations = [
            allocation
            for allocation in allocations
            if allocation.evidence_date <= evaluation_date
        ]
        schedule_by_id = {schedule.schedule_id: schedule for schedule in active_schedules}

        scheduled_quantity = sum(
            (schedule.planned_quantity for schedule in active_schedules),
            Decimal("0"),
        )
        due_quantity = sum(
            (
                schedule.planned_quantity
                for schedule in active_schedules
                if schedule.expected_delivery_date <= evaluation_date
            ),
            Decimal("0"),
        )
        dispatched_quantity = self._quantity_for_type(
            effective_allocations, QuantityAllocationType.DISPATCH
        )
        accepted_quantity = self._quantity_for_type(
            effective_allocations, QuantityAllocationType.RECEIPT
        )
        unscheduled_dispatch_quantity = sum(
            (
                allocation.quantity
                for allocation in effective_allocations
                if allocation.allocation_type == QuantityAllocationType.DISPATCH
                and allocation.schedule_id is None
            ),
            Decimal("0"),
        )
        unscheduled_accepted_quantity = sum(
            (
                allocation.quantity
                for allocation in effective_allocations
                if allocation.allocation_type == QuantityAllocationType.RECEIPT
                and allocation.schedule_id is None
            ),
            Decimal("0"),
        )

        dispatch_allocations = {
            allocation.allocation_id: allocation
            for allocation in effective_allocations
            if allocation.allocation_type == QuantityAllocationType.DISPATCH
        }
        receipts_by_schedule: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        receipts_by_dispatch: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for allocation in effective_allocations:
            if (
                allocation.allocation_type == QuantityAllocationType.RECEIPT
                and allocation.schedule_id
            ):
                receipts_by_schedule[allocation.schedule_id] += allocation.quantity
            if allocation.matched_dispatch_allocation_id in dispatch_allocations:
                receipts_by_dispatch[
                    allocation.matched_dispatch_allocation_id
                ] += allocation.quantity
        dispatched_not_received = sum(
            (
                max(
                    allocation.quantity - receipts_by_dispatch.get(allocation_id, Decimal("0")),
                    Decimal("0"),
                )
                for allocation_id, allocation in dispatch_allocations.items()
            ),
            Decimal("0"),
        )
        receipt_without_challan = sum(
            (
                allocation.quantity
                for allocation in effective_allocations
                if allocation.allocation_type == QuantityAllocationType.RECEIPT
                and allocation.matched_dispatch_allocation_id not in dispatch_allocations
            ),
            Decimal("0"),
        )

        overdue_quantity = Decimal("0")
        findings: List[DeliveryReconciliationFinding] = []
        if scheduled_quantity < po_item.ordered_quantity:
            findings.append(
                DeliveryReconciliationFinding(
                    type=DeliveryFindingType.MISSING_DELIVERY_SCHEDULE,
                    po_item_id=po_item.po_item_id,
                    quantity=po_item.ordered_quantity - scheduled_quantity,
                    explanation="Part of the ordered quantity has no active delivery schedule.",
                )
            )

        for schedule in active_schedules:
            received_by_schedule = receipts_by_schedule.get(
                schedule.schedule_id, Decimal("0")
            )
            if schedule.expected_delivery_date < evaluation_date:
                remaining = max(
                    schedule.planned_quantity - received_by_schedule,
                    Decimal("0"),
                )
                overdue_quantity += remaining
                if remaining > 0:
                    findings.append(
                        DeliveryReconciliationFinding(
                            type=DeliveryFindingType.OVERDUE_DELIVERY,
                            po_item_id=po_item.po_item_id,
                            schedule_id=schedule.schedule_id,
                            quantity=remaining,
                            explanation=(
                                f"{remaining} units remain unreceived after the "
                                f"confirmed due date {schedule.expected_delivery_date}."
                            ),
                        )
                    )
            if received_by_schedule > schedule.planned_quantity:
                findings.append(
                    DeliveryReconciliationFinding(
                        type=DeliveryFindingType.OVER_DELIVERY,
                        po_item_id=po_item.po_item_id,
                        schedule_id=schedule.schedule_id,
                        quantity=received_by_schedule - schedule.planned_quantity,
                        explanation=(
                            "Accepted receipt quantity exceeds this delivery "
                            "schedule's planned quantity."
                        ),
                    )
                )

        if dispatched_not_received > 0:
            findings.append(
                DeliveryReconciliationFinding(
                    type=DeliveryFindingType.DISPATCH_NOT_RECEIVED,
                    po_item_id=po_item.po_item_id,
                    quantity=dispatched_not_received,
                    explanation=(
                        "Dispatch evidence exists for this quantity, but matching "
                        "receipt evidence has not been recorded; this is not evidence "
                        "that the goods are lost."
                    ),
                )
            )
        if receipt_without_challan > 0:
            findings.append(
                DeliveryReconciliationFinding(
                    type=DeliveryFindingType.RECEIPT_WITHOUT_CHALLAN,
                    po_item_id=po_item.po_item_id,
                    quantity=receipt_without_challan,
                    explanation="Receipt evidence exists without matching dispatch evidence.",
                )
            )
        if unscheduled_dispatch_quantity + unscheduled_accepted_quantity > 0:
            findings.append(
                DeliveryReconciliationFinding(
                    type=DeliveryFindingType.UNSCHEDULED_EVIDENCE,
                    po_item_id=po_item.po_item_id,
                    quantity=unscheduled_dispatch_quantity
                    + unscheduled_accepted_quantity,
                    explanation=(
                        "This evidence is retained against the PO item but is not "
                        "attributed to a delivery schedule."
                    ),
                )
            )

        for allocation in effective_allocations:
            if allocation.allocation_type != QuantityAllocationType.RECEIPT:
                continue
            schedule = schedule_by_id.get(allocation.schedule_id or "")
            if schedule is None:
                continue
            if allocation.evidence_date > schedule.expected_delivery_date:
                finding_type = DeliveryFindingType.LATE_DELIVERY
                explanation = (
                    f"Receipt was recorded on {allocation.evidence_date}, after the "
                    f"confirmed due date {schedule.expected_delivery_date}."
                )
            elif allocation.evidence_date < schedule.expected_delivery_date:
                finding_type = DeliveryFindingType.EARLY_DELIVERY
                explanation = (
                    f"Receipt was recorded on {allocation.evidence_date}, before the "
                    f"confirmed due date {schedule.expected_delivery_date}."
                )
            else:
                continue
            findings.append(
                DeliveryReconciliationFinding(
                    type=finding_type,
                    po_item_id=po_item.po_item_id,
                    schedule_id=schedule.schedule_id,
                    quantity=allocation.quantity,
                    explanation=explanation,
                )
            )

        if dispatched_quantity > po_item.ordered_quantity:
            findings.append(
                DeliveryReconciliationFinding(
                    type=DeliveryFindingType.OVER_DELIVERY,
                    po_item_id=po_item.po_item_id,
                    quantity=dispatched_quantity - po_item.ordered_quantity,
                    explanation="Total dispatched quantity exceeds the ordered quantity.",
                )
            )
        if accepted_quantity > po_item.ordered_quantity:
            findings.append(
                DeliveryReconciliationFinding(
                    type=DeliveryFindingType.OVER_DELIVERY,
                    po_item_id=po_item.po_item_id,
                    quantity=accepted_quantity - po_item.ordered_quantity,
                    explanation="Accepted receipt quantity exceeds the ordered quantity.",
                )
            )

        summary = DeliveryReconciliationSummary(
            po_item_id=po_item.po_item_id,
            evaluation_date=evaluation_date,
            ordered_quantity=po_item.ordered_quantity,
            scheduled_quantity=scheduled_quantity,
            quantity_due_by_evaluation_date=due_quantity,
            dispatched_quantity=dispatched_quantity,
            accepted_quantity=accepted_quantity,
            outstanding_quantity=max(
                po_item.ordered_quantity - accepted_quantity, Decimal("0")
            ),
            overdue_quantity=overdue_quantity,
            dispatched_not_received_quantity=dispatched_not_received,
            unscheduled_dispatch_quantity=unscheduled_dispatch_quantity,
            unscheduled_accepted_quantity=unscheduled_accepted_quantity,
        )
        return summary, findings

    @staticmethod
    def _quantity_for_type(
        allocations: Iterable[QuantityAllocation],
        allocation_type: QuantityAllocationType,
    ) -> Decimal:
        return sum(
            (
                allocation.quantity
                for allocation in allocations
                if allocation.allocation_type == allocation_type
            ),
            Decimal("0"),
        )

