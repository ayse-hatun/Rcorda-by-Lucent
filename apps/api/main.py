import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure root packages are importable
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from apps.api.routes import health, exceptions, dashboard, uploads

app = FastAPI(
    title="Recorda API",
    version="0.1.0",
    description="Three-way document reconciliation and exception management service for Procurement, Stores, and Finance.",
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register route modules
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(exceptions.router, prefix="/v1/exceptions", tags=["Exceptions"])
app.include_router(dashboard.router, prefix="/v1/dashboard", tags=["Dashboard"])
app.include_router(uploads.router, prefix="/v1/uploads", tags=["Uploads"])


@app.get("/")
def root():
    return {
        "service": "Recorda API",
        "status": "operational",
        "docs_url": "/docs",
    }
