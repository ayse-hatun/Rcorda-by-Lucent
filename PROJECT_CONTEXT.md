# Recorda — Project Context & Implementation Reference

## 1. Project Purpose & Architecture
Recorda is an automated procurement and financial reconciliation platform designed to audit and reconcile the 3-way/4-way matching pipeline across **Purchase Orders (PO)**, **Delivery Challans (DC)**, **Goods Receipt Notes (GRN)**, and **Supplier Invoices (INV)**.

### Architecture Overview
- **Backend Framework:** FastAPI (modular routers in `apps/api/routes/`)
- **Persistence & ORM:** PostgreSQL + SQLAlchemy 2.0 (declarative models in `apps/api/models.py`)
- **Migrations:** Alembic (`migrations/versions/`)
- **Data Serialization:** Pydantic v2 schemas (`apps/api/schemas.py`)
- **Core Domain Packages:** `packages/` (`ocr`, `classification`, `extraction`, `matching`, `reconciliation`, `storage`)
- **Storage Abstraction:** FileSystemStorage / S3/MinIO compatible (`packages/storage/`)

---

## 2. Completed Commands & Implementation Decisions

- **Commands 01–05.1: Procurement Baseline & Delivery Schedules**
  - Supplier & PO core domains, delivery schedule models, PostgreSQL store adapter (`PgDeliveryScheduleStore`), exception handling, and dashboards.
- **Command 06: Document Ingestion & Storage**
  - Storage abstraction (`FileSystemStorage`, S3-compatible). Document upload endpoint, SHA-256 deduplication, file validation, metadata storage.
- **Command 07: OCR Processing Pipeline**
  - Deterministic engine wrapper around PaddleOCR. Multi-page PDF splitting to page images. Page-level results (`OcrRun`, `OcrPageResult`) with bounding boxes, confidence, text blocks, and status tracking.
- **Command 08: Document Classification Interface**
  - Clean decoupled classification interface (`DocumentClassifierInterface`). Model training postponed by project decision; classification persists type history and human-review flags without blocking extraction.
- **Command 09: Structured Data Extraction**
  - Specialized extractors: `PurchaseOrderExtractor`, `DeliveryChallanExtractor`, `GoodsReceiptNoteExtractor`, `SupplierInvoiceExtractor`.
  - Extracts line items, currency, PO reference numbers, and totals with field-level bounding box provenance.
- **Command 10: Human Verification & Review Workflow**
  - Persistent verification records (`DocumentVerification`, `VerificationCorrection`).
  - Strict approval gate: state machine transitions (`DRAFT`, `PENDING_REVIEW`, `APPROVED`, `REJECTED`).
  - Optimistic locking via integer `version` to prevent concurrent overwrite. Line item and header corrections audit trail. Original extraction results remain immutable.
- **Command 11: Document Matching & Relationship Management**
  - Type-aware matching between documents (`DC -> PO`, `GRN -> PO & DC`, `INV -> PO`).
  - Authoritative rule: Only `APPROVED` verification data is used for matching.
  - Relational persistence: `DocumentRelationship` (states: `CONFIRMED`, `PROPOSED`, `FLAGGED_AMBIGUOUS`, `REJECTED`) and `DocumentMatchAudit`.
  - Transaction chain traversal (`matching_service.get_transaction_chain()`). Supplier consistency and reference integrity validation.
