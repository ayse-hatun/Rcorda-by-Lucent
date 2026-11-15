from datetime import date
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.database import get_session_factory
from apps.api.main import app
from apps.api.models import PurchaseOrder, PurchaseOrderItem, Supplier

client = TestClient(app)


@pytest.fixture
def cleanup_tracker():
    """Tracks created supplier and PO IDs to ensure isolated cleanup from PostgreSQL."""
    created_po_ids = []
    created_supplier_ids = []

    yield {"po_ids": created_po_ids, "supplier_ids": created_supplier_ids}

    # Safe cleanup after test completes
    factory = get_session_factory()
    with factory() as session:
        for po_id in created_po_ids:
            po = session.get(PurchaseOrder, po_id)
            if po:
                session.delete(po)
        for supp_id in created_supplier_ids:
            supp = session.get(Supplier, supp_id)
            if supp:
                session.delete(supp)
        session.commit()


def test_supplier_crud_lifecycle(cleanup_tracker):
    """Test full CRUD lifecycle for Supplier API with PostgreSQL persistence."""
    unique_suffix = uuid.uuid4().hex[:8]
    code = f"SUP-{unique_suffix}"
    payload = {
        "name": f"Indus Engineering {unique_suffix}",
        "code": code,
        "contact_email": f"sales@{unique_suffix}.com",
        "contact_phone": "+92-21-998877",
        "address": "Plot 42, SITE Area, Karachi",
    }

    # 1. Create Supplier (POST)
    res = client.post("/v1/suppliers", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()
    supplier_id = uuid.UUID(data["id"])
    cleanup_tracker["supplier_ids"].append(supplier_id)
    assert data["name"] == payload["name"]
    assert data["code"] == code

    # 2. Get Supplier by ID (GET)
    res = client.get(f"/v1/suppliers/{supplier_id}")
    assert res.status_code == 200
    assert res.json()["id"] == str(supplier_id)

    # 3. List Suppliers with search and pagination (GET)
    res = client.get(f"/v1/suppliers?search={unique_suffix}&limit=10&offset=0")
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 1
    assert any(s["id"] == str(supplier_id) for s in items)

    # 4. Update Supplier (PATCH)
    patch_payload = {
        "name": f"Indus Engineering Updated {unique_suffix}",
        "contact_email": f"support@{unique_suffix}.com",
    }
    res = client.patch(f"/v1/suppliers/{supplier_id}", json=patch_payload)
    assert res.status_code == 200
    updated_data = res.json()
    assert updated_data["name"] == patch_payload["name"]
    assert updated_data["contact_email"] == patch_payload["contact_email"]
    assert updated_data["code"] == code


def test_supplier_duplicate_rejection(cleanup_tracker):
    """Test that duplicate supplier codes return 409 Conflict."""
    code = f"SUP-{uuid.uuid4().hex[:8]}"

    res1 = client.post("/v1/suppliers", json={"name": "Supplier 1", "code": code})
    assert res1.status_code == 201
    cleanup_tracker["supplier_ids"].append(uuid.UUID(res1.json()["id"]))

    # Attempt to create duplicate supplier code
    res2 = client.post("/v1/suppliers", json={"name": "Supplier 2", "code": code})
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_supplier_validation_and_not_found():
    """Test validation errors (422) and not found errors (404)."""
    # Empty name should fail validation with 422
    res = client.post(
        "/v1/suppliers",
        json={"name": "   ", "code": f"SUP-VAL-{uuid.uuid4().hex[:8]}"},
    )
    assert res.status_code == 422

    # Non-existent ID returns 404
    non_existent = uuid.uuid4()
    res = client.get(f"/v1/suppliers/{non_existent}")
    assert res.status_code == 404

    res = client.patch(f"/v1/suppliers/{non_existent}", json={"name": "New Name"})
    assert res.status_code == 404


def test_purchase_order_creation_multiple_items_and_decimal_totals(cleanup_tracker):
    """
    Test creating a PO with multiple items, explicit currency,
    verifying Decimal totals and line item calculation.
    """
    unique_suffix = uuid.uuid4().hex[:8]
    # Create Supplier first
    supp_res = client.post(
        "/v1/suppliers",
        json={"name": f"PO Test Supplier {unique_suffix}", "code": f"VEND-{unique_suffix}"},
    )
    assert supp_res.status_code == 201
    supplier_id = uuid.UUID(supp_res.json()["id"])
    cleanup_tracker["supplier_ids"].append(supplier_id)

    po_number = f"PO-{unique_suffix}"
    # Item 1: 10 units @ 1500.50 = 15005.00
    # Item 2: 25.5 units @ 200.00 = 5100.00
    # Item 3: 5 units @ 1000.00, explicit total_price = 5000.00
    # Total PO = 25105.0000 PKR
    po_payload = {
        "po_number": po_number,
        "supplier_id": str(supplier_id),
        "order_date": "2026-11-13",
        "currency": "USD",
        "status": "ISSUED",
        "items": [
            {
                "line_item_id": "LINE-01",
                "item_code": "VALVE-50MM",
                "description": "Industrial ball valve 50mm",
                "ordered_quantity": "10.0000",
                "unit": "pcs",
                "unit_price": "1500.5000",
            },
            {
                "line_item_id": "LINE-02",
                "item_code": "FLANGE-50MM",
                "description": "Carbon steel flange 50mm",
                "ordered_quantity": "25.5000",
                "unit": "pcs",
                "unit_price": "200.0000",
            },
            {
                "line_item_id": "LINE-03",
                "item_code": "GASKET-50MM",
                "description": "Spiral wound gasket",
                "ordered_quantity": "5.0000",
                "unit": "pcs",
                "unit_price": "1000.0000",
                "total_price": "5000.0000",
            },
        ],
    }

    # 1. Create Purchase Order (POST)
    res = client.post("/v1/purchase-orders", json=po_payload)
    assert res.status_code == 201, res.text
    po_data = res.json()
    po_id = uuid.UUID(po_data["id"])
    cleanup_tracker["po_ids"].append(po_id)

    assert po_data["po_number"] == po_number
    assert po_data["currency"] == "USD"
    assert po_data["status"] == "ISSUED"
    assert Decimal(str(po_data["total_amount"])) == Decimal("25105.0000")
    assert len(po_data["items"]) == 3
    assert po_data["supplier"]["id"] == str(supplier_id)

    # 2. Get PO by ID (GET)
    get_res = client.get(f"/v1/purchase-orders/{po_id}")
    assert get_res.status_code == 200
    assert get_res.json()["po_number"] == po_number
    assert len(get_res.json()["items"]) == 3

    # 3. Get PO Items endpoint (GET /items)
    items_res = client.get(f"/v1/purchase-orders/{po_id}/items")
    assert items_res.status_code == 200
    items_data = items_res.json()
    assert len(items_data) == 3
    assert items_data[0]["line_item_id"] == "LINE-01"
    assert Decimal(str(items_data[0]["total_price"])) == Decimal("15005.0000")

    # 4. Patch PO status and order_date
    patch_res = client.patch(
        f"/v1/purchase-orders/{po_id}",
        json={"status": "PARTIALLY_FULFILLED", "order_date": "2026-11-14"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["status"] == "PARTIALLY_FULFILLED"
    assert patch_res.json()["order_date"] == "2026-11-14"


def test_purchase_order_filters_and_pagination(cleanup_tracker):
    """Test filtering purchase orders by status, supplier_id, and po_number search."""
    unique_suffix = uuid.uuid4().hex[:8]
    # Create 2 suppliers
    s1_res = client.post(
        "/v1/suppliers", json={"name": f"Supp1 {unique_suffix}", "code": f"S1-{unique_suffix}"}
    )
    s2_res = client.post(
        "/v1/suppliers", json={"name": f"Supp2 {unique_suffix}", "code": f"S2-{unique_suffix}"}
    )
    s1_id = uuid.UUID(s1_res.json()["id"])
    s2_id = uuid.UUID(s2_res.json()["id"])
    cleanup_tracker["supplier_ids"].extend([s1_id, s2_id])

    # Create PO 1 for s1
    po1_res = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-ALPHA-{unique_suffix}",
            "supplier_id": str(s1_id),
            "status": "ISSUED",
            "items": [{"line_item_id": "L1", "description": "Desc 1", "ordered_quantity": 10}],
        },
    )
    po1_id = uuid.UUID(po1_res.json()["id"])
    cleanup_tracker["po_ids"].append(po1_id)

    # Create PO 2 for s2
    po2_res = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-BETA-{unique_suffix}",
            "supplier_id": str(s2_id),
            "status": "APPROVED",
            "items": [{"line_item_id": "L1", "description": "Desc 2", "ordered_quantity": 5}],
        },
    )
    po2_id = uuid.UUID(po2_res.json()["id"])
    cleanup_tracker["po_ids"].append(po2_id)

    # Filter by supplier s1
    res = client.get(f"/v1/purchase-orders?supplier_id={s1_id}")
    assert res.status_code == 200
    pos = res.json()
    assert any(p["id"] == str(po1_id) for p in pos)
    assert not any(p["id"] == str(po2_id) for p in pos)

    # Filter by status
    res = client.get("/v1/purchase-orders?status=APPROVED")
    assert res.status_code == 200
    assert any(p["id"] == str(po2_id) for p in res.json())

    # Search by PO number
    res = client.get(f"/v1/purchase-orders?search=ALPHA-{unique_suffix}")
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["id"] == str(po1_id)


