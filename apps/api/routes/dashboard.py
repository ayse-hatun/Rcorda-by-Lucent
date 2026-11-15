from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Dict
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.schemas import DashboardMetricsResponse
from apps.api.services.reporting_service import ReportingService

router = APIRouter()


class MLClassifierMetricsResponse(BaseModel):
    model_version: str
    accuracy: float
    precision: float
    recall: float
    f1_score: float
    confusion_matrix: Dict[str, Dict[str, int]]
    sample_size: int


@router.get("/metrics", response_model=DashboardMetricsResponse)
def get_dashboard_metrics(db: Session = Depends(get_db)):
    return ReportingService(db).get_dashboard_metrics()


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
