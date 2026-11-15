from datetime import date, datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.database import get_engine, get_session_factory
from apps.api.models import (
    Document,
    Exception,
    ExceptionHistory,
    ExtractedDocumentData,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
)


@pytest.fixture
def db_session():
    """Provides a transactional database session against PostgreSQL that rolls back after each test."""
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


def test_postgresql_connection_and_table_presence(db_session: Session):
    """Verify that PostgreSQL is reachable and all MVP tables exist."""
    result = db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
    )
    tables = {row[0] for row in result.fetchall()}
    expected_tables = {
        "suppliers",
        "purchase_orders",
        "purchase_order_items",
        "documents",
        "extracted_document_data",
        "exceptions",
        "exception_history",
        "alembic_version",
    }
    assert expected_tables.issubset(tables)


def test_supplier_persistence_and_relationships(db_session: Session):
    """Verify Supplier persistence and relationship with PurchaseOrder."""
    supplier_code = f"SUP-{uuid.uuid4().hex[:8]}"
    supplier = Supplier(
        name="Apex Industrial Supplies",
        code=supplier_code,
        contact_email="billing@apex-supplies.com",
        contact_phone="+92-21-3456789",
        address="Sector 15, Korangi Industrial Area, Karachi",
    )
    db_session.add(supplier)
    db_session.flush()

    assert supplier.id is not None
    assert supplier.created_at is not None

    # Retrieve supplier
    retrieved = db_session.query(Supplier).filter_by(code=supplier_code).one()
    assert retrieved.name == "Apex Industrial Supplies"
    assert retrieved.purchase_orders == []


