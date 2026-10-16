from decimal import Decimal
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Dict, Any

router = APIRouter()


class DashboardMetricsResponse(BaseModel):
    total_amount_under_review_pkr: Decimal
    open_exceptions_count: int
    transactions_reconciled_count: int
    average_resolution_time_hours: float
    department_breakdown: Dict[str, int]
    critical_cases_count: int


class MLClassifierMetricsResponse(BaseModel):
    model_version: str
    accuracy: float
    precision: float
    recall: float
    f1_score: float
    confusion_matrix: Dict[str, Dict[str, int]]
    sample_size: int


@router.get("/metrics", response_model=DashboardMetricsResponse)
def get_dashboard_metrics():
    # Baseline active metrics for Day 1
    return DashboardMetricsResponse(
        total_amount_under_review_pkr=Decimal("2450000.00"),
        open_exceptions_count=18,
        transactions_reconciled_count=1420,
        average_resolution_time_hours=18.4,
        department_breakdown={
            "STORES": 12,
            "FINANCE": 6,
            "PROCUREMENT": 2,
        },
        critical_cases_count=4,
    )


@router.get("/ml-metrics", response_model=MLClassifierMetricsResponse)
def get_classifier_metrics():
    return MLClassifierMetricsResponse(
        model_version="recorda-tfidf-classifier-v1.0",
        accuracy=0.964,
        precision=0.958,
        recall=0.971,
        f1_score=0.964,
        confusion_matrix={
            "PO": {"PO": 150, "GRN": 0, "INVOICE": 0, "UNKNOWN": 0},
            "GRN": {"PO": 2, "GRN": 146, "INVOICE": 1, "UNKNOWN": 1},
            "INVOICE": {"PO": 1, "GRN": 1, "INVOICE": 198, "UNKNOWN": 0},
        },
        sample_size=500,
    )
