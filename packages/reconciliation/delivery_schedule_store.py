import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator, List, Optional

from packages.domain.models import (
    DeliverySchedule,
    DeliveryScheduleStatus,
    EvidenceDocument,
    EvidenceDocumentLine,
    DeliveryValidationRun,
    DocumentType,
    PurchaseOrderItem,
    QuantityAllocation,
    QuantityAllocationType,
    ScheduleAmendment,
    ScheduleAmendmentStatus,
)
from packages.reconciliation.delivery_schedules import DeliveryScheduleReconciliationEngine


class ScheduleNotFoundError(LookupError):
    pass


class EvidenceNotFoundError(LookupError):
    pass


class DeliveryScheduleStore:
    """SQLite persistence for PO delivery schedules, allocations, and validation runs."""

    def __init__(self, database_path: Optional[str] = None):
        default_path = Path(__file__).resolve().parents[2] / "recorda.sqlite3"
        self.database_path = database_path or os.environ.get(
            "RECORDA_SCHEDULE_DB", str(default_path)
        )
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = DeliveryScheduleReconciliationEngine()
        self._initialize()

    @contextmanager
    def _connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS po_items (
                    po_item_id TEXT PRIMARY KEY,
                    po_reference TEXT NOT NULL,
                    line_item_id TEXT NOT NULL,
                    item_code TEXT,
                    description TEXT NOT NULL,
                    ordered_quantity TEXT NOT NULL,
                    unit TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    po_item_id TEXT NOT NULL REFERENCES po_items(po_item_id),
                    planned_quantity TEXT NOT NULL,
                    expected_delivery_date TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_amendments (
                    amendment_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL REFERENCES delivery_schedules(schedule_id),
                    previous_quantity TEXT NOT NULL,
                    previous_expected_date TEXT NOT NULL,
                    proposed_quantity TEXT NOT NULL,
                    proposed_expected_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS evidence_documents (
                    document_type TEXT NOT NULL CHECK (
                        document_type IN ('DELIVERY_CHALLAN', 'GRN')
                    ),
                    document_id TEXT NOT NULL,
                    document_number TEXT NOT NULL,
                    po_reference TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (document_type, document_id)
                );
                CREATE TABLE IF NOT EXISTS source_document_lines (
                    document_type TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    line_item_id TEXT NOT NULL,
                    line_quantity TEXT NOT NULL,
                    evidence_date TEXT NOT NULL,
                    PRIMARY KEY (document_type, document_id, line_item_id)
                );
                CREATE TABLE IF NOT EXISTS source_document_line_po_items (
                    document_type TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    line_item_id TEXT NOT NULL,
                    po_item_id TEXT NOT NULL REFERENCES po_items(po_item_id),
                    PRIMARY KEY (document_type, document_id, line_item_id, po_item_id),
                    FOREIGN KEY (document_type, document_id, line_item_id)
                        REFERENCES source_document_lines(
                            document_type, document_id, line_item_id
                        )
                );
                CREATE TABLE IF NOT EXISTS quantity_allocations (
                    allocation_id TEXT PRIMARY KEY,
                    allocation_type TEXT NOT NULL,
                    source_document_type TEXT NOT NULL,
                    source_document_id TEXT NOT NULL,
                    source_line_item_id TEXT NOT NULL,
                    po_item_id TEXT NOT NULL REFERENCES po_items(po_item_id),
                    schedule_id TEXT,
                    quantity TEXT NOT NULL,
                    matched_dispatch_allocation_id TEXT
                        REFERENCES quantity_allocations(allocation_id),
                    FOREIGN KEY (source_document_type, source_document_id, source_line_item_id)
                        REFERENCES source_document_lines(document_type, document_id, line_item_id),
                    FOREIGN KEY (schedule_id, po_item_id)
                        REFERENCES delivery_schedules(schedule_id, po_item_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_quantity_allocation_target
                    ON quantity_allocations(
                        allocation_type, source_document_type, source_document_id,
                        source_line_item_id, po_item_id, ifnull(schedule_id, '')
                    );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_schedule_item
                    ON delivery_schedules(schedule_id, po_item_id);
                CREATE TRIGGER IF NOT EXISTS validate_quantity_allocation_insert
                BEFORE INSERT ON quantity_allocations
                BEGIN
                    SELECT CASE WHEN NEW.allocation_type NOT IN ('DISPATCH', 'RECEIPT')
                        THEN RAISE(ABORT, 'Allocation type must be DISPATCH or RECEIPT') END;
                    SELECT CASE WHEN NEW.allocation_type = 'DISPATCH'
                        AND NEW.matched_dispatch_allocation_id IS NOT NULL
                        THEN RAISE(ABORT, 'Dispatch allocations cannot match another dispatch') END;
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM evidence_documents AS document
                        JOIN source_document_lines AS source_line
                            ON source_line.document_type = document.document_type
                            AND source_line.document_id = document.document_id
                        JOIN source_document_line_po_items AS line_item
                            ON line_item.document_type = source_line.document_type
                            AND line_item.document_id = source_line.document_id
                            AND line_item.line_item_id = source_line.line_item_id
                        JOIN po_items AS po_item
                            ON po_item.po_item_id = line_item.po_item_id
                        WHERE document.document_type = NEW.source_document_type
                            AND document.document_id = NEW.source_document_id
                            AND source_line.line_item_id = NEW.source_line_item_id
                            AND line_item.po_item_id = NEW.po_item_id
                            AND document.po_reference = po_item.po_reference
                            AND (
                                (NEW.allocation_type = 'DISPATCH'
                                    AND document.document_type = 'DELIVERY_CHALLAN')
                                OR (NEW.allocation_type = 'RECEIPT'
                                    AND document.document_type = 'GRN')
                            )
                    ) THEN RAISE(ABORT, 'Allocation source document, line, type, or PO item is not registered') END;
                    SELECT CASE WHEN NEW.matched_dispatch_allocation_id IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1
                            FROM quantity_allocations AS dispatch
                            JOIN source_document_lines AS dispatch_line
                                ON dispatch_line.document_type = dispatch.source_document_type
                                AND dispatch_line.document_id = dispatch.source_document_id
                                AND dispatch_line.line_item_id = dispatch.source_line_item_id
                            JOIN source_document_lines AS receipt_line
                                ON receipt_line.document_type = NEW.source_document_type
                                AND receipt_line.document_id = NEW.source_document_id
                                AND receipt_line.line_item_id = NEW.source_line_item_id
                            WHERE dispatch.allocation_id = NEW.matched_dispatch_allocation_id
                                AND dispatch.allocation_type = 'DISPATCH'
                                AND dispatch.po_item_id = NEW.po_item_id
                                AND dispatch.schedule_id IS NEW.schedule_id
                                AND dispatch_line.evidence_date <= receipt_line.evidence_date
                        ) THEN RAISE(ABORT, 'Matched dispatch allocation is invalid') END;
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_evidence_document_update
                BEFORE UPDATE ON evidence_documents
                BEGIN
                    SELECT RAISE(ABORT, 'Evidence document registrations are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_evidence_document_delete
                BEFORE DELETE ON evidence_documents
                BEGIN
                    SELECT RAISE(ABORT, 'Evidence document registrations are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_source_line_update
                BEFORE UPDATE ON source_document_lines
                BEGIN
                    SELECT RAISE(ABORT, 'Registered evidence lines are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_source_line_delete
                BEFORE DELETE ON source_document_lines
                BEGIN
                    SELECT RAISE(ABORT, 'Registered evidence lines are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_source_line_mapping_update
                BEFORE UPDATE ON source_document_line_po_items
                BEGIN
                    SELECT RAISE(ABORT, 'Evidence line PO mappings are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_source_line_mapping_delete
                BEFORE DELETE ON source_document_line_po_items
                BEGIN
                    SELECT RAISE(ABORT, 'Evidence line PO mappings are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_allocation_update
                BEFORE UPDATE ON quantity_allocations
                BEGIN
                    SELECT RAISE(ABORT, 'Quantity allocations are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_allocation_delete
                BEFORE DELETE ON quantity_allocations
                BEGIN
                    SELECT RAISE(ABORT, 'Quantity allocations are immutable');
                END;
                CREATE TABLE IF NOT EXISTS validation_runs (
                    run_id TEXT PRIMARY KEY,
                    po_item_id TEXT NOT NULL REFERENCES po_items(po_item_id),
                    evaluation_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                """
            )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS preserve_validation_run_update
                BEFORE UPDATE ON validation_runs
                BEGIN
                    SELECT RAISE(ABORT, 'Validation runs are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_validation_run_delete
                BEFORE DELETE ON validation_runs
                BEGIN
                    SELECT RAISE(ABORT, 'Validation runs are immutable');
                END;
                """
            )
            allocation_columns = {
                row["name"]
                for row in connection.execute(
                "PRAGMA table_info(quantity_allocations)"
                ).fetchall()
            }
            if "matched_dispatch_allocation_id" not in allocation_columns:
                connection.execute(
                "ALTER TABLE quantity_allocations "
                "ADD COLUMN matched_dispatch_allocation_id TEXT "
                "REFERENCES quantity_allocations(allocation_id)"
                )

    def register_evidence_document(
        self, document: EvidenceDocument
    ) -> EvidenceDocument:
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

        with self._connection(write=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM evidence_documents
                WHERE document_type = ? AND document_id = ?
                """,
                (document.document_type.value, document.document_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["document_number"] != document.document_number
                    or existing["po_reference"] != document.po_reference
                    or existing["sha256"] != document.sha256
                ):
                    raise ValueError(
                        "Registered evidence document identity is immutable."
                    )
                registered_lines = self._evidence_lines_for_document(
                    connection, document.document_type, document.document_id
                )
                def line_signature(line: EvidenceDocumentLine):
                    return (
                        line.quantity,
                        line.evidence_date,
                        frozenset(line.po_item_ids),
                    )

                if {
                    line.line_item_id: line_signature(line)
                    for line in registered_lines
                } != {
                    line.line_item_id: line_signature(line)
                    for line in document.lines
                }:
                    raise ValueError(
                        "Registered evidence document lines are immutable."
                    )
            else:
                legacy_line_ids = {
                    row["line_item_id"]
                    for row in connection.execute(
                        """
                        SELECT line_item_id FROM source_document_lines
                        WHERE document_type = ? AND document_id = ?
                        """,
                        (document.document_type.value, document.document_id),
                    ).fetchall()
                }
                if not legacy_line_ids.issubset(set(line_ids)):
                    raise ValueError(
                        "Evidence registration cannot omit existing source lines."
                    )
                fingerprint_owner = connection.execute(
                    "SELECT document_id FROM evidence_documents WHERE sha256 = ?",
                    (document.sha256,),
                ).fetchone()
                if fingerprint_owner is not None:
                    raise ValueError(
                        "This document fingerprint is already registered as "
                        f"{fingerprint_owner['document_id']}."
                    )
                connection.execute(
                    """
                    INSERT INTO evidence_documents
                        (document_type, document_id, document_number, po_reference, sha256)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        document.document_type.value,
                        document.document_id,
                        document.document_number,
                        document.po_reference,
                        document.sha256,
                    ),
                )

            for line in document.lines:
                for po_item_id in line.po_item_ids:
                    po_item = self._get_po_item(connection, po_item_id)
                    if po_item.po_reference != document.po_reference:
                        raise ValueError(
                            "Evidence line PO items must belong to the document's PO."
                        )
                existing_line = connection.execute(
                    """
                    SELECT line_quantity, evidence_date FROM source_document_lines
                    WHERE document_type = ? AND document_id = ? AND line_item_id = ?
                    """,
                    (
                        document.document_type.value,
                        document.document_id,
                        line.line_item_id,
                    ),
                ).fetchone()
                if existing_line is None:
                    connection.execute(
                        """
                        INSERT INTO source_document_lines
                            (document_type, document_id, line_item_id,
                             line_quantity, evidence_date)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            document.document_type.value,
                            document.document_id,
                            line.line_item_id,
                            str(line.quantity),
                            line.evidence_date.isoformat(),
                        ),
                    )
                elif (
                    Decimal(existing_line["line_quantity"]) != line.quantity
                    or date.fromisoformat(existing_line["evidence_date"])
                    != line.evidence_date
                ):
                    raise ValueError(
                        "Registered evidence document lines are immutable."
                    )
                existing_mappings = {
                    row["po_item_id"]
                    for row in connection.execute(
                        """
                        SELECT po_item_id FROM source_document_line_po_items
                        WHERE document_type = ? AND document_id = ? AND line_item_id = ?
                        """,
                        (
                            document.document_type.value,
                            document.document_id,
                            line.line_item_id,
                        ),
                    ).fetchall()
                }
                if existing_mappings and existing_mappings != set(line.po_item_ids):
                    raise ValueError(
                        "Registered evidence line PO item mappings are immutable."
                    )
                for po_item_id in line.po_item_ids:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO source_document_line_po_items
                            (document_type, document_id, line_item_id, po_item_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            document.document_type.value,
                            document.document_id,
                            line.line_item_id,
                            po_item_id,
                        ),
                    )
        return document

    def get_evidence_document(
        self, document_type: DocumentType, document_id: str
    ) -> EvidenceDocument:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM evidence_documents
                WHERE document_type = ? AND document_id = ?
                """,
                (document_type.value, document_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(
                    f"Evidence document {document_id} is not registered."
                )
            return EvidenceDocument(
                document_id=row["document_id"],
                document_type=row["document_type"],
                document_number=row["document_number"],
                po_reference=row["po_reference"],
                sha256=row["sha256"],
                lines=self._evidence_lines_for_document(
                    connection, document_type, document_id
                ),
            )

    @staticmethod
    def _evidence_lines_for_document(
        connection: sqlite3.Connection,
        document_type: DocumentType,
        document_id: str,
    ) -> List[EvidenceDocumentLine]:
        rows = connection.execute(
            """
            SELECT source_line.* FROM source_document_lines AS source_line
            WHERE source_line.document_type = ? AND source_line.document_id = ?
            ORDER BY source_line.rowid
            """,
            (document_type.value, document_id),
        ).fetchall()
        lines = []
        for row in rows:
            mappings = connection.execute(
                """
                SELECT po_item_id FROM source_document_line_po_items
                WHERE document_type = ? AND document_id = ? AND line_item_id = ?
                ORDER BY rowid
                """,
                (document_type.value, document_id, row["line_item_id"]),
            ).fetchall()
            lines.append(
                EvidenceDocumentLine(
                    line_item_id=row["line_item_id"],
                    quantity=Decimal(row["line_quantity"]),
                    evidence_date=date.fromisoformat(row["evidence_date"]),
                    po_item_ids=[mapping["po_item_id"] for mapping in mappings],
                )
            )
        return lines

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
        item = PurchaseOrderItem(
            po_item_id=po_item_id or str(uuid.uuid4()),
            po_reference=po_reference,
            line_item_id=line_item_id,
            item_code=item_code,
            description=description,
            ordered_quantity=ordered_quantity,
            unit=unit,
        )
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO po_items
                    (po_item_id, po_reference, line_item_id, item_code, description,
                     ordered_quantity, unit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.po_item_id,
                    item.po_reference,
                    item.line_item_id,
                    item.item_code,
                    item.description,
                    str(item.ordered_quantity),
                    item.unit,
                ),
            )
        return item

    def get_po_item(self, po_item_id: str) -> PurchaseOrderItem:
        with self._connection() as connection:
            return self._get_po_item(connection, po_item_id)

    def create_schedule(
        self,
        po_item_id: str,
        planned_quantity: Decimal,
        expected_delivery_date: date,
    ) -> DeliverySchedule:
        schedule = DeliverySchedule(
            schedule_id=str(uuid.uuid4()),
            po_item_id=po_item_id,
            planned_quantity=planned_quantity,
            expected_delivery_date=expected_delivery_date,
        )
        with self._connection(write=True) as connection:
            item = self._get_po_item(connection, po_item_id)
            allocated = self._scheduled_quantity(connection, po_item_id)
            if allocated + planned_quantity > item.ordered_quantity:
                raise ValueError("Scheduled quantity cannot exceed the PO item quantity.")
            connection.execute(
                """
                INSERT INTO delivery_schedules
                    (schedule_id, po_item_id, planned_quantity, expected_delivery_date, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    schedule.schedule_id,
                    po_item_id,
                    str(planned_quantity),
                    expected_delivery_date.isoformat(),
                    schedule.status.value,
                ),
            )
        return schedule

    def list_schedules(self, po_item_id: str) -> List[DeliverySchedule]:
        with self._connection() as connection:
            self._get_po_item(connection, po_item_id)
            rows = connection.execute(
                "SELECT * FROM delivery_schedules WHERE po_item_id = ? ORDER BY rowid",
                (po_item_id,),
            ).fetchall()
            return [self._schedule_from_row(connection, row) for row in rows]

    def propose_amendment(
        self,
        schedule_id: str,
        proposed_quantity: Decimal,
        proposed_expected_date: date,
        requested_by: str,
    ) -> ScheduleAmendment:
        with self._connection(write=True) as connection:
            schedule = self._get_schedule(connection, schedule_id)
            if schedule.status == DeliveryScheduleStatus.CANCELLED:
                raise ValueError("A cancelled schedule cannot be amended.")
            amendment = ScheduleAmendment(
                amendment_id=str(uuid.uuid4()),
                schedule_id=schedule_id,
                previous_quantity=schedule.planned_quantity,
                previous_expected_date=schedule.expected_delivery_date,
                proposed_quantity=proposed_quantity,
                proposed_expected_date=proposed_expected_date,
                requested_by=requested_by,
            )
            connection.execute(
                """
                INSERT INTO schedule_amendments
                    (amendment_id, schedule_id, previous_quantity,
                     previous_expected_date, proposed_quantity, proposed_expected_date,
                     status, requested_by, requested_at, approved_by, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    amendment.amendment_id,
                    schedule_id,
                    str(amendment.previous_quantity),
                    amendment.previous_expected_date.isoformat(),
                    str(amendment.proposed_quantity),
                    amendment.proposed_expected_date.isoformat(),
                    amendment.status.value,
                    amendment.requested_by,
                    amendment.requested_at.isoformat(),
                ),
            )
            return amendment

    def approve_amendment(
        self, amendment_id: str, approved_by: str
    ) -> ScheduleAmendment:
        with self._connection(write=True) as connection:
            amendment_row = connection.execute(
                "SELECT * FROM schedule_amendments WHERE amendment_id = ?",
                (amendment_id,),
            ).fetchone()
            if amendment_row is None:
                raise ScheduleNotFoundError(f"Amendment {amendment_id} was not found.")
            amendment = self._amendment_from_row(amendment_row)
            if amendment.status != ScheduleAmendmentStatus.PENDING_APPROVAL:
                raise ValueError("Only pending schedule amendments can be approved.")

            schedule_row = connection.execute(
                "SELECT * FROM delivery_schedules WHERE schedule_id = ?",
                (amendment.schedule_id,),
            ).fetchone()
            if schedule_row is None:
                raise ScheduleNotFoundError(
                    f"Schedule {amendment.schedule_id} was not found."
                )
            schedule = self._schedule_from_row(connection, schedule_row)
            if schedule.status == DeliveryScheduleStatus.CANCELLED:
                raise ValueError("A cancelled schedule cannot be amended.")
            if (
                schedule.planned_quantity != amendment.previous_quantity
                or schedule.expected_delivery_date != amendment.previous_expected_date
            ):
                raise ValueError(
                    "This amendment is based on an outdated schedule; submit a new amendment."
                )

            item = self._get_po_item(connection, schedule.po_item_id)
            current_total = self._scheduled_quantity(connection, schedule.po_item_id)
            if current_total - schedule.planned_quantity + amendment.proposed_quantity > item.ordered_quantity:
                raise ValueError("Amended schedules cannot exceed the PO item quantity.")

            already_dispatched = self._schedule_allocation_quantity(
                connection, schedule.schedule_id, QuantityAllocationType.DISPATCH
            )
            accepted = self._schedule_receipt_quantity(connection, schedule.schedule_id)
            if max(already_dispatched, accepted) > amendment.proposed_quantity:
                raise ValueError(
                    "An amendment cannot reduce the schedule below quantities already evidenced."
                )
            if accepted >= amendment.proposed_quantity:
                status = DeliveryScheduleStatus.FULFILLED
            elif accepted > 0:
                status = DeliveryScheduleStatus.PARTIALLY_FULFILLED
            else:
                status = DeliveryScheduleStatus.SCHEDULED
            approved_at = datetime.now(timezone.utc)
            connection.execute(
                """
                UPDATE delivery_schedules
                SET planned_quantity = ?, expected_delivery_date = ?, status = ?
                WHERE schedule_id = ?
                """,
                (
                    str(amendment.proposed_quantity),
                    amendment.proposed_expected_date.isoformat(),
                    status.value,
                    amendment.schedule_id,
                ),
            )
            connection.execute(
                """
                UPDATE schedule_amendments
                SET status = ?, approved_by = ?, approved_at = ?
                WHERE amendment_id = ?
                """,
                (
                    ScheduleAmendmentStatus.APPROVED.value,
                    approved_by,
                    approved_at.isoformat(),
                    amendment_id,
                ),
            )
            return amendment.model_copy(
                update={
                    "status": ScheduleAmendmentStatus.APPROVED,
                    "approved_by": approved_by,
                    "approved_at": approved_at,
                }
            )

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
            raise ValueError("Dispatch allocations cannot link to another dispatch allocation.")

        with self._connection(write=True) as connection:
            po_item = self._get_po_item(connection, allocation.po_item_id)
            evidence = connection.execute(
                """
                SELECT document.po_reference, source_line.line_quantity,
                    source_line.evidence_date, line_item.po_item_id
                FROM evidence_documents AS document
                JOIN source_document_lines AS source_line
                    ON source_line.document_type = document.document_type
                    AND source_line.document_id = document.document_id
                JOIN source_document_line_po_items AS line_item
                    ON line_item.document_type = source_line.document_type
                    AND line_item.document_id = source_line.document_id
                    AND line_item.line_item_id = source_line.line_item_id
                WHERE document.document_type = ? AND document.document_id = ?
                    AND source_line.line_item_id = ? AND line_item.po_item_id = ?
                """,
                (
                    allocation.source_document_type.value,
                    allocation.source_document_id,
                    allocation.source_line_item_id,
                    allocation.po_item_id,
                ),
            ).fetchone()
            if evidence is None:
                document_exists = connection.execute(
                    """
                    SELECT 1 FROM evidence_documents
                    WHERE document_type = ? AND document_id = ?
                    """,
                    (
                        allocation.source_document_type.value,
                        allocation.source_document_id,
                    ),
                ).fetchone()
                if document_exists is None:
                    raise EvidenceNotFoundError(
                        f"Evidence document {allocation.source_document_id} is not registered."
                    )
                line_exists = connection.execute(
                    """
                    SELECT 1 FROM source_document_lines
                    WHERE document_type = ? AND document_id = ? AND line_item_id = ?
                    """,
                    (
                        allocation.source_document_type.value,
                        allocation.source_document_id,
                        allocation.source_line_item_id,
                    ),
                ).fetchone()
                if line_exists is None:
                    raise EvidenceNotFoundError(
                        f"Evidence line {allocation.source_line_item_id} is not registered on "
                        f"document {allocation.source_document_id}."
                    )
                raise ValueError("Evidence line is not mapped to this PO item.")
            if evidence["po_reference"] != po_item.po_reference:
                raise ValueError("Evidence document belongs to a different purchase order.")
            if (
                Decimal(evidence["line_quantity"]) != allocation.source_line_quantity
                or date.fromisoformat(evidence["evidence_date"])
                != allocation.evidence_date
            ):
                raise ValueError(
                    "Allocation quantity and date must match registered evidence."
                )

            if allocation.schedule_id:
                schedule = connection.execute(
                    """
                    SELECT status FROM delivery_schedules
                    WHERE schedule_id = ? AND po_item_id = ?
                    """,
                    (allocation.schedule_id, allocation.po_item_id),
                ).fetchone()
                if schedule is None:
                    raise ValueError("The delivery schedule does not belong to this PO item.")
                if schedule["status"] == DeliveryScheduleStatus.CANCELLED.value:
                    raise ValueError("Allocations cannot be made to a cancelled schedule.")

            duplicate = connection.execute(
                """
                SELECT * FROM quantity_allocations
                WHERE allocation_type = ? AND source_document_type = ?
                    AND source_document_id = ? AND source_line_item_id = ?
                    AND po_item_id = ? AND ifnull(schedule_id, '') = ifnull(?, '')
                """,
                (
                    allocation.allocation_type.value,
                    allocation.source_document_type.value,
                    allocation.source_document_id,
                    allocation.source_line_item_id,
                    allocation.po_item_id,
                    allocation.schedule_id,
                ),
            ).fetchone()
            if duplicate:
                if (
                    Decimal(duplicate["quantity"]) == allocation.quantity
                    and duplicate["matched_dispatch_allocation_id"]
                    == allocation.matched_dispatch_allocation_id
                ):
                    return self._allocation_from_row(connection, duplicate)
                raise ValueError(
                    "This source line already has a different allocation to that PO item and schedule."
                )

            if allocation.matched_dispatch_allocation_id:
                dispatch = connection.execute(
                    """
                    SELECT allocation.quantity, allocation.po_item_id,
                        allocation.schedule_id, allocation.allocation_type,
                        source_line.evidence_date
                    FROM quantity_allocations AS allocation
                    JOIN source_document_lines AS source_line
                        ON source_line.document_type = allocation.source_document_type
                        AND source_line.document_id = allocation.source_document_id
                        AND source_line.line_item_id = allocation.source_line_item_id
                    WHERE allocation.allocation_id = ?
                    """,
                    (allocation.matched_dispatch_allocation_id,),
                ).fetchone()
                if (
                    dispatch is None
                    or dispatch["allocation_type"] != QuantityAllocationType.DISPATCH.value
                    or dispatch["po_item_id"] != allocation.po_item_id
                    or dispatch["schedule_id"] != allocation.schedule_id
                ):
                    raise ValueError(
                        "A receipt must link to a dispatch allocation for the same PO item and schedule."
                    )
                if (
                    date.fromisoformat(dispatch["evidence_date"])
                    > allocation.evidence_date
                ):
                    raise ValueError("A receipt cannot predate its matched dispatch.")
                matched_quantity = sum(
                    (
                        Decimal(row["quantity"])
                        for row in connection.execute(
                            """
                            SELECT quantity FROM quantity_allocations
                            WHERE matched_dispatch_allocation_id = ?
                            """,
                            (allocation.matched_dispatch_allocation_id,),
                        ).fetchall()
                    ),
                    Decimal("0"),
                )
                if matched_quantity + allocation.quantity > Decimal(dispatch["quantity"]):
                    raise ValueError(
                        "Matched receipts cannot exceed the dispatch allocation quantity."
                    )

            exact_allocated = sum(
                (
                    Decimal(row["quantity"])
                    for row in connection.execute(
                        """
                        SELECT quantity FROM quantity_allocations
                        WHERE source_document_type = ? AND source_document_id = ?
                            AND source_line_item_id = ?
                        """,
                        (
                            allocation.source_document_type.value,
                            allocation.source_document_id,
                            allocation.source_line_item_id,
                        ),
                    ).fetchall()
                ),
                Decimal("0"),
            )
            if exact_allocated + allocation.quantity > allocation.source_line_quantity:
                raise ValueError("Allocations cannot exceed the source document line quantity.")

            if allocation.schedule_id:
                schedule_row = connection.execute(
                    """
                    SELECT planned_quantity FROM delivery_schedules
                    WHERE schedule_id = ?
                    """,
                    (allocation.schedule_id,),
                ).fetchone()
                schedule_allocated = self._schedule_allocation_quantity(
                    connection, allocation.schedule_id, allocation.allocation_type
                )
                if schedule_allocated + allocation.quantity > Decimal(
                    schedule_row["planned_quantity"]
                ):
                    raise ValueError(
                        "Scheduled allocations cannot exceed the planned schedule quantity; "
                        "allocate the remaining evidence as unscheduled."
                    )

            connection.execute(
                """
                INSERT INTO quantity_allocations
                    (allocation_id, allocation_type, source_document_type,
                     source_document_id, source_line_item_id, po_item_id,
                     schedule_id, quantity, matched_dispatch_allocation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    allocation.allocation_id,
                    allocation.allocation_type.value,
                    allocation.source_document_type.value,
                    allocation.source_document_id,
                    allocation.source_line_item_id,
                    allocation.po_item_id,
                    allocation.schedule_id,
                    str(allocation.quantity),
                    allocation.matched_dispatch_allocation_id,
                ),
            )

            if (
                allocation.allocation_type == QuantityAllocationType.RECEIPT
                and allocation.schedule_id
            ):
                scheduled = connection.execute(
                    "SELECT planned_quantity FROM delivery_schedules WHERE schedule_id = ?",
                    (allocation.schedule_id,),
                ).fetchone()
                accepted = self._schedule_receipt_quantity(
                    connection, allocation.schedule_id
                )
                planned_quantity = Decimal(scheduled["planned_quantity"])
                if accepted >= planned_quantity:
                    status = DeliveryScheduleStatus.FULFILLED
                else:
                    status = DeliveryScheduleStatus.PARTIALLY_FULFILLED
                connection.execute(
                    "UPDATE delivery_schedules SET status = ? WHERE schedule_id = ?",
                    (status.value, allocation.schedule_id),
                )
        return allocation

    def list_allocations(self, po_item_id: str) -> List[QuantityAllocation]:
        with self._connection() as connection:
            self._get_po_item(connection, po_item_id)
            rows = connection.execute(
                """
                SELECT allocation.* FROM quantity_allocations AS allocation
                JOIN evidence_documents AS document
                    ON document.document_type = allocation.source_document_type
                    AND document.document_id = allocation.source_document_id
                JOIN source_document_line_po_items AS line_item
                    ON line_item.document_type = allocation.source_document_type
                    AND line_item.document_id = allocation.source_document_id
                    AND line_item.line_item_id = allocation.source_line_item_id
                    AND line_item.po_item_id = allocation.po_item_id
                WHERE allocation.po_item_id = ? ORDER BY allocation.rowid
                """,
                (po_item_id,),
            ).fetchall()
            return [self._allocation_from_row(connection, row) for row in rows]

    def run_reconciliation(
        self, po_item_id: str, evaluation_date: date
    ) -> DeliveryValidationRun:
        with self._connection(write=True) as connection:
            item = self._get_po_item(connection, po_item_id)
            schedules = [
                self._schedule_from_row(connection, row)
                for row in connection.execute(
                    "SELECT * FROM delivery_schedules WHERE po_item_id = ? ORDER BY rowid",
                    (po_item_id,),
                ).fetchall()
            ]
            allocations = self._allocations_for_item(connection, po_item_id)
            summary, findings = self.engine.reconcile(
                item, schedules, allocations, evaluation_date
            )
            run = DeliveryValidationRun(
                run_id=str(uuid.uuid4()),
                po_item_id=po_item_id,
                evaluation_date=evaluation_date,
                summary=summary,
                findings=findings,
            )
            connection.execute(
                """
                INSERT INTO validation_runs
                    (run_id, po_item_id, evaluation_date, created_at, result_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    po_item_id,
                    evaluation_date.isoformat(),
                    run.created_at.isoformat(),
                    json.dumps(run.model_dump(mode="json")),
                ),
            )
            return run

    def list_validation_runs(self, po_item_id: str) -> List[DeliveryValidationRun]:
        with self._connection() as connection:
            self._get_po_item(connection, po_item_id)
            rows = connection.execute(
                """
                SELECT result_json FROM validation_runs
                WHERE po_item_id = ? ORDER BY rowid
                """,
                (po_item_id,),
            ).fetchall()
            return [
                DeliveryValidationRun.model_validate(json.loads(row["result_json"]))
                for row in rows
            ]

    def _get_po_item(
        self, connection: sqlite3.Connection, po_item_id: str
    ) -> PurchaseOrderItem:
        row = connection.execute(
            "SELECT * FROM po_items WHERE po_item_id = ?", (po_item_id,)
        ).fetchone()
        if row is None:
            raise ScheduleNotFoundError(f"PO item {po_item_id} was not found.")
        return PurchaseOrderItem(
            po_item_id=row["po_item_id"],
            po_reference=row["po_reference"],
            line_item_id=row["line_item_id"],
            item_code=row["item_code"],
            description=row["description"],
            ordered_quantity=Decimal(row["ordered_quantity"]),
            unit=row["unit"],
        )

    def _get_schedule(
        self, connection: sqlite3.Connection, schedule_id: str
    ) -> DeliverySchedule:
        row = connection.execute(
            "SELECT * FROM delivery_schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        if row is None:
            raise ScheduleNotFoundError(f"Schedule {schedule_id} was not found.")
        return self._schedule_from_row(connection, row)

    def _schedule_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> DeliverySchedule:
        amendments = connection.execute(
            "SELECT * FROM schedule_amendments WHERE schedule_id = ? ORDER BY rowid",
            (row["schedule_id"],),
        ).fetchall()
        return DeliverySchedule(
            schedule_id=row["schedule_id"],
            po_item_id=row["po_item_id"],
            planned_quantity=Decimal(row["planned_quantity"]),
            expected_delivery_date=date.fromisoformat(row["expected_delivery_date"]),
            status=row["status"],
            amendment_history=[
                self._amendment_from_row(amendment) for amendment in amendments
            ],
        )

    @staticmethod
    def _amendment_from_row(row: sqlite3.Row) -> ScheduleAmendment:
        return ScheduleAmendment(
            amendment_id=row["amendment_id"],
            schedule_id=row["schedule_id"],
            previous_quantity=Decimal(row["previous_quantity"]),
            previous_expected_date=date.fromisoformat(row["previous_expected_date"]),
            proposed_quantity=Decimal(row["proposed_quantity"]),
            proposed_expected_date=date.fromisoformat(row["proposed_expected_date"]),
            status=row["status"],
            requested_by=row["requested_by"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            approved_by=row["approved_by"],
            approved_at=(
                datetime.fromisoformat(row["approved_at"])
                if row["approved_at"]
                else None
            ),
        )

    def _allocations_for_item(
        self, connection: sqlite3.Connection, po_item_id: str
    ) -> List[QuantityAllocation]:
        rows = connection.execute(
            """
            SELECT allocation.* FROM quantity_allocations AS allocation
            JOIN evidence_documents AS document
                ON document.document_type = allocation.source_document_type
                AND document.document_id = allocation.source_document_id
            JOIN source_document_line_po_items AS line_item
                ON line_item.document_type = allocation.source_document_type
                AND line_item.document_id = allocation.source_document_id
                AND line_item.line_item_id = allocation.source_line_item_id
                AND line_item.po_item_id = allocation.po_item_id
            WHERE allocation.po_item_id = ? ORDER BY allocation.rowid
            """,
            (po_item_id,),
        ).fetchall()
        return [self._allocation_from_row(connection, row) for row in rows]

    def _allocation_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> QuantityAllocation:
        source_line = connection.execute(
            """
            SELECT line_quantity, evidence_date FROM source_document_lines
            WHERE document_type = ? AND document_id = ? AND line_item_id = ?
            """,
            (
                row["source_document_type"],
                row["source_document_id"],
                row["source_line_item_id"],
            ),
        ).fetchone()
        return QuantityAllocation(
            allocation_id=row["allocation_id"],
            allocation_type=row["allocation_type"],
            source_document_type=row["source_document_type"],
            source_document_id=row["source_document_id"],
            source_line_item_id=row["source_line_item_id"],
            source_line_quantity=Decimal(source_line["line_quantity"]),
            evidence_date=date.fromisoformat(source_line["evidence_date"]),
            po_item_id=row["po_item_id"],
            schedule_id=row["schedule_id"],
            quantity=Decimal(row["quantity"]),
            matched_dispatch_allocation_id=row[
                "matched_dispatch_allocation_id"
            ],
        )

    @staticmethod
    def _scheduled_quantity(
        connection: sqlite3.Connection, po_item_id: str
    ) -> Decimal:
        quantities = connection.execute(
            """
            SELECT planned_quantity FROM delivery_schedules
            WHERE po_item_id = ? AND status != ?
            """,
            (po_item_id, DeliveryScheduleStatus.CANCELLED.value),
        ).fetchall()
        return sum(
            (Decimal(row["planned_quantity"]) for row in quantities), Decimal("0")
        )

    @staticmethod
    def _schedule_receipt_quantity(
        connection: sqlite3.Connection, schedule_id: str
    ) -> Decimal:
        rows = connection.execute(
            """
            SELECT allocation.quantity FROM quantity_allocations AS allocation
            JOIN evidence_documents AS document
                ON document.document_type = allocation.source_document_type
                AND document.document_id = allocation.source_document_id
            JOIN source_document_line_po_items AS line_item
                ON line_item.document_type = allocation.source_document_type
                AND line_item.document_id = allocation.source_document_id
                AND line_item.line_item_id = allocation.source_line_item_id
                AND line_item.po_item_id = allocation.po_item_id
            WHERE allocation.schedule_id = ? AND allocation.allocation_type = ?
            """,
            (schedule_id, QuantityAllocationType.RECEIPT.value),
        ).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))

    @staticmethod
    def _schedule_allocation_quantity(
        connection: sqlite3.Connection,
        schedule_id: str,
        allocation_type: QuantityAllocationType,
    ) -> Decimal:
        rows = connection.execute(
            """
            SELECT quantity FROM quantity_allocations
            WHERE schedule_id = ? AND allocation_type = ?
            """,
            (schedule_id, allocation_type.value),
        ).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))
