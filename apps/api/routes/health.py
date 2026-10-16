from fastapi import APIRouter
from datetime import datetime, timezone

router = APIRouter()


@router.get("")
def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "Recorda Backend API",
    }