def test_purchase_order_and_items_relationship(db_session: Session):
    """Verify PurchaseOrder with nested PurchaseOrderItem relationships and cascade."""
    po_num = f"PO-{uuid.uuid4().hex[:6]}"
    supplier = Supplier(name="General Mill Traders", code=f"SUP-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()

    po = PurchaseOrder(
        po_number=po_num,
        supplier_id=supplier.id,
        order_date=date(2026, 11, 1),
        total_amount=Decimal("150000.5000"),
        currency="USD",
        status="ISSUED",
    )

    item1 = PurchaseOrderItem(
        line_item_id="LI-001",
        item_code="STEEL-ROD-10MM",
        description="High tensile steel rod 10mm",
        ordered_quantity=Decimal("500.0000"),
        unit="pcs",
        unit_price=Decimal("200.0000"),
        total_price=Decimal("100000.0000"),
    )
    item2 = PurchaseOrderItem(
        line_item_id="LI-002",
        item_code="STEEL-PLATE-5MM",
        description="Mild steel plate 5mm",
        ordered_quantity=Decimal("100.0000"),
        unit="pcs",
        unit_price=Decimal("500.0050"),
        total_price=Decimal("50000.5000"),
    )
    po.items.extend([item1, item2])

    db_session.add(po)
    db_session.flush()

    # Query back
    saved_po = db_session.query(PurchaseOrder).filter_by(po_number=po_num).one()
    assert saved_po.supplier.name == "General Mill Traders"
    assert len(saved_po.items) == 2
    assert {it.line_item_id for it in saved_po.items} == {"LI-001", "LI-002"}
    assert saved_po.items[0].purchase_order_id == saved_po.id


def test_document_and_extracted_data_relationship(db_session: Session):
    """Verify Document and 1-to-1 ExtractedDocumentData with JSONB fields."""
    sha = uuid.uuid4().hex + uuid.uuid4().hex
    doc = Document(
        document_type="INVOICE",
        document_number="INV-2026-999",
        po_reference="PO-8888",
        sha256=sha,
        s3_key="documents/2026/11/inv-999.pdf",
        file_name="invoice-999.pdf",
        mime_type="application/pdf",
        status="PROCESSED",
    )

    extracted = ExtractedDocumentData(
        raw_text="Tax Invoice # INV-2026-999 Total: PKR 50,000",
        fields={
            "invoice_number": "INV-2026-999",
            "invoice_date": "2026-11-10",
            "total_pkr": "50000.00",
            "tax_amount_pkr": "7500.00",
        },
        line_items=[
            {
                "item_code": "STEEL-ROD-10MM",
                "description": "Steel Rod",
                "quantity": 250,
                "unit_price": 200,
                "line_total": 50000,
                "bbox": [50.0, 120.0, 540.0, 140.0],
            }
        ],
        confidence_score=0.985,
        model_version="classifier-v1.0-tfidf",
    )
    doc.extracted_data = extracted
    db_session.add(doc)
    db_session.flush()

    saved_doc = db_session.query(Document).filter_by(sha256=sha).one()
    assert saved_doc.extracted_data is not None
    assert saved_doc.extracted_data.confidence_score == pytest.approx(0.985)
    assert saved_doc.extracted_data.fields["total_pkr"] == "50000.00"
    assert len(saved_doc.extracted_data.line_items) == 1
    assert saved_doc.extracted_data.line_items[0]["bbox"] == [50.0, 120.0, 540.0, 140.0]


def test_exception_and_durable_exception_history_on_deletion(db_session: Session):
    """
    Verify Exception case creation, checklist flags, and that audit history
    is preserved durably (NOT destroyed or cascade-deleted) when an exception is removed.
    """
    ex_id = f"EX-{uuid.uuid4().hex[:6].upper()}"
    po_ref = f"PO-{uuid.uuid4().hex[:6].upper()}"

    exc = Exception(
        id=ex_id,
        po_reference=po_ref,
        supplier_name="Pak Cement Corp",
        type="QUANTITY_SHORTAGE",
        status="UNDER_REVIEW",
        owner_department="STORES",
        assigned_user_id="usr-stores-001",
        amount_under_review=Decimal("25000.0000"),
        currency="USD",
        po_present=True,
        grn_present=True,
        invoice_present=True,
        resolution_state="PENDING_SUPPLIER_CREDIT_NOTE",
        resolution_note="Stores verified short shipment of 50 bags.",
    )

    h1 = ExceptionHistory(
        action="CREATED",
        actor_id="system-recon-engine",
        actor_role="SYSTEM",
        to_status="NEW",
        note="Exception automatically triggered by 3-way reconciliation engine",
        details={"discrepancy_type": "QUANTITY_SHORTAGE", "delta_quantity": 50},
    )
    h2 = ExceptionHistory(
        action="STATUS_CHANGE",
        actor_id="usr-stores-001",
        actor_role="STORES",
        from_status="NEW",
        to_status="UNDER_REVIEW",
        note="Assigned to Stores for delivery receipt recount.",
        details={"assigned_to": "usr-stores-001"},
    )
    exc.history.extend([h1, h2])

    db_session.add(exc)
    db_session.flush()

    h1_id = h1.id
    h2_id = h2.id

    # Query back during active lifecycle
    saved_ex = db_session.query(Exception).filter_by(id=ex_id).one()
    assert saved_ex.amount_under_review == Decimal("25000.0000")
    assert saved_ex.currency == "USD"
    assert saved_ex.po_present is True
    assert saved_ex.grn_present is True
    assert len(saved_ex.history) == 2
    assert saved_ex.history[0].action == "CREATED"
    assert saved_ex.history[1].to_status == "UNDER_REVIEW"

    # Delete exception record and verify audit trail survives
    db_session.delete(saved_ex)
    db_session.flush()

    # Exception is deleted
    assert db_session.query(Exception).filter_by(id=ex_id).first() is None

    # History records must NOT be destroyed; foreign key sets exception_id to NULL
    surviving_h1 = db_session.get(ExceptionHistory, h1_id)
    surviving_h2 = db_session.get(ExceptionHistory, h2_id)
    assert surviving_h1 is not None
    assert surviving_h2 is not None
    assert surviving_h1.action == "CREATED"
    assert surviving_h1.exception_id is None
    assert surviving_h2.action == "STATUS_CHANGE"
    assert surviving_h2.exception_id is None


def test_exception_history_survives_entire_lifecycle(db_session: Session):
    """Verify that ExceptionHistory accumulates and survives through the full resolution lifecycle."""
    ex_id = f"EX-LIFE-{uuid.uuid4().hex[:6].upper()}"
    po_ref = f"PO-LIFE-{uuid.uuid4().hex[:6].upper()}"

    exc = Exception(
        id=ex_id,
        po_reference=po_ref,
        supplier_name="Steel Dynamics",
        type="PRICE_VARIANCE",
        status="NEW",
        owner_department="FINANCE",
        amount_under_review=Decimal("1200.0000"),
        currency="USD",
    )
    db_session.add(exc)
    db_session.flush()

    # Lifecycle Step 1: Created
    h_create = ExceptionHistory(
        exception_id=ex_id,
        action="CREATED",
        actor_id="recon-job",
        actor_role="SYSTEM",
        to_status="NEW",
        note="Variance detected: contracted $100 vs billed $112",
    )
    db_session.add(h_create)

    # Lifecycle Step 2: Under Review
    exc.status = "UNDER_REVIEW"
    exc.assigned_user_id = "fin-analyst-02"
    h_review = ExceptionHistory(
        exception_id=ex_id,
        action="ASSIGNED",
        actor_id="fin-lead",
        actor_role="FINANCE",
        from_status="NEW",
        to_status="UNDER_REVIEW",
        note="Assigned to senior finance analyst for price schedule validation",
    )
    db_session.add(h_review)

    # Lifecycle Step 3: Resolved
    exc.status = "RESOLVED_ACCEPTED"
    exc.resolution_state = "ACCEPTED_SURCHARGE"
    exc.resolution_note = "Supplier freight fuel surcharge approved as per clause 4.2"
    h_resolve = ExceptionHistory(
        exception_id=ex_id,
        action="RESOLVED",
        actor_id="fin-analyst-02",
        actor_role="FINANCE",
        from_status="UNDER_REVIEW",
        to_status="RESOLVED_ACCEPTED",
        note="Approved variance, matching amendment",
    )
    db_session.add(h_resolve)
    db_session.flush()

    # Query back and verify complete chronological history
    reloaded_exc = db_session.get(Exception, ex_id)
    assert len(reloaded_exc.history) == 3
    assert [h.action for h in reloaded_exc.history] == ["CREATED", "ASSIGNED", "RESOLVED"]
    assert reloaded_exc.status == "RESOLVED_ACCEPTED"
    assert reloaded_exc.currency == "USD"