def test_purchase_order_validation_and_errors(cleanup_tracker):
    """Test validation errors (422), duplicates (409), and missing foreign keys (400)."""
    unique_suffix = uuid.uuid4().hex[:8]

    # 1. Non-existent supplier_id should fail with 400
    fake_supplier_id = uuid.uuid4()
    res = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-ERR-{unique_suffix}",
            "supplier_id": str(fake_supplier_id),
            "items": [{"line_item_id": "L1", "description": "Desc", "ordered_quantity": 10}],
        },
    )
    assert res.status_code == 400
    assert "does not exist" in res.json()["detail"]

    # 2. Empty items array should fail with 422
    res = client.post(
        "/v1/purchase-orders",
        json={"po_number": f"PO-EMPTY-{unique_suffix}", "items": []},
    )
    assert res.status_code == 422

    # 3. Non-positive quantity should fail with 422
    res = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-ZEROQTY-{unique_suffix}",
            "items": [{"line_item_id": "L1", "description": "Desc", "ordered_quantity": 0}],
        },
    )
    assert res.status_code == 422

    # 4. Negative unit price should fail with 422
    res = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-NEGPRICE-{unique_suffix}",
            "items": [
                {
                    "line_item_id": "L1",
                    "description": "Desc",
                    "ordered_quantity": 1,
                    "unit_price": -50.0,
                }
            ],
        },
    )
    assert res.status_code == 422

    # 5. Duplicate PO number should fail with 409
    po_num = f"PO-DUP-{unique_suffix}"
    res1 = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": po_num,
            "items": [{"line_item_id": "L1", "description": "Desc", "ordered_quantity": 1}],
        },
    )
    assert res1.status_code == 201
    cleanup_tracker["po_ids"].append(uuid.UUID(res1.json()["id"]))

    res2 = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": po_num,
            "items": [{"line_item_id": "L1", "description": "Desc", "ordered_quantity": 1}],
        },
    )
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_persistence_across_sessions(cleanup_tracker):
    """Verify that records created via HTTP API are committed and readable across independent DB sessions."""
    unique_suffix = uuid.uuid4().hex[:8]
    supp_code = f"SUP-SESS-{unique_suffix}"
    po_num = f"PO-SESS-{unique_suffix}"

    # 1. Create Supplier via API
    s_res = client.post(
        "/v1/suppliers",
        json={"name": f"Session Test Supplier {unique_suffix}", "code": supp_code},
    )
    assert s_res.status_code == 201
    supplier_id = uuid.UUID(s_res.json()["id"])
    cleanup_tracker["supplier_ids"].append(supplier_id)

    # 2. Create PO via API
    po_res = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": po_num,
            "supplier_id": str(supplier_id),
            "currency": "USD",
            "items": [
                {
                    "line_item_id": "L-01",
                    "description": "Cross-session item",
                    "ordered_quantity": "50.0000",
                    "unit_price": "120.0000",
                }
            ],
        },
    )
    assert po_res.status_code == 201
    po_id = uuid.UUID(po_res.json()["id"])
    cleanup_tracker["po_ids"].append(po_id)

    # 3. Open a brand-new SQLAlchemy session and query PostgreSQL directly
    factory = get_session_factory()
    with factory() as independent_session:
        # Check Supplier in independent session
        db_supp = independent_session.execute(
            select(Supplier).where(Supplier.code == supp_code)
        ).scalar_one_or_none()
        assert db_supp is not None
        assert db_supp.id == supplier_id

        # Check PO and line items in independent session
        db_po = independent_session.execute(
            select(PurchaseOrder).where(PurchaseOrder.po_number == po_num)
        ).scalar_one_or_none()
        assert db_po is not None
        assert db_po.id == po_id
        assert db_po.total_amount == Decimal("6000.0000")
        assert len(db_po.items) == 1
        assert db_po.items[0].line_item_id == "L-01"
        assert db_po.items[0].ordered_quantity == Decimal("50.0000")


