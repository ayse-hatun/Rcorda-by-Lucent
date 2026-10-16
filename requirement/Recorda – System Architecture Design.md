# Recorda – System Architecture Design

**Version:** 2.0 (Updated Architecture Specification)  
**Target:** Mid-size Enterprise Reconciliation & Exception Control  
**Stack:** Next.js (TypeScript) · FastAPI (Python 3.12) · Celery + Redis · PostgreSQL 16 (RLS) · S3 / MinIO

---

## 1. System Design Principles

1. **Deterministic Core, Narrow ML at Edge:** Machine Learning is strictly confined to **document classification** (*raw text $\rightarrow$ TF-IDF $\rightarrow$ classifier*). All reconciliation, arithmetic, duplicate detection, and exception rules are executed by deterministic Python code using `Decimal` arithmetic.
2. **Every Mismatch is an Exception Case:** Anomalies are not loose flags; they generate a formal **Exception Case (`EX-XXXX`)** with an assigned owner, evidence checklist, and audit trail.
3. **Investigation, Not Accusation:** The system initiates a structured investigation process; it never assumes vendor wrongdoing and never makes automated payment/approval decisions.
4. **Idempotent 7-Step Pipeline:** Ingest, Understand, Structure, Connect, Reconcile, Manage, Resolve. Every stage is retryable and verifiable.
5. **Auditable & Localized:** All monetary amounts are tracked in **PKR (Rs.)** with an append-only cryptographic event log.

---

## 2. High-Level Architecture Diagram

```
      [ Browser Users ]                      [ Storage / MinIO / S3 ]
  Procurement · Stores · Finance · Mgmt                 |
             |                                          |
             v                                          v
   +-------------------+                     +--------------------+
   | Next.js Frontend  | <--- REST / JSON -> |  FastAPI Backend   |
   | (App Router, TS)  |                     |  (Stateless API)   |
   +-------------------+                     +--------------------+
             |                                 |          |
             |                                 v          v
             |                         PostgreSQL 16    Redis 7
             |                         (Tables + RLS) (Broker/Cache)
             |                                            |
             +--------------------+                       v
                                  |            Celery Worker Pool
                                  v            ------------------
                         Document Viewer       1. Ingest & Hash
                         (react-pdf + bboxes)  2. Classify (ML: TF-IDF + Model)
                                               3. Structure (Fields & BBoxes)
                                               4. Connect (PO/GRN Linker)
                                               5. Reconcile (Rules Engine)
                                               6. Manage (Create EX Cases)
                                               7. Resolve & Audit Logger
```

---

## 3. The 7-Step Processing Pipeline

| Stage | Task Queue | Mechanism | Output |
| :--- | :--- | :--- | :--- |
| **1. Ingest** | `ingest` | Hash check (SHA-256), store raw file in S3, register `documents` row. | File stored immutably. |
| **2. Understand** | `ml-classify` | Fast text extraction (PyMuPDF / OCR) $\rightarrow$ TF-IDF feature extraction $\rightarrow$ ML Document Classifier. | Classified as `PO`, `GRN`, or `INV`. |
| **3. Structure** | `structure` | Extract document metadata and tabular line items with normalized bounding boxes $[x_0, y_0, x_1, y_1]$. | `extracted_fields` & `line_items` records. |
| **4. Connect** | `default` | Link related documents by PO reference number into a unified Transaction Bundle (`cases`). | Document bundle assembled. |
| **5. Reconcile** | `default` | Run deterministic matching rules: PO vs GRN quantities, GRN vs Invoice quantities, PO vs Invoice unit price, duplicate invoice detection. | Finding list with delta values. |
| **6. Manage** | `default` | If discrepancies exist, create an `exceptions` case (`EX-XXXX`), calculate amount under review in PKR, auto-assign owner (`Stores`/`Finance`/`Procurement`), populate evidence checklist. | Active `EX-XXXX` created. |
| **7. Resolve** | `default` | Human review: side-by-side inspection, inline comments, document attachments, resolution state recording, and immutable audit event appending. | Case resolved & exported. |

---

## 4. Data Architecture & Relational Schema

### Core Tables