- **Command 12: Reconciliation Engine**
  - Deterministic 2-way, 3-way, and 4-way matching engine comparing approved PO, DC, GRN, and Invoice data within confirmed transaction chains.
  - Authoritative inputs: strictly `APPROVED` verification data and `CONFIRMED` relationships.
  - Quantities & rates: exact `Decimal` arithmetic (zero float policy) with configurable tolerance.
  - Granular findings: `OVER_BILLED_QUANTITY`, `UNDER_DELIVERED`, `REJECTED_GOODS_BILLED`, `OVER_DELIVERED`, `PARTIAL_DELIVERY`, `PRICE_VARIANCE`, `CALCULATION_DISCREPANCY`, `TAX_DISCREPANCY`, `TOTAL_MISMATCH`, `MISSING_PO`, `MISSING_GRN`, `MISSING_DC`, `UNMATCHED_LINE_ITEM`, `SUPPLIER_MISMATCH`, `NON_USD_CURRENCY`.
  - Relational persistence: `ReconciliationRun` (statuses: `PASS`, `WARNING`, `FAIL`, `INCOMPLETE`, `NEEDS_REVIEW`) and `ReconciliationDiscrepancy`.
  - API endpoints: `POST /v1/documents/{document_id}/reconcile`, `GET /v1/documents/{document_id}/reconciliations`, `GET /v1/documents/{document_id}/reconciliations/latest`.

---

## 3. Existing Models, Services & API Routes

### Core Models (`apps/api/models.py`)
- `Document`: Upload metadata, SHA-256 hash, mime type, document type.
- `OcrRun`, `OcrPageResult`: Page-level text blocks and confidence scores.
- `ExtractedDocumentData`: JSON payload of extracted fields with extraction metadata.
- `DocumentVerification`: Verification status (`APPROVED`, `REJECTED`, etc.), verified JSON data, version for concurrency.
- `VerificationCorrection`: Field-level and line-item corrections audit trail.
- `DocumentRelationship`: Directed links between documents (`CONFIRMED`, `FLAGGED_AMBIGUOUS`, etc.) with match score and criteria.
- `DocumentMatchAudit`: Log of match decisions and user manual link actions.
- `ReconciliationRun`: Execution record of deterministic reconciliation, overall status, and summary metrics.
- `ReconciliationDiscrepancy`: Detailed line-item and header discrepancies with financial amounts at risk.
- `Supplier`, `PurchaseOrder`, `DeliverySchedule`, `Discrepancy`: Baseline procurement models.

### Core Services (`apps/api/services/`)
- `document_service.py`: Document ingestion, storage, OCR execution, and extraction orchestration.
- `verification_service.py`: Concurrency-safe review, approve, reject, and correction auditing.
- `matching_service.py`: Automated relationship detection, ambiguity resolution, and transaction chain assembly.
- `reconciliation_service.py`: Deterministic 2-way/3-way/4-way reconciliation and financial discrepancy calculation.

### API Routes Mounted (`apps/api/main.py`)
- `/v1/documents`: Document upload, raw files, OCR run, extraction, verification, relationships, matching, and reconciliation endpoints.
- `/v1/suppliers`: Supplier management.
- `/v1/purchase-orders`: Purchase order tracking.
- `/v1/delivery-schedules`: Delivery schedules and status tracking.
- `/v1/exceptions`: Exception tracking.
- `/v1/dashboard`: KPI metrics.
- `/health`: API health check.

---

## 4. Database & Migration Status
- Current Head Migration: `f5a6b7c8d9e2_add_reconciliation_runs_and_discrepancies.py`
- All migrations applied cleanly to PostgreSQL.
- Test Suite: 164 total tests passing (152 regression + 12 targeted reconciliation tests).

---

## 5. Frozen Business Rules
1. **USD Only:** All monetary amounts are strictly in `USD`. Non-USD currencies are flagged or rejected; conversion is outside MVP scope.
2. **Approved Documents Only:** Upstream reconciliation and matching must only consume documents with `DocumentVerification.status == 'APPROVED'`. Raw OCR and unverified extraction are never used for reconciliation.
3. **Confirmed Relationships Only:** Reconciliation operates solely on `CONFIRMED` relationships. `FLAGGED_AMBIGUOUS`, `PROPOSED`, or `REJECTED` links are excluded.
4. **No Float Arithmetic:** All monetary values and quantities must use Python `Decimal`.
5. **No Frontend & No Authentication:** In current MVP scope, authentication/RBAC and web UI are intentionally deferred.

---

## 6. Current Next Task
- Awaiting next command instructions from user.
