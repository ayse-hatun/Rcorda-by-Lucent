import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure root packages are importable
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from apps.api.config import load_settings

settings = load_settings()

from apps.api.routes import (
    dashboard,
    delivery_schedules,
    documents,
    exceptions,
    health,
    purchase_orders,
    suppliers,
    uploads,
    verifications,
    matching,
    reconciliation,
    reconciliation_exceptions,
    reporting,
)

app = FastAPI(
    title="Recorda API",
    version="0.1.0",
    description="Three-way document reconciliation and exception management service for Procurement, Stores, and Finance.",
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register route modules
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(exceptions.router, prefix="/v1/exceptions", tags=["Exceptions"])
app.include_router(dashboard.router, prefix="/v1/dashboard", tags=["Dashboard"])
app.include_router(documents.router, prefix="/v1/documents", tags=["Documents"])
app.include_router(uploads.router, prefix="/v1/uploads", tags=["Uploads"])
app.include_router(suppliers.router, prefix="/v1/suppliers", tags=["Suppliers"])
app.include_router(
    purchase_orders.router, prefix="/v1/purchase-orders", tags=["Purchase Orders"]
)
app.include_router(
    delivery_schedules.router,
    prefix="/v1/delivery-schedules",
    tags=["Delivery Schedules"],
)
app.include_router(
    verifications.router,
    prefix="/v1/documents",
    tags=["Human Verification"],
)
app.include_router(
    matching.router,
    prefix="/v1/documents",
    tags=["Document Matching"],
)
app.include_router(
    reconciliation.router,
    prefix="/v1/documents",
    tags=["Reconciliation"],
)
app.include_router(
    reconciliation_exceptions.router,
    prefix="/v1/reconciliation-exceptions",
    tags=["Reconciliation Exceptions"],
)
app.include_router(
    reporting.router,
    prefix="/v1/reports",
    tags=["Reporting"],
)
app.include_router(
    reporting.router,
    prefix="/v1/dashboard",
    tags=["Reporting"],
    include_in_schema=False,
)


@app.get("/")
def root():
    return {
        "service": "Recorda API",
        "status": "operational",
        "docs_url": "/docs",
    }