#### `exceptions` (The Core Exception Case Object)
```sql
CREATE TABLE exceptions (
    id VARCHAR(32) PRIMARY KEY,                    -- e.g. 'EX-1048'
    org_id UUID NOT NULL REFERENCES organizations(id),
    case_id UUID NOT NULL REFERENCES cases(id),
    type VARCHAR(64) NOT NULL,                     -- 'QUANTITY_DISCREPANCY', 'PRICE_VARIANCE', 'MISSING_GRN', etc.
    status VARCHAR(32) NOT NULL DEFAULT 'NEW',     -- 'NEW', 'UNDER_REVIEW', 'PENDING_EVIDENCE', 'RESOLVED_ACCEPTED', 'RESOLVED_CREDIT_NOTE', 'RESOLVED_RE_INVOICED', 'REJECTED', 'CLOSED'
    owner_department VARCHAR(32) NOT NULL,        -- 'STORES', 'FINANCE', 'PROCUREMENT'
    assigned_user_id UUID REFERENCES users(id),
    amount_under_review NUMERIC(18, 4) NOT NULL,   -- Amount in PKR (Rs.)
    currency VARCHAR(8) NOT NULL DEFAULT 'PKR',
    po_present BOOLEAN NOT NULL DEFAULT FALSE,
    grn_present BOOLEAN NOT NULL DEFAULT FALSE,
    invoice_present BOOLEAN NOT NULL DEFAULT FALSE,
    resolution_state VARCHAR(64),
    resolution_note TEXT,
    resolved_by UUID REFERENCES users(id),
    resolved_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,             -- Optimistic locking
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

#### `exception_comments` (Collaborative Investigation Thread)
```sql
CREATE TABLE exception_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exception_id VARCHAR(32) NOT NULL REFERENCES exceptions(id) ON DELETE CASCADE,
    author_id UUID NOT NULL REFERENCES users(id),
    author_role VARCHAR(32) NOT NULL,              -- 'STORES', 'FINANCE', 'PROCUREMENT', 'MANAGEMENT'
    content TEXT NOT NULL,
    attachment_s3_key VARCHAR(512),               -- Attached proof (delivery note scan, supplier email)
    attachment_filename VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

#### `assignments` (Department & User Reassignment History)
```sql
CREATE TABLE assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exception_id VARCHAR(32) NOT NULL REFERENCES exceptions(id) ON DELETE CASCADE,
    assigned_from_dept VARCHAR(32),
    assigned_to_dept VARCHAR(32) NOT NULL,
    assigned_from_user UUID REFERENCES users(id),
    assigned_to_user UUID REFERENCES users(id),
    assigned_by UUID NOT NULL REFERENCES users(id),
    reason TEXT,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

#### Supporting Entity Tables
- **`organizations`** (`id`, `name`, `created_at`)
- **`users`** (`id`, `org_id`, `name`, `email`, `role`: `PROCUREMENT`, `STORES`, `FINANCE`, `MANAGEMENT`, `ADMIN`)
- **`cases`** (`id`, `org_id`, `po_number`, `supplier_name`, `status`, `created_at`, `version`)
- **`documents`** (`id`, `case_id`, `type`: `PO`, `GRN`, `INVOICE`, `s3_key`, `sha256`, `page_count`, `status`)
- **`extracted_fields`** (`id`, `document_id`, `name`, `value`, `confidence`, `page`, `bbox` JSONB)
- **`line_items`** (`id`, `document_id`, `item_code`, `description`, `qty`, `unit`, `unit_price`, `total`)
- **`audit_events`** (`id`, `org_id`, `actor_id`, `action`, `entity_type`, `entity_id`, `before_state` JSONB, `after_state` JSONB, `prev_hash`, `curr_hash`, `created_at`)
- **`rule_sets`** (`id`, `org_id`, `version`, `price_tolerance_pct`, `price_tolerance_amt`, `qty_tolerance`, `currency`)
- **`ml_evaluations`** (`id`, `model_version`, `dataset_name`, `accuracy`, `precision`, `recall`, `f1_score`, `confusion_matrix` JSONB, `evaluated_at`)

---

## 5. Management Dashboard Queries

The Management Dashboard aggregates metrics across unresolved exceptions, capital at risk, and ML performance:

### 1. Capital Under Review (in PKR)
```sql
SELECT 
    COALESCE(SUM(amount_under_review), 0) AS total_amount_at_risk_pkr,
    COUNT(*) AS total_open_exceptions
FROM exceptions 
WHERE org_id = :org_id 
  AND status IN ('NEW', 'UNDER_REVIEW', 'PENDING_EVIDENCE');
```

### 2. Exceptions by Owner Department
```sql
SELECT 
    owner_department,
    status,
    COUNT(*) AS exception_count,
    SUM(amount_under_review) AS departmental_at_risk_pkr
FROM exceptions 
WHERE org_id = :org_id
GROUP BY owner_department, status;
```

### 3. Mean Time to Resolution (MTTR)
```sql
SELECT 
    AVG(EXTRACT(EPOCH FROM (resolved_at - created_at))/3600)::NUMERIC(10, 2) AS avg_resolution_hours
FROM exceptions 
WHERE org_id = :org_id 
  AND resolved_at IS NOT NULL 
  AND created_at >= NOW() - INTERVAL '30 days';
