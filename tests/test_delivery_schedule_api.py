"""API integration tests for delivery schedule endpoints backed by PostgreSQL.

These tests use FastAPI TestClient and hit the real API layer end-to-end.
Every write goes through the HTTP route → PgDeliveryScheduleStore → PostgreSQL.

Run from ayse-hatun-super-sniffle/:
    python -m pytest tests/test_delivery_schedule_api.py -v
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app


client = TestClient(app, raise_server_exceptions=True)

BASE = "/v1/delivery-schedules"


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _sha() -> str:
    return uuid.uuid4().hex * 2  # 64 hex chars


# ---------------------------------------------------------------------------
# Helper: create a PO item via API
# ---------------------------------------------------------------------------


def _create_po_item(po_ref: str, ordered_qty: int = 500) -> dict:
    uid = _uid()
    r = client.post(f"{BASE}/po-items", json={
        "po_reference": po_ref,
        "line_item_id": f"LI-{uid}",
        "description": f"Test item {uid}",
        "ordered_quantity": str(ordered_qty),
        "unit": "pcs",
    })
    assert r.status_code == 201, r.text
    return r.json()


def _register_challan(po_item_id: str, po_ref: str, qty: int = 200,
                      ev_date: str = "2024-08-01") -> dict:
    uid = _uid()
    r = client.post(f"{BASE}/source-documents", json={
        "document_id": f"DC-{uid}",
        "document_type": "DELIVERY_CHALLAN",
        "document_number": f"NUM-{uid}",
        "po_reference": po_ref,
        "sha256": _sha(),
        "lines": [{
            "line_item_id": f"LINE-{uid}",
            "quantity": str(qty),
            "evidence_date": ev_date,
            "po_item_ids": [po_item_id],
        }],
    })
    assert r.status_code == 201, r.text
    return r.json()


def _register_grn(po_item_id: str, po_ref: str, qty: int = 200,
                  ev_date: str = "2024-08-10") -> dict:
    uid = _uid()
    r = client.post(f"{BASE}/source-documents", json={
        "document_id": f"GRN-{uid}",
        "document_type": "GRN",
        "document_number": f"GRN-NUM-{uid}",
        "po_reference": po_ref,
        "sha256": _sha(),
        "lines": [{
            "line_item_id": f"GLINE-{uid}",
            "quantity": str(qty),
            "evidence_date": ev_date,
            "po_item_ids": [po_item_id],
        }],
    })
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPoItemAndScheduleCreation:
    def test_create_po_item_and_retrieve(self):
        uid = _uid()
        po_ref = f"PO-API-{uid}"
        item = _create_po_item(po_ref, ordered_qty=300)

        assert item["po_reference"] == po_ref
        assert Decimal(item["ordered_quantity"]) == Decimal("300")
        po_item_id = item["po_item_id"]

        # Retrieve via GET
        r = client.get(f"{BASE}/po-items/{po_item_id}")
        assert r.status_code == 200
        bundle = r.json()
        assert bundle["po_item"]["po_item_id"] == po_item_id
        assert bundle["schedules"] == []

    def test_create_schedule_and_retrieve(self):
        uid = _uid()
        item = _create_po_item(f"PO-SCHED-{uid}", ordered_qty=500)
        po_item_id = item["po_item_id"]

        r = client.post(f"{BASE}/po-items/{po_item_id}/schedules", json={
            "planned_quantity": "200",
            "expected_delivery_date": "2024-09-01",
        })
        assert r.status_code == 201
        sched = r.json()
        assert Decimal(sched["planned_quantity"]) == Decimal("200")
        assert sched["status"] == "SCHEDULED"

        # GET bundle includes the schedule
        r2 = client.get(f"{BASE}/po-items/{po_item_id}")
        assert r2.status_code == 200
        schedules = r2.json()["schedules"]
        assert len(schedules) == 1
        assert schedules[0]["schedule_id"] == sched["schedule_id"]

    def test_multiple_schedules_per_po_item(self):
        uid = _uid()
        item = _create_po_item(f"PO-MULTI-{uid}", ordered_qty=900)
        pid = item["po_item_id"]

        for i, (qty, d) in enumerate([("300", "2024-07-01"), ("300", "2024-08-01"), ("300", "2024-09-01")]):
            r = client.post(f"{BASE}/po-items/{pid}/schedules", json={
                "planned_quantity": qty,
                "expected_delivery_date": d,
            })
            assert r.status_code == 201, r.text

        bundle = client.get(f"{BASE}/po-items/{pid}").json()
        assert len(bundle["schedules"]) == 3

    def test_schedule_exceeding_ordered_quantity_rejected(self):
        uid = _uid()
        item = _create_po_item(f"PO-CAP-{uid}", ordered_qty=100)
        pid = item["po_item_id"]

        # First schedule takes full qty
        client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "80", "expected_delivery_date": "2024-09-01",
        })
        # Second over-runs
        r = client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "30", "expected_delivery_date": "2024-10-01",
        })
        assert r.status_code == 422

    def test_missing_po_item_returns_404(self):
        r = client.get(f"{BASE}/po-items/does-not-exist-xyz")
        assert r.status_code == 404


class TestAmendments:
    def test_propose_and_approve_amendment(self):
        uid = _uid()
        item = _create_po_item(f"PO-AMD-{uid}", ordered_qty=500)
        pid = item["po_item_id"]

        sched = client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "300", "expected_delivery_date": "2024-08-01",
        }).json()
        sid = sched["schedule_id"]

        # Propose
        r = client.post(f"{BASE}/schedules/{sid}/amendments", json={
            "proposed_quantity": "250",
            "proposed_expected_date": "2024-08-15",
            "requested_by": "planner@example.com",
        })
        assert r.status_code == 201
        amend = r.json()
        assert amend["status"] == "PENDING_APPROVAL"
        assert Decimal(amend["previous_quantity"]) == Decimal("300")

        # Approve
        r2 = client.post(f"{BASE}/amendments/{amend['amendment_id']}/approve", json={
            "approved_by": "manager@example.com",
        })
        assert r2.status_code == 200
        approved = r2.json()
        assert approved["status"] == "APPROVED"
        assert approved["approved_by"] == "manager@example.com"

        # Schedule quantity updated
        bundle = client.get(f"{BASE}/po-items/{pid}").json()
        updated = bundle["schedules"][0]
        assert Decimal(updated["planned_quantity"]) == Decimal("250")
        assert len(updated["amendment_history"]) == 1

    def test_amendment_history_preserved_across_requests(self):
        uid = _uid()
        item = _create_po_item(f"PO-HIST-{uid}", ordered_qty=400)
        pid = item["po_item_id"]
        sched = client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "400", "expected_delivery_date": "2024-08-01",
        }).json()
        sid = sched["schedule_id"]

        amend = client.post(f"{BASE}/schedules/{sid}/amendments", json={
            "proposed_quantity": "350",
            "proposed_expected_date": "2024-08-20",
            "requested_by": "planner@test.com",
        }).json()
        client.post(f"{BASE}/amendments/{amend['amendment_id']}/approve", json={
            "approved_by": "cfo@test.com",
        })

        # Fresh GET — amendment history must survive across HTTP requests
        bundle = client.get(f"{BASE}/po-items/{pid}").json()
        hist = bundle["schedules"][0]["amendment_history"]
        assert len(hist) == 1
        assert hist[0]["approved_by"] == "cfo@test.com"


class TestAllocations:
    def test_dispatch_allocation_via_api(self):
        uid = _uid()
        po_ref = f"PO-DAPI-{uid}"
        item = _create_po_item(po_ref, ordered_qty=200)
        pid = item["po_item_id"]
        sched = client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "200", "expected_delivery_date": "2024-09-01",
        }).json()
        challan = _register_challan(pid, po_ref, qty=200, ev_date="2024-08-20")

        r = client.post(f"{BASE}/allocations", json={
            "allocation_type": "DISPATCH",
            "source_document_type": "DELIVERY_CHALLAN",
            "source_document_id": challan["document_id"],
            "source_line_item_id": challan["lines"][0]["line_item_id"],
            "source_line_quantity": "200",
            "evidence_date": "2024-08-20",
            "po_item_id": pid,
            "schedule_id": sched["schedule_id"],
            "quantity": "200",
        })
        assert r.status_code == 201
        alloc = r.json()
        assert alloc["allocation_type"] == "DISPATCH"

        # Check via list
        allocs = client.get(f"{BASE}/po-items/{pid}/allocations").json()
        assert len(allocs) == 1

    def test_dispatch_then_receipt_marks_schedule_fulfilled(self):
        uid = _uid()
        po_ref = f"PO-FULL-{uid}"
        item = _create_po_item(po_ref, ordered_qty=150)
        pid = item["po_item_id"]
        sched = client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "150", "expected_delivery_date": "2024-09-01",
        }).json()
        sid = sched["schedule_id"]

        challan = _register_challan(pid, po_ref, qty=150, ev_date="2024-08-25")
        grn = _register_grn(pid, po_ref, qty=150, ev_date="2024-09-01")

        dispatch = client.post(f"{BASE}/allocations", json={
            "allocation_type": "DISPATCH",
            "source_document_type": "DELIVERY_CHALLAN",
            "source_document_id": challan["document_id"],
            "source_line_item_id": challan["lines"][0]["line_item_id"],
            "source_line_quantity": "150",
            "evidence_date": "2024-08-25",
            "po_item_id": pid,
            "schedule_id": sid,
            "quantity": "150",
        }).json()

        client.post(f"{BASE}/allocations", json={
            "allocation_type": "RECEIPT",
            "source_document_type": "GRN",
            "source_document_id": grn["document_id"],
            "source_line_item_id": grn["lines"][0]["line_item_id"],
            "source_line_quantity": "150",
            "evidence_date": "2024-09-01",
            "po_item_id": pid,
            "schedule_id": sid,
            "quantity": "150",
            "matched_dispatch_allocation_id": dispatch["allocation_id"],
        })

        bundle = client.get(f"{BASE}/po-items/{pid}").json()
        assert bundle["schedules"][0]["status"] == "FULFILLED"

    def test_wrong_document_type_for_dispatch_rejected(self):
        uid = _uid()
        po_ref = f"PO-WDT-{uid}"
        item = _create_po_item(po_ref, ordered_qty=100)
        pid = item["po_item_id"]
        grn = _register_grn(pid, po_ref, qty=100)

        r = client.post(f"{BASE}/allocations", json={
            "allocation_type": "DISPATCH",
            "source_document_type": "GRN",   # wrong for DISPATCH
            "source_document_id": grn["document_id"],
            "source_line_item_id": grn["lines"][0]["line_item_id"],
            "source_line_quantity": "100",
            "evidence_date": "2024-08-10",
            "po_item_id": pid,
            "quantity": "100",
        })
        assert r.status_code == 422

    def test_unregistered_evidence_document_rejected(self):
        uid = _uid()
        item = _create_po_item(f"PO-UNR-{uid}", ordered_qty=100)
        pid = item["po_item_id"]

        r = client.post(f"{BASE}/allocations", json={
            "allocation_type": "DISPATCH",
            "source_document_type": "DELIVERY_CHALLAN",
            "source_document_id": f"GHOST-{uid}",
            "source_line_item_id": f"LINE-{uid}",
            "source_line_quantity": "100",
            "evidence_date": "2024-08-01",
            "po_item_id": pid,
            "quantity": "100",
        })
        assert r.status_code == 404


class TestReconciliation:
    def test_reconciliation_run_via_api(self):
        uid = _uid()
        item = _create_po_item(f"PO-REC-{uid}", ordered_qty=300)
        pid = item["po_item_id"]
        client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "300", "expected_delivery_date": "2024-08-01",
        })

        r = client.post(f"{BASE}/po-items/{pid}/reconcile", json={
            "evaluation_date": "2024-07-01",
        })
        assert r.status_code == 200
        run = r.json()
        assert run["po_item_id"] == pid
        assert run["run_id"] is not None

        # Retrieve validation runs
        runs = client.get(f"{BASE}/po-items/{pid}/validation-runs").json()
        assert len(runs) >= 1
        assert runs[-1]["run_id"] == run["run_id"]


class TestPersistenceAcrossRequests:
    def test_data_persists_across_separate_api_requests(self):
        """Create data in one request; verify it in a completely separate request."""
        uid = _uid()
        po_ref = f"PO-PERSIST-API-{uid}"
        item = _create_po_item(po_ref, ordered_qty=600)
        pid = item["po_item_id"]

        # Request 1: create schedule
        s1 = client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "200", "expected_delivery_date": "2024-07-01",
        }).json()
        s2 = client.post(f"{BASE}/po-items/{pid}/schedules", json={
            "planned_quantity": "400", "expected_delivery_date": "2024-08-01",
        }).json()

        # Request 2: independent GET — data must be in PostgreSQL
        bundle = client.get(f"{BASE}/po-items/{pid}").json()
        ids = {s["schedule_id"] for s in bundle["schedules"]}
        assert s1["schedule_id"] in ids
        assert s2["schedule_id"] in ids
        total = sum(Decimal(s["planned_quantity"]) for s in bundle["schedules"])
        assert total == Decimal("600")
