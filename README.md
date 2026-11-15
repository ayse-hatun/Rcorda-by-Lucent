# Recorda — Automated Procurement & Financial Reconciliation Platform

Recorda is an automated procurement audit and financial reconciliation backend designed to execute deterministic 2-way, 3-way, and 4-way matching across **Purchase Orders (PO)**, **Delivery Challans (DC)**, **Goods Receipt Notes (GRN)**, and **Supplier Invoices (INV)**.

---

## Key Features

- **Document Ingestion & Storage:** Upload and store documents with SHA-256 deduplication and storage abstractions (local filesystem or S3-compatible object storage).
- **OCR & Extraction Pipeline:** Multi-page PDF page splitting, OCR text extraction with bounding-box provenance, and structured field extraction for PO, DC, GRN, and Invoices.
- **Human Verification & Review Gate:** Concurrency-safe review workflow (`PENDING_REVIEW`, `IN_REVIEW`, `APPROVED`, `REJECTED`, `RESUBMITTED`) with optimistic locking and full audit trails of field and line-item corrections.
- **Deterministic Relationship Matching:** Automated and manual relationship management (`DC → PO`, `GRN → PO & DC`, `INV → PO`) to assemble transaction chains using strictly approved verification records.
- **Zero-Float Reconciliation Engine:** High-precision `Decimal` arithmetic enforcing strict tolerances and financial variance calculations across transaction chains.
- **Granular Discrepancy Detection:**
  - **Quantity Discrepancies:** `OVER_BILLED_QUANTITY`, `UNDER_DELIVERED`, `OVER_DELIVERED`, `PARTIAL_DELIVERY`, `REJECTED_GOODS_BILLED`.
  - **Financial Discrepancies:** `PRICE_VARIANCE`, `CALCULATION_DISCREPANCY`, `TAX_DISCREPANCY`, `TOTAL_MISMATCH`.
  - **Chain & Document Discrepancies:** `MISSING_PO`, `MISSING_GRN`, `MISSING_DC`, `UNMATCHED_LINE_ITEM`, `SUPPLIER_MISMATCH`, `NON_USD_CURRENCY`.
- **Durable Exception Management:** Discrepancy projection into lifecycle-managed exceptions (`OPEN` → `UNDER_REVIEW` → `RESOLVED` / `REJECTED` / `REOPENED`) with financial exposure calculations.

---

## Architecture Overview

```text
Recorda/
├── apps/
│   └── api/                  # FastAPI REST Application
│       ├── main.py           # Application entrypoint & router mounts
│       ├── models.py         # SQLAlchemy 2.0 declarative database models
│       ├── schemas.py        # Pydantic v2 request/response schemas
│       ├── database.py       # Engine & session management
│       ├── config.py         # App configuration & environment validation
│       ├── storage.py        # FileSystem / S3 storage abstraction
│       ├── routes/           # Modular endpoint routers
│       └── services/         # Domain services (matching, reconciliation, review, exceptions)
├── migrations/               # Alembic database migrations (PostgreSQL)
├── packages/                 # Decoupled core domain libraries
│   ├── ocr/                  # Deterministic OCR engine wrappers
│   ├── classification/       # Document classification interfaces
│   ├── extraction/           # PO, DC, GRN, and Invoice field extractors
│   └── reconciliation/       # Core reconciliation logic & schedule stores
└── tests/                    # Integration and end-to-end test suites
```

---

## Tech Stack

- **Runtime:** Python 3.13+ / 3.14
- **Web Framework:** [FastAPI](https://fastapi.tiangolo.com/) (Uvicorn ASGI)
- **Database & ORM:** PostgreSQL / [Supabase](https://supabase.com/) with [SQLAlchemy 2.0](https://www.sqlalchemy.org/)
- **Migrations:** [Alembic](https://alembic.sqlalchemy.org/)
- **Data Validation:** [Pydantic v2](https://docs.pydantic.dev/)
- **Test Suite:** [pytest](https://docs.pytest.org/) & Starlette TestClient

---

## Getting Started

### 1. Prerequisites
- Python 3.13+ (or 3.14)
- PostgreSQL database instance (or Supabase project)

### 2. Environment Configuration
Copy the example environment file and set your database connection:

```bash
cp .env.example .env
```

Edit `.env` to configure your credentials:
```env
APP_ENV=development
API_HOST=127.0.0.1
API_PORT=8000
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

### 3. Install Dependencies
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 4. Run Database Migrations
Apply all schema migrations to your PostgreSQL database:
```bash
alembic upgrade head
```

### 5. Start the API Server
```bash
uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000
```
Interactive OpenAPI documentation is available at `http://127.0.0.1:8000/docs`.

---

## API Endpoints Summary

| Router | Method | Path | Description |
|---|---|---|---|
| **Health** | `GET` | `/health` | API and database health check |
| **Documents** | `POST` | `/v1/documents/upload` | Ingest document (PDF/Image) with SHA-256 deduplication |
| | `GET` | `/v1/documents/{id}` | Retrieve document metadata |
| | `POST` | `/v1/documents/{id}/ocr` | Trigger OCR text extraction |
| | `POST` | `/v1/documents/{id}/extract` | Trigger structured data extraction |
| **Verification** | `GET` | `/v1/documents/{id}/verification` | Get current verification session |
| | `POST` | `/v1/documents/{id}/verification/approve` | Approve verified fields (gate for matching) |
| | `POST` | `/v1/documents/{id}/verification/reject` | Reject verification with mandatory reason |
| | `POST` | `/v1/documents/{id}/verification/resubmit` | Resubmit rejected document for re-review |
| | `POST` | `/v1/documents/{id}/verification/correct-field` | Correct single field with audit trail |
| **Matching** | `POST` | `/v1/documents/{id}/match` | Execute deterministic type-aware matching |
| | `GET` | `/v1/documents/{id}/relationships` | Inspect transaction chain & relationships |
| **Reconciliation** | `POST` | `/v1/documents/{id}/reconcile` | Run 2-way/3-way/4-way reconciliation |
| | `GET` | `/v1/documents/{id}/reconciliations/latest`| Get latest reconciliation run findings |
| **Exceptions** | `GET` | `/v1/reconciliation-exceptions` | Query open/under-review discrepancies |
| | `PATCH` | `/v1/reconciliation-exceptions/{id}/status` | Transition exception lifecycle |
| | `POST` | `/v1/reconciliation-exceptions/{id}/resolve`| Resolve exception with audit note |
| **Reporting** | `GET` | `/v1/dashboard/metrics` | System KPIs and discrepancy summaries |
| | `GET` | `/v1/reports/reconciliation-runs/{id}` | Comprehensive financial reconciliation audit report |

---

## Running Tests

Execute the complete end-to-end integration and unit test suite:

```bash
# Run all tests
pytest

# Run focused reconciliation end-to-end workflow tests
pytest tests/test_end_to_end_workflow.py -v
```

---

## Business Rules & Safeguards

1. **USD Standard:** All currency calculations strictly operate in `USD`. Non-USD transactions are flagged as `NON_USD_CURRENCY`.
2. **Approved Data Gate:** Matching and reconciliation strictly consume data with `DocumentVerification.status == 'APPROVED'`. Unreviewed or rejected documents cannot be reconciled.
3. **Confirmed Relationships Only:** Reconciliation operates exclusively on `CONFIRMED` document relationship chains. Ambiguous or rejected relationships are excluded.
4. **Exact Decimal Arithmetic:** Float arithmetic is strictly prohibited; all quantities and monetary calculations use Python `Decimal`.