def test_purchase_order_omitted_currency_defaults_to_usd(cleanup_tracker):
    """Verify that omitting the currency in a PO creation payload defaults to USD."""
    unique_suffix = uuid.uuid4().hex[:8]
    s_res = client.post(
        "/v1/suppliers",
        json={"name": f"Default Currency Supplier {unique_suffix}", "code": f"S-DEF-{unique_suffix}"},
    )
    assert s_res.status_code == 201
    supplier_id = uuid.UUID(s_res.json()["id"])
    cleanup_tracker["supplier_ids"].append(supplier_id)

    po_num = f"PO-DEF-CURR-{unique_suffix}"
    # Omit currency completely from the payload
    payload = {
        "po_number": po_num,
        "supplier_id": str(supplier_id),
        "items": [
            {
                "line_item_id": "L-DEFAULT",
                "description": "Item without explicit currency",
                "ordered_quantity": "10.0000",
                "unit_price": "25.0000",
            }
        ],
    }

    res = client.post("/v1/purchase-orders", json=payload)
    assert res.status_code == 201, res.text
    po_data = res.json()
    po_id = uuid.UUID(po_data["id"])
    cleanup_tracker["po_ids"].append(po_id)

    # API response must return USD default
    assert po_data["currency"] == "USD"

    # Verify persisted in PostgreSQL as USD
    factory = get_session_factory()
    with factory() as session:
        db_po = session.get(PurchaseOrder, po_id)
        assert db_po is not None
        assert db_po.currency == "USD"


