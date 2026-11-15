"""Exception management service for persisted reconciliation discrepancies.

Creates durable exception records from reconciliation discrepancies, enforces a
strict lifecycle, and maintains an immutable audit trail. The original
reconciliation run and discrepancy rows are never modified.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.models import (
    Document,
    DocumentVerification,
    ReconciliationDiscrepancy,
    ReconciliationException,
    ReconciliationExceptionHistory,
    ReconciliationRun,
)


class ExceptionManagementError(Exception):
    """Base exception for reconciliation exception operations."""
    pass


class ReconciliationExceptionNotFoundError(ExceptionManagementError):
    """Raised when an exception cannot be found."""
    pass


class ReconciliationRunNotFoundError(ExceptionManagementError):
    """Raised when a reconciliation run cannot be found."""
    pass


class DiscrepancyNotFoundError(ExceptionManagementError):
    """Raised when a reconciliation discrepancy cannot be found."""
    pass


class InvalidExceptionStatusError(ExceptionManagementError):
    """Raised when an unsupported exception status is supplied."""
    pass


class InvalidExceptionTransitionError(ExceptionManagementError):
    """Raised when a lifecycle transition is not permitted."""
    pass


class InvalidResolutionError(ExceptionManagementError):
    """Raised when a resolution is attempted without resolution notes."""
    pass


class ReconciliationExceptionService:
    """Manage persisted exceptions derived from reconciliation discrepancies."""

    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    REOPENED = "REOPENED"

    VALID_STATUSES = frozenset(
        {OPEN, UNDER_REVIEW, RESOLVED, REJECTED, REOPENED}
    )
    VALID_TRANSITIONS = {
        OPEN: frozenset({UNDER_REVIEW}),
        UNDER_REVIEW: frozenset({RESOLVED, REJECTED}),
        RESOLVED: frozenset({REOPENED}),
        REJECTED: frozenset({REOPENED}),
        REOPENED: frozenset({UNDER_REVIEW, RESOLVED, REJECTED}),
    }

    def __init__(self, db: Session):
        self.db = db

    def create_from_reconciliation_run(
        self,
        reconciliation_run_id: uuid.UUID,
        actor: str = "SYSTEM",
    ) -> List[ReconciliationException]:
        """Create exceptions for every discrepancy in a run, without duplicates."""
        run = self.db.get(ReconciliationRun, reconciliation_run_id)
        if run is None:
            raise ReconciliationRunNotFoundError(
                f"Reconciliation run '{reconciliation_run_id}' was not found."
            )

        actor = self._normalize_actor(actor)
        expected_discrepancy_ids = {d.id for d in run.discrepancies}
        existing_discrepancy_ids = set(
            self.db.execute(
                select(ReconciliationException.discrepancy_id).where(
                    ReconciliationException.reconciliation_run_id
                    == reconciliation_run_id
                )
            ).scalars()
        )

        created_any = False
        for discrepancy in run.discrepancies:
            if discrepancy.id in existing_discrepancy_ids:
                continue
            exception = self._build_exception(discrepancy, run, actor)
            self.db.add(exception)
            created_any = True

        if created_any:
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                existing = self.list_exceptions(
                    reconciliation_run_id=reconciliation_run_id
                )
                covered_discrepancy_ids = {
                    exception.discrepancy_id for exception in existing
                }
                if not expected_discrepancy_ids.issubset(covered_discrepancy_ids):
                    raise
                return existing
            except SQLAlchemyError:
                self.db.rollback()
                raise

        return self.list_exceptions(reconciliation_run_id=reconciliation_run_id)

    def create_from_discrepancy(
        self,
        discrepancy_id: uuid.UUID,
        actor: str = "SYSTEM",
    ) -> ReconciliationException:
        """Create (or return the existing) exception for a single discrepancy."""
        discrepancy = self.db.get(ReconciliationDiscrepancy, discrepancy_id)
        if discrepancy is None:
            raise DiscrepancyNotFoundError(
                f"Reconciliation discrepancy '{discrepancy_id}' was not found."
            )

        existing = self._get_by_discrepancy_id(discrepancy_id)
        if existing is not None:
            return existing

        run = self.db.get(ReconciliationRun, discrepancy.run_id)
        if run is None:
            raise ReconciliationRunNotFoundError(
                f"Reconciliation run '{discrepancy.run_id}' was not found."
            )

        exception = self._build_exception(
            discrepancy, run, self._normalize_actor(actor)
        )
        self.db.add(exception)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self._get_by_discrepancy_id(discrepancy_id)
            if existing is not None:
                return existing
            raise
        except SQLAlchemyError:
            self.db.rollback()
            raise

        self.db.refresh(exception)
        return exception

    def list_exceptions(
        self,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        exception_type: Optional[str] = None,
        supplier_name: Optional[str] = None,
        po_reference: Optional[str] = None,
        reconciliation_run_id: Optional[uuid.UUID] = None,
        discrepancy_id: Optional[uuid.UUID] = None,
    ) -> List[ReconciliationException]:
        """List exceptions with optional filters, newest first."""
        query = select(ReconciliationException)

        if status:
            query = query.where(
                ReconciliationException.status == status.strip().upper()
            )
        if severity:
            query = query.where(
                ReconciliationException.severity == severity.strip().upper()
            )
        if exception_type:
            query = query.where(
                ReconciliationException.exception_type
                == exception_type.strip().upper()
            )
        if supplier_name:
            query = query.where(
                ReconciliationException.supplier_name.ilike(
                    f"%{supplier_name.strip()}%"
                )
            )
        if po_reference:
            query = query.where(
                ReconciliationException.po_reference.ilike(
                    f"%{po_reference.strip()}%"
                )
            )
        if reconciliation_run_id:
            query = query.where(
                ReconciliationException.reconciliation_run_id
                == reconciliation_run_id
            )
        if discrepancy_id:
            query = query.where(
                ReconciliationException.discrepancy_id == discrepancy_id
            )

        query = query.order_by(ReconciliationException.created_at.desc())
        return list(self.db.execute(query).scalars().all())

    def get_exception(
        self, exception_id: uuid.UUID
    ) -> ReconciliationException:
        """Retrieve a single exception or raise a not-found error."""
        exception = self.db.get(ReconciliationException, exception_id)
        if exception is None:
            raise ReconciliationExceptionNotFoundError(
                f"Reconciliation exception '{exception_id}' was not found."
            )
        return exception

    def get_history(
        self, exception_id: uuid.UUID
    ) -> List[ReconciliationExceptionHistory]:
        """Return the immutable audit history for an exception."""
        self.get_exception(exception_id)
        return list(
            self.db.execute(
                select(ReconciliationExceptionHistory)
                .where(
                    ReconciliationExceptionHistory.exception_id == exception_id
                )
                .order_by(
                    ReconciliationExceptionHistory.created_at.asc(),
                    ReconciliationExceptionHistory.id.asc(),
                )
            )
            .scalars()
            .all()
        )

    def update_status(
        self,
        exception_id: uuid.UUID,
        new_status: str,
        actor: str = "SYSTEM",
        note: Optional[str] = None,
        supporting_information: Optional[Dict[str, Any]] = None,
    ) -> ReconciliationException:
        """Apply a validated lifecycle transition and record history."""
        exception = self.get_exception(exception_id)
        requested_status = (new_status or "").strip().upper()
        if requested_status not in self.VALID_STATUSES:
            raise InvalidExceptionStatusError(
                f"Unsupported exception status '{new_status}'."
            )

        allowed = self.VALID_TRANSITIONS.get(exception.status, frozenset())
        if requested_status not in allowed:
            raise InvalidExceptionTransitionError(
                f"Cannot transition exception from '{exception.status}' "
                f"to '{requested_status}'."
            )

        previous_status = exception.status
        actor = self._normalize_actor(actor)
        now = datetime.now(timezone.utc)

        exception.status = requested_status
        if requested_status in {self.RESOLVED, self.REJECTED}:
            if note:
                exception.resolution_notes = note
            exception.resolution_supporting_information = self._as_json_dict(
                supporting_information
            )
            exception.resolved_by = actor
            exception.resolved_at = now
        elif requested_status == self.REOPENED:
            exception.resolved_by = None
            exception.resolved_at = None

        action = (
            requested_status
            if requested_status in {self.RESOLVED, self.REJECTED, self.REOPENED}
            else "STATUS_CHANGED"
        )
        exception.history.append(
            self._build_history_entry(
                action=action,
                actor_id=actor,
                from_status=previous_status,
                to_status=requested_status,
                note=note,
                details=self._as_json_dict(supporting_information),
            )
        )

        self.db.commit()
        self.db.refresh(exception)
        return exception

    def resolve(
        self,
        exception_id: uuid.UUID,
        resolution_notes: str,
        actor: str = "SYSTEM",
        supporting_information: Optional[Dict[str, Any]] = None,
    ) -> ReconciliationException:
        """Resolve an exception with mandatory notes and optional evidence."""
        exception = self.get_exception(exception_id)
        if not resolution_notes or not resolution_notes.strip():
            raise InvalidResolutionError(
                "Resolution notes are required to resolve an exception."
            )

        allowed = self.VALID_TRANSITIONS.get(exception.status, frozenset())
        if self.RESOLVED not in allowed:
            raise InvalidExceptionTransitionError(
                f"Cannot resolve exception from status '{exception.status}'."
            )

        previous_status = exception.status
        actor = self._normalize_actor(actor)
        now = datetime.now(timezone.utc)

        exception.status = self.RESOLVED
        exception.resolution_notes = resolution_notes.strip()
        exception.resolution_supporting_information = self._as_json_dict(
            supporting_information
        )
        exception.resolved_by = actor
        exception.resolved_at = now
        exception.history.append(
            self._build_history_entry(
                action=self.RESOLVED,
                actor_id=actor,
                from_status=previous_status,
                to_status=self.RESOLVED,
                note=resolution_notes.strip(),
                details=self._as_json_dict(supporting_information),
            )
        )

        self.db.commit()
        self.db.refresh(exception)
        return exception

    def _build_exception(
        self,
        discrepancy: ReconciliationDiscrepancy,
        run: ReconciliationRun,
        actor: str,
    ) -> ReconciliationException:
        supplier_name, po_reference = self._resolve_purchase_order_context(run)
        related_document_ids = self._related_document_ids(run)

        exception = ReconciliationException(
            discrepancy_id=discrepancy.id,
            reconciliation_run_id=run.id,
            exception_type=discrepancy.discrepancy_type,
            severity=discrepancy.severity,
            description=discrepancy.explanation,
            supplier_name=supplier_name,
            po_document_id=run.po_document_id,
            po_reference=po_reference,
            related_document_ids=related_document_ids,
            financial_impact=(
                discrepancy.amount_at_risk
                if discrepancy.amount_at_risk is not None
                else Decimal("0.0000")
            ),
            currency=discrepancy.currency or "USD",
            status=self.OPEN,
            item_code=discrepancy.item_code,
            line_item_description=discrepancy.line_item_description,
            source_evidence=self._as_json_dict(discrepancy.source_evidence),
            created_by=actor,
        )
        exception.history.append(
            self._build_history_entry(
                action="CREATED",
                actor_id=actor,
                from_status=None,
                to_status=self.OPEN,
                note=discrepancy.explanation,
                details={
                    "discrepancy_id": str(discrepancy.id),
                    "discrepancy_type": discrepancy.discrepancy_type,
                    "severity": discrepancy.severity,
                    "reconciliation_run_id": str(run.id),
                },
            )
        )
        return exception

    def _build_history_entry(
        self,
        action: str,
        actor_id: str,
        from_status: Optional[str],
        to_status: Optional[str],
        note: Optional[str],
        details: Optional[Dict[str, Any]],
    ) -> ReconciliationExceptionHistory:
        return ReconciliationExceptionHistory(
            action=action,
            actor_id=actor_id,
            from_status=from_status,
            to_status=to_status,
            note=note,
            details=details,
        )

    def _get_by_discrepancy_id(
        self, discrepancy_id: uuid.UUID
    ) -> Optional[ReconciliationException]:
        return (
            self.db.execute(
                select(ReconciliationException).where(
                    ReconciliationException.discrepancy_id == discrepancy_id
                )
            )
            .scalars()
            .first()
        )

    def _resolve_purchase_order_context(
        self, run: ReconciliationRun
    ) -> Tuple[Optional[str], Optional[str]]:
        supplier_name: Optional[str] = None
        po_reference: Optional[str] = None

        if run.po_document_id is not None:
            po_document = self.db.get(Document, run.po_document_id)
            if po_document is not None:
                po_reference = po_document.po_reference
                if po_document.supplier is not None:
                    supplier_name = po_document.supplier.name

                verification = (
                    self.db.execute(
                        select(DocumentVerification)
                        .where(
                            DocumentVerification.document_id == po_document.id,
                            DocumentVerification.status == "APPROVED",
                        )
                        .order_by(DocumentVerification.version.desc())
                    )
                    .scalars()
                    .first()
                )
                if verification and verification.verified_fields:
                    fields = verification.verified_fields
                    supplier_name = fields.get("supplier_name") or supplier_name
                    po_reference = (
                        fields.get("po_number")
                        or fields.get("po_reference")
                        or po_reference
                    )

        if po_reference is None and run.root_document_id is not None:
            root_document = self.db.get(Document, run.root_document_id)
            if root_document is not None:
                po_reference = root_document.po_reference

        return supplier_name, po_reference

    def _related_document_ids(self, run: ReconciliationRun) -> List[str]:
        related: List[str] = []
        for document_id in run.reconciled_document_ids or []:
            value = str(document_id)
            if value not in related:
                related.append(value)

        if run.root_document_id is not None:
            root_id = str(run.root_document_id)
            if root_id not in related:
                related.append(root_id)

        if run.po_document_id is not None:
            po_id = str(run.po_document_id)
            if po_id not in related:
                related.append(po_id)

        return related

    def _normalize_actor(self, actor: Optional[str]) -> str:
        normalized = (actor or "").strip()
        return normalized or "SYSTEM"

    def _as_json_dict(
        self, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        return dict(value) if value else None
