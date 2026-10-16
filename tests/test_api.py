from fastapi.testclient import TestClient
import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from apps.api.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "Recorda" in data["service"]


def test_dashboard_metrics():
    response = client.get("/v1/dashboard/metrics")
    assert response.status_code == 200
    data = response.json()
    assert "total_amount_under_review_pkr" in data
    assert data["open_exceptions_count"] == 18
    assert "STORES" in data["department_breakdown"]


def test_dashboard_ml_metrics():
    response = client.get("/v1/dashboard/ml-metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["accuracy"] > 0.90
    assert "PO" in data["confusion_matrix"]


def test_list_exceptions():
    response = client.get("/v1/exceptions")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2
    assert data[0]["currency"] == "PKR"


def test_reconcile_demo_endpoint():
    response = client.post("/v1/exceptions/reconcile-demo")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "EX-1048"
    assert data["owner_department"] == "STORES"
    assert data["amount_under_review_pkr"] == "125000.00"
    assert len(data["findings"]) == 2