```

---

## 6. Machine Learning Scope & Classifier Interface

In accordance with product guidelines, ML is strictly bounded to document classification.

```python
from abc import ABC, abstractmethod
from enum import Enum
from pydantic import BaseModel

class DocumentType(str, Enum):
    PURCHASE_ORDER = "PO"
    GOODS_RECEIVED_NOTE = "GRN"
    SUPPLIER_INVOICE = "INVOICE"
    UNKNOWN = "UNKNOWN"

class ClassificationResult(BaseModel):
    document_type: DocumentType
    confidence: float
    model_version: str
    feature_summary: dict | None = None

class DocumentClassifierInterface(ABC):
    @abstractmethod
    def classify(self, raw_text: str, file_metadata: dict) -> ClassificationResult:
        """
        Classifies ingested document into PO, GRN, or INVOICE.
        Underlying implementation uses TF-IDF + Logistic Regression / Client-trained model.
        """
        pass
```

If `confidence < 0.80`, the document is marked as `UNKNOWN`, initiating a prompt for manual classification by the uploader.

---

## 7. Deterministic Reconciliation Engine (`packages/matching`)

The reconciliation engine enforces strict business invariants using standard Python `Decimal`:

```python
from decimal import Decimal
from typing import List
from pydantic import BaseModel

class ReconciliationRules:
    @staticmethod
    def check_quantities(ordered_qty: Decimal, received_qty: Decimal, invoiced_qty: Decimal) -> List[dict]:
        findings = []
        # Short Delivery Check
        if received_qty < ordered_qty:
            findings.append({
                "type": "SHORT_DELIVERY",
                "delta": ordered_qty - received_qty,
                "msg": f"Stores received {received_qty} units out of {ordered_qty} ordered."
            })
        # Overbill Check (Paying for unreceived goods)
        if invoiced_qty > received_qty:
            findings.append({
                "type": "OVERBILLING_UNRECEIVED",
                "delta": invoiced_qty - received_qty,
                "msg": f"Invoiced {invoiced_qty} units exceeds verified receipt of {received_qty} units."
            })
        return findings

    @staticmethod
    def check_unit_price(po_price: Decimal, inv_price: Decimal, tolerance_pct: Decimal) -> List[dict]:
        findings = []
        if inv_price > po_price:
            variance_pct = ((inv_price - po_price) / po_price) * Decimal(100)
            if variance_pct > tolerance_pct:
                findings.append({
                    "type": "PRICE_VARIANCE_EXCEEDED",
                    "po_price": po_price,
                    "inv_price": inv_price,
                    "variance_pct": variance_pct,
                    "msg": f"Invoice price Rs. {inv_price} exceeds PO price Rs. {po_price} by {variance_pct:.2f}%."
                })
        return findings
```

---

## 8. API Surface

| Endpoint | Method | Role Access | Purpose |
| :--- | :--- | :--- | :--- |
| `/v1/uploads` | `POST` | All | Get S3 pre-signed upload URL and register documents |
| `/v1/dashboard/metrics` | `GET` | Management, Finance | Retrieve operational KPIs (open exceptions, Rs. under review, MTTR) |
| `/v1/dashboard/ml-metrics` | `GET` | Management, Admin | Retrieve classifier accuracy, F1, and confusion matrix |
| `/v1/exceptions` | `GET` | All | List Exception Cases with department, status, and severity filters |
| `/v1/exceptions/{id}` | `GET` | All | Full Exception Case with 3-way matrix, document links, and checklist |
| `/v1/exceptions/{id}/assign` | `POST` | Management, Dept Leads | Reassign exception to Stores, Finance, or Procurement |
| `/v1/exceptions/{id}/comments`| `POST` | All | Post comment or attach supplemental evidence |
| `/v1/exceptions/{id}/resolve` | `POST` | Finance, Management | Record final resolution state (`Credit Note`, `Accepted`, etc.) |
| `/v1/fields/{id}` | `PATCH` | Reviewers | Correct misread OCR field with mandatory audit logging |
| `/v1/exceptions/{id}/export` | `GET` | Finance, Management | Download compiled audit report (PDF / CSV) |

---

## 9. Security & Multi-Tenancy

- **Row-Level Security (RLS):** Every PostgreSQL table enforces `WHERE org_id = current_setting('app.current_org_id')`.
- **RBAC Matrix:**
  - `Procurement`: Access to PO verification and price variance views.
  - `Stores`: Access to GRN receipt counts and short delivery investigations.
  - `Finance`: Access to invoice approvals, credit note tracking, and resolution states.
  - `Management`: Executive dashboard, reassignments, and macro resolution audits.
- **Data Protection:** All S3 objects encrypted at rest with AES-256; signed URLs expire after 15 minutes.