def test_purchase_order_rejects_unsupported_currencies(cleanup_tracker):
    """Verify that non-USD currencies (e.g. PKR, EUR, GBP) are rejected with HTTP 422."""
    unique_suffix = uuid.uuid4().hex[:8]
    s_res = client.post(
        "/v1/suppliers",
        json={"name": f"Currency Test Supplier {unique_suffix}", "code": f"S-CURR-{unique_suffix}"},
    )
    assert s_res.status_code == 201
    supplier_id = uuid.UUID(s_res.json()["id"])
    cleanup_tracker["supplier_ids"].append(supplier_id)

    base_items = [
        {
            "line_item_id": "L-1",
            "description": "Currency test item",
            "ordered_quantity": "5.0000",
            "unit_price": "10.0000",
        }
    ]

    # 1. Reject PKR
    res_pkr = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-PKR-{unique_suffix}",
            "supplier_id": str(supplier_id),
            "currency": "PKR",
            "items": base_items,
        },
    )
    assert res_pkr.status_code == 422
    assert "not supported" in str(res_pkr.json()["detail"]).lower()

    # 2. Reject EUR
    res_eur = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-EUR-{unique_suffix}",
            "supplier_id": str(supplier_id),
            "currency": "EUR",
            "items": base_items,
        },
    )
    assert res_eur.status_code == 422
    assert "not supported" in str(res_eur.json()["detail"]).lower()

    # 3. Create valid USD PO, then verify PATCH also rejects unsupported currency
    valid_res = client.post(
        "/v1/purchase-orders",
        json={
            "po_number": f"PO-VALID-{unique_suffix}",
            "supplier_id": str(supplier_id),
            "currency": "USD",
            "items": base_items,
        },
    )
    assert valid_res.status_code == 201
    po_id = uuid.UUID(valid_res.json()["id"])
    cleanup_tracker["po_ids"].append(po_id)

    patch_res = client.patch(
        f"/v1/purchase-orders/{po_id}",
        json={"currency": "GBP"},
    )
    assert patch_res.status_code == 422
    assert "not supported" in str(patch_res.json()["detail"]).lower()
