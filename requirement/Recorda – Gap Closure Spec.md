# Recorda – Gap Closure Spec

Extraction · Line Alignment · Auth · Security & API Contracts · Test Plan · Revised 25-Day Plan

Builds on: Requirements & Pipeline v2.0, System Architecture v2.0, User Flows v2.0, 25-Day Roadmap v2.0.

## 0. Gaps and Where They Are Closed

| # | Gap found | Closed in |
| --- | --- | --- |
| 1 | No method for field extraction (step 3, Structure) | Section 1 |
| 2 | No authentication day / design | Section 5 |
| 3 | No line-item alignment step | Section 2 |
| 4 | OCR was paid by default | Section 4 |
| 5 | Classifier labels ignore Pakistani documents (challan, MRN, credit note) | Section 3 |
| 6 | "Real-time" comments implied websockets | Section 6.5 |
| 7 | Framework versions may be stale | Section 9 |
| 8 | No buffer days, no cut list | Section 8 |
| + | Steps 04, 05, 06 of your doc order missing | Sections 6, 7, 10 |

## 1. Field Extraction (Stage 3: Structure)

**The problem:** parsers and OCR give text and word boxes. They do not tell you which number is the quantity. With ML limited to classification, extraction must be done by deterministic, testable methods.

### 1.1 Principle

One `FieldExtractor` interface, three input tiers, **one shared mapping layer**. Everything produces the same output schema, so the rest of the pipeline never cares where the data came from.

```
Excel / CSV  ──┐
Digital PDF  ──┼─→  Table + word-box reconstruction  ─→  Header mapper  ─→  Normalizers  ─→  Validators  ─→  ExtractedDocument
Scan / image ──┘     (OCR first for scans)                  (synonyms)        (numbers,dates)  (arithmetic)
```

### 1.2 Tier A: Excel / CSV

1. Detect the header row: the first row where at least 3 cells match the known header vocabulary.
2. Map columns with the **header synonym dictionary** (fuzzy match with RapidFuzz, threshold 0.85).
3. Skip subtotal, footer and blank rows.
4. If a required column cannot be mapped, show a one-time **column mapping screen**. The mapping is saved as a supplier template (1.6).

### 1.3 Tier B: Digital PDF

1. `pdfplumber` table extraction (lines strategy first, text strategy as fallback).
2. Same header mapper as Tier A.
3. Header fields (PO number, invoice number, dates, totals, NTN/STRN) through **label-anchored extraction** (1.5).

### 1.4 Tier C: Scans and Images

1. Preprocess (deskew, denoise, upscale to about 300 DPI with OpenCV).
2. OCR gives words with boxes and confidence (Section 4).
3. **Rebuild the table from geometry:** cluster words into rows by y-position, find column boundaries from the header row's x-ranges, assign each word to a column.
4. Same header mapper from here on.

### 1.5 Header Fields: Label-Anchored Extraction

For each field there is a list of labels per document type. The value is the nearest valid token to the right of the label, otherwise directly below it, within a distance limit.

| Field | Labels (examples) | Value pattern |
| --- | --- | --- |
| PO number | PO No, P.O. #, Purchase Order, LPO | Alphanumeric with dashes, e.g. `PO-44819` |
| GRN number | GRN No, MRN, Gate Inward No, Challan No | Alphanumeric |
| Invoice number | Invoice No, Inv #, Bill No | Alphanumeric |
| Date | Date, Dated, Invoice Date | `dd/mm/yyyy`, `dd-Mon-yyyy`, `yyyy-mm-dd` |
| Supplier | Vendor, Supplier, Bill From, letterhead top block | Text; matched to supplier master |
| NTN / STRN | NTN, STRN, Sales Tax Reg | Digit patterns with optional dashes |
| Totals | Sub Total, GST, Sales Tax, Grand Total, Net Payable | Money |

When two candidates conflict, prefer the one nearest the label, then the one on page 1, and lower the confidence.

### 1.6 Header Synonym Dictionary (seed)

| Canonical | Synonyms |
| --- | --- |
| `item_code` | Item Code, SKU, Code, Part No, Material No, Article |
| `description` | Description, Item, Particulars, Details, Material |
| `qty` | Qty, Quantity, Nos, Pcs, Qty Received, Qty Rcvd, Delivered |
| `unit` | Unit, UOM, U/M |
| `unit_price` | Rate, Price, Unit Price, Rate/Unit |
| `line_total` | Amount, Total, Value, Line Total, Net Amount |

Stored in a table, not code, so it grows without a deploy.

### 1.7 Supplier Templates (learning without ML)

- After a reviewer confirms a document, save its mapping (column synonyms, label positions) keyed by **supplier NTN, or normalized name**.
- Next document from that supplier uses the template first, so accuracy rises with every confirmed document.
- Template hits are logged; a miss falls back to the generic path.

### 1.8 Pakistan-Specific Normalizers (must have)

| Case | Rule |
| --- | --- |
| Lakh grouping | `1,25,000.00` → `125000.00` (strip all commas before parsing) |
| Currency markers | Remove `Rs.`, `Rs`, `PKR`, `/-` |
| Urdu and Arabic-Indic digits | Map `۰۱۲۳۴۵۶۷۸۹` and `٠١٢٣٤٥٦٧٨٩` to `0–9` |
| Dates | Day-first by default; reject ambiguous swaps when day > 12 disagrees |
| Units | Map Pcs/Nos/Pc → `PCS`, Ctn/Carton → `CTN`, Bx/Box → `BOX`, Kg, Mtr, Ltr, Bag; conversions only from the `unit_conversions` table, never guessed |
| Non-goods lines | Freight, Loading, GST, Discount, Rounding detected by keyword and excluded from quantity matching |
| OCR digit confusion | O↔0, l↔1, S↔5, B↔8 corrected only inside numeric fields |

### 1.9 Self-Check Validators

- `qty × unit_price = line_total` (tolerance Rs. 1 for rounding)
- `sum(lines) + tax - discount = document total`
- Required fields present for the document type

**If a validator fails, the field is marked `NEEDS_CHECK` and shown highlighted to the reviewer. The system never guesses a replacement.**

### 1.10 Output Contract

```json
{
  "doc_id": "uuid",
  "doc_type": "INVOICE",
  "header": {
    "invoice_no": {"value": "INV-2291", "conf": 0.97, "page": 1, "bbox": [120,80,310,110], "source": "label_anchor"},
    "po_ref": {"value": "PO-44819", "conf": 0.94, "page": 1, "bbox": [120,120,300,150], "source": "label_anchor"}
  },
  "lines": [
    {"line_no": 1, "item_code": "GB-99", "description": "Industrial Gearbox", "qty": "500", "unit": "PCS", "unit_price": "2500.00", "line_total": "1250000.00", "conf": 0.95, "bbox": [60,300,940,330]}
  ],
  "validation": {"arithmetic_ok": true, "needs_check": []}
}
```

Money is always a string (`Decimal`), boxes are normalized to a 0–1000 grid.

### 1.11 Optional Fallback (decision needed)

A language-model extractor can sit behind the same `FieldExtractor` interface, **off by default**. It would be used only on documents where rules fail. This crosses your "ML only for classification" boundary, so it stays out of the MVP unless you decide otherwise (see Section 11).

## 2. Line-Item Alignment (Stage 4: Connect)

Reconciliation is only as good as the line pairing. Quantities are compared **per aligned line**, never per document.

### 2.1 Algorithm

1. **Exact key:** normalized `item_code` equal → score 1.00.
2. **Alias memory:** `item_aliases` has a confirmed mapping for this supplier and text → score 1.00.
3. **Description similarity:** normalize (lowercase, remove punctuation and unit words), then `token_set_ratio` (RapidFuzz) plus trigram similarity (`pg_trgm`).
4. **Tie-breakers:** unit price within 15%, same unit, same line order.
5. **Global assignment:** build a score matrix and solve with the Hungarian algorithm (`scipy.optimize.linear_sum_assignment`). One invoice line can never claim two PO lines.
6. **Thresholds:**

| Score | Action |
| --- | --- |
| 0.85 or higher | Auto-link |
| 0.60 – 0.84 | Suggest link; reviewer confirms |
| Below 0.60 | Unmatched |

### 2.2 Cardinality Rules

- **PO line → many GRN lines** (partial deliveries): received quantity is the **sum** across GRNs.
- **PO line → many invoice lines / invoices:** billed quantity is **cumulative** against received.
- Cumulative totals are kept in a per-PO ledger so a second invoice is checked against what is already billed.

### 2.3 New Exception Types From Alignment

| Type | Meaning |
| --- | --- |
| `UNORDERED_ITEM` | Invoice line has no PO line |
| `UNRECEIVED_LINE` | PO line with no GRN line yet billed |
| `LINK_UNCONFIRMED` | Match score in the 0.60–0.84 band |

### 2.4 Schema Additions

```sql
CREATE TABLE line_matches (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL, case_id UUID NOT NULL,
  po_line_id UUID, grn_line_id UUID, invoice_line_id UUID,
  score NUMERIC(4,3) NOT NULL, method VARCHAR(16) NOT NULL,  -- CODE | ALIAS | FUZZY | MANUAL
  confirmed_by UUID, confirmed_at TIMESTAMPTZ
);
CREATE TABLE item_aliases (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL, supplier_id UUID,
  alias_text TEXT NOT NULL, canonical_item_code VARCHAR(64) NOT NULL,
  created_by UUID, created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE unit_conversions (
  org_id UUID NOT NULL, item_code VARCHAR(64) NOT NULL,
  from_unit VARCHAR(16) NOT NULL, to_unit VARCHAR(16) NOT NULL, factor NUMERIC(18,6) NOT NULL
);
CREATE TABLE supplier_templates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL, supplier_key VARCHAR(64) NOT NULL, doc_type VARCHAR(16) NOT NULL,
  mapping JSONB NOT NULL, hits INT DEFAULT 0, updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE header_synonyms (
  canonical VARCHAR(32) NOT NULL, synonym TEXT NOT NULL, org_id UUID
);
```

Every reviewer confirmation writes to `item_aliases`, so the same pairing is automatic next time.

## 3. Classifier Label Contract (Fixes Gap 5)

Pakistani factories use more document names than PO / GRN / Invoice. Decide the label set **before** the client trains the model.

### 3.1 Recommended Label Set

| Label | Includes | Role in reconciliation |
| --- | --- | --- |
| `PO` | Purchase Order, LPO, work order | Ordered truth |
| `GRN` | GRN, MRN, Gate Inward, Stores Receipt | **Received truth** (buyer-issued) |
| `INVOICE` | Sales Tax Invoice, Commercial Invoice, Bill | Billed truth |
| `DELIVERY_CHALLAN` | Supplier dispatch note, delivery challan | Supporting evidence; used when GRN is missing (flag `MISSING_GRN`, never counted as received) |
| `CREDIT_NOTE` | Supplier credit note | Attached to resolution; reduces billed amount |
| `OTHER` | Quotation, statement, bank slip, anything else | Stored, not reconciled |

The system runs on the first three; the others are stored as case evidence.

### 3.2 Interface Contract

```python
class ClassificationResult(BaseModel):
    label: str            # one of the configured labels
    confidence: float     # calibrated probability 0..1
    model_version: str
    top_k: list[tuple[str, float]]

class DocumentClassifierInterface(ABC):
    labels: list[str]     # read from config, not hard-coded
    @abstractmethod
    def classify(self, text: str, meta: dict) -> ClassificationResult: ...
```

- Input: text of the first two pages (cell text for Excel). **Never use the file name as a feature** (it leaks the label in training and fails in production).
- Confidence below 0.80 → `UNKNOWN` → the uploader tags it manually; the manual label is saved as new training data.
- A **keyword heuristic adapter** ships first so the pipeline works before the client model is ready, and stays as the fallback if the model is unavailable.
- Metrics (accuracy, precision, recall, F1, confusion matrix) are written to `ml_evaluations` per model version.

### 3.3 Training and Evaluation Protocol (for the client's model)

1. Split **by supplier**, not randomly, so layouts do not leak into the test set.
2. Calibrate probabilities (`CalibratedClassifierCV`).
3. Report macro F1 and the confusion matrix. Pay attention to GRN vs DELIVERY\_CHALLAN, the most likely confusion.
4. Freeze the test set; never train on it.

## 4. OCR Strategy (Free by Default)

| Item | Decision |
| --- | --- |
| Interface | `OcrEngine.read(image) → words[text, bbox, conf]` plus optional table cells |
| Default engine | **PaddleOCR** (CPU), with its table structure model for tables |
| Alternative | Docling, test side by side on your sample scans |
| Baseline | Tesseract, used only for comparison |
| Paid adapters | AWS Textract / Azure Document Intelligence, behind a config flag, **off by default** |
| Preprocessing | OpenCV: deskew, denoise, contrast, upscale to about 300 DPI, remove stamps only when they overlap text |
| Urdu | Test the Urdu/Arabic-script model on your own scans before promising Urdu support |
| Execution | Always in the background worker; the UI shows progress |

**Honest performance targets on a CPU laptop:**

- Native PDF or Excel 3-document case: under 10 seconds.
- Scanned 3-document case: under 2 minutes.
- Your earlier "under 60 seconds" is realistic for digital documents only.

Work on bad scans is the main schedule risk (Section 8).

## 5. Authentication and Tenancy (Fixes Gap 2)

| Item | Decision |
| --- | --- |
| Library | `fastapi-users` with SQLAlchemy |
| Passwords | argon2id |
| Session | JWT in an **httpOnly, Secure, SameSite=Lax cookie**; access token 15 minutes, refresh token 7 days with rotation |
| Same origin | Next.js rewrites `/api/*` to FastAPI, so the cookie is first-party and no CORS exceptions are needed |
| CSRF | Double-submit token on all mutating requests |
| Roles | `PROCUREMENT`, `STORES`, `FINANCE`, `MANAGEMENT`, `ADMIN` |
| Onboarding | Admin invites by email; the invite carries `org_id` and role. No open self-registration |
| Brute force | Rate limit login (Redis), lock after 5 failures for 15 minutes |
| Password reset | Email link; prints to the console in development |
| Later | SSO (OIDC) via Keycloak or a managed provider when a customer asks |

### Row-Level Security Wiring

Every request runs inside one transaction that begins with `SET LOCAL app.current_org_id = '<org uuid>'`. RLS policies read that setting. A missing setting returns zero rows, never all rows.

```sql
ALTER TABLE exceptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON exceptions
  USING (org_id = current_setting('app.current_org_id', true)::uuid);
```

The app connects as a non-owner database role so RLS cannot be bypassed by accident.

## 6. Security and API Contracts (Step 04)

### 6.1 Threats and Controls

| Threat | Control |
| --- | --- |
| Malicious or oversized upload | Allowed types by magic bytes (not extension), size limit 20 MB, page limit 50, ClamAV scan, no active content processed, random storage keys |
| Cross-tenant data access | RLS plus an automated cross-tenant test in CI; 404 (not 403) for foreign objects |
| Stolen or guessed download link | Signed URLs, 15-minute expiry, bound to the object key |
| Script in comments or filenames | Output encoded by React; filenames sanitized; attachments served with `Content-Disposition: attachment` |
| Audit tampering | Append-only table, hash chain, database role without UPDATE/DELETE, nightly chain verification job |
| Double submission / retries | `Idempotency-Key` header on uploads and resolutions |
| Concurrent edits | Optimistic locking with `If-Match: <version>` |
| Secrets leakage | `.env` never committed; secret scanning in CI |
| Accidental payment path | **No payment endpoint exists**; a test asserts it |

### 6.2 RBAC Matrix

| Action | Proc. | Stores | Finance | Mgmt | Admin |
| --- | --- | --- | --- | --- | --- |
| Upload documents | Yes | Yes | Yes | No | Yes |
| View exceptions | Own dept | Own dept | Own dept | All | All |
| Comment / attach proof | Yes | Yes | Yes | Yes | Yes |
| Correct a field (audited) | Yes | Yes | Yes | No | Yes |
| Confirm line link | Yes | Yes | Yes | No | Yes |
| Reassign owner | No | No | No | Yes | Yes |
| Resolve: Accepted / Credit Note / Re-invoiced | No | No | Yes | Yes | No |
| Reject | No | No | Yes | Yes | No |
| Dashboard | No | No | Summary | Full | Full |
| Manage users, rules, templates | No | No | No | No | Yes |

Stores can add receiving notes but cannot approve credit resolutions, as your roadmap states.

### 6.3 API Conventions

- Base path `/v1`; JSON only; money as strings; timestamps in ISO 8601 UTC.
- Cursor pagination: `?limit=50&cursor=...`.
- Errors use `application/problem+json` with a stable `code` field.
- OpenAPI is generated by FastAPI and committed; frontend types are generated with `openapi-typescript`. **The frontend can start against mocks on day 1 because the contract exists.**
- Rate limits per user and per IP.

### 6.4 Core Endpoint Contracts

```
POST /v1/uploads                  -> 201 {upload_id, signed_url, expires_at}
POST /v1/uploads/{id}/complete    -> 202 {document_id, status:"PROCESSING"}
GET  /v1/documents/{id}           -> 200 {type, type_confidence, status, fields[], lines[]}
PATCH /v1/documents/{id}/classification   {label}         (manual tag)
GET  /v1/exceptions?owner=&status=&cursor=
GET  /v1/exceptions/{id}          -> 200 (see below)
POST /v1/exceptions/{id}/assign   {department, user_id?, reason}
POST /v1/exceptions/{id}/comments {content, attachment_upload_id?}
POST /v1/exceptions/{id}/resolve  {state, credit_note_ref?, credit_amount?, note}
PATCH /v1/fields/{id}             {value, reason_code, note}  -> triggers re-reconcile
POST /v1/line-matches/{id}/confirm
GET  /v1/exceptions/{id}/export?format=csv|json
GET  /v1/audit?entity=&entity_id=&cursor=
```

`GET /v1/exceptions/{id}` response (abridged):

```json
{
  "id": "EX-1048", "version": 3, "status": "UNDER_REVIEW",
  "type": "OVERBILLING_UNRECEIVED", "owner_department": "STORES",
  "amount_under_review": {"value": "125000.00", "currency": "PKR"},
  "evidence": {"po": true, "grn": true, "invoice": true},
  "lines": [{
    "item": "GB-99", "ordered": "500", "received": "450", "invoiced": "500",
    "findings": [{"code": "SHORT_DELIVERY", "delta": "50"}, {"code": "OVERBILLING_UNRECEIVED", "delta": "50"}],
    "evidence_refs": [{"doc": "GRN-8812", "page": 1, "bbox": [120,410,380,445]}]
  }]
}
```

`POST /resolve` requires `If-Match: <version>` and an `Idempotency-Key`; it returns `409` when the version is stale.

### 6.5 Comments Without Websockets (Fixes Gap 6)

The comment thread refreshes with TanStack Query `refetchInterval` of 10 seconds while the tab is focused, and on window focus. Server-Sent Events can replace polling later with no schema change.

### 6.6 Audit Events (minimum set)

`DOCUMENT_INGESTED`, `DOCUMENT_CLASSIFIED`, `CLASSIFICATION_OVERRIDDEN`, `FIELD_CORRECTED`, `LINE_LINK_CONFIRMED`, `EXCEPTION_CREATED`, `EXCEPTION_ASSIGNED`, `COMMENT_ADDED`, `EXCEPTION_RESOLVED`, `EXPORT_GENERATED`, `LOGIN`, `LOGIN_FAILED`. Each stores actor, before, after, previous hash and current hash.

## 7. Test Plan and Acceptance Criteria (Step 05)

### 7.1 Test Layers

| Layer | What | Tool |
| --- | --- | --- |
| Unit | Reconciliation rules, normalizers, validators, aligner | pytest, property tests with Hypothesis |
| Integration | Pipeline stages with real Postgres and MinIO | pytest + Docker Compose |
| Contract | API matches the OpenAPI spec | schemathesis |
| Security | Cross-tenant, RBAC, upload abuse | pytest suites |
| Frontend | Key flows | Playwright |
| ML / extraction evaluation | Golden dataset scores | evaluation script, run in CI |

### 7.2 Golden Dataset (build before the app)

- 30–50 linked cases (PO + receipt + invoice), each with a hand-written expected outcome file.
- Cover: exact match, short delivery, over-billing, price change, partial deliveries (2 and 3 GRNs), duplicate invoice, missing GRN, missing PO, unordered item, rounding, lakh-format numbers, Urdu digits, scanned and phone-photo versions, Excel version.
- Split by supplier. Never tune on the test portion.
- Extend with synthetic cases (generator from the SAP data) for volume only.

### 7.3 Acceptance Criteria

| ID | Criterion | Pass condition |
| --- | --- | --- |
| AC-1 | Core example | PO 500, GRN 450, Invoice 500 → one exception, type overbilling, Rs. amount = 50 × unit price, owner Stores |
| AC-2 | Partial deliveries | GRNs of 250 and 200 against PO 500 → received 450, short 50, no duplicate exception |
| AC-3 | Price tolerance | Price above tolerance → `PRICE_VARIANCE`; within tolerance → no exception |
| AC-4 | Duplicate invoice | Same supplier + number + amount → `DUPLICATE_INVOICE` |
| AC-5 | Missing documents | No GRN → `MISSING_GRN`; challan alone is never counted as received |
| AC-6 | Number formats | Lakh grouping, Rs./PKR markers, Urdu digits parse correctly |
| AC-7 | No guessing | Failed arithmetic validation → field `NEEDS_CHECK`; no silent value |
| AC-8 | Line alignment | At least 95% of true line pairs linked on the golden set; no line claimed twice |
| AC-9 | Correction loop | Reviewer corrects a field → re-reconcile in under 2 seconds → audit event with before/after |
| AC-10 | Tenant isolation | Org A cannot read, list or download anything of org B (API and storage), tested automatically |
| AC-11 | RBAC | Stores cannot resolve with credit note; matrix in 6.2 enforced by tests |
| AC-12 | Audit integrity | Hash chain verifies; altering any row is detected |
| AC-13 | Idempotency | Re-delivered or retried task produces no duplicate documents, cases or exceptions |
| AC-14 | Resilience | Corrupt file → dead-letter queue with a visible alert and one-click retry |
| AC-15 | No payment path | No endpoint or job can pay, release or approve payment (asserted by test) |
| AC-16 | Performance | Digital 3-document case under 10 s; scanned under 2 min on the dev machine |

### 7.4 Starting Quality Targets (set final numbers after the first baseline run)

| Measure | Starting target |
| --- | --- |
| Classifier macro F1 (split by supplier) | 0.95 or higher |
| Header field accuracy (PO no., invoice no., date, total) | 95% or higher on digital documents |
| Line quantity and price accuracy | 98% or higher on digital documents, 90% or higher on scans |
| Mismatch recall (quantity, price, duplicate) | 100% on the golden set for rule-based checks |
| False alert rate | Under 10% of exceptions |

## 8. Revised 25-Day Plan With Two Lanes

Two people can work in parallel because the API contract and mocks exist from week 1. **Lane A** = backend and ML. **Lane B** = frontend, data and tests.

| Days | Lane A (backend / ML) | Lane B (frontend / data) |
| --- | --- | --- |
| 1–2 | Monorepo, Docker Compose, Postgres schema, RLS, exception tables, OpenAPI skeleton | Collect and label golden dataset (first 15 cases); set up Next.js shell with mocked API from the contract |
| 3–4 | **Auth, roles, invite flow, RLS session wiring** | Login screens, role-based navigation, design system |
| 5–6 | Reconciliation engine plus unit tests (all AC-1 to AC-5) | Exception inbox page on mocks |
| 7 | Upload, storage, hashing, magic-byte checks, ClamAV | Upload screen with progress |
| 8–9 | Native PDF and Excel parsers, normalizers (lakh, Urdu digits, dates, units), header mapper | Review workspace layout, PDF viewer, box overlays on fixtures |
| 10–11 | OCR adapter (PaddleOCR) plus preprocessing, table rebuild from geometry | 3-way matrix component, click-to-highlight |
| 12 | **Label-anchored header extraction, supplier templates, validators** | Column mapping screen; finish golden dataset (30–50 cases) |
| 13 | **Line alignment (Hungarian), aliases, ledger** | Line-link confirmation UI |
| 14 | Classifier interface, heuristic adapter, manual override endpoint | Dashboard page on mocks |
| 15 | Document linker, exception generator, owner routing | Comments thread (polling) and attachments UI |
| 16 | Celery 7-step pipeline, idempotency, queues | Resolution modal, field correction modal |
| **17** | **Buffer / integration day: wire frontend to the real API, fix contract drift** | Same |
| 18 | Resolution states, audit hash chain, chain verifier | Audit log page |
| 19 | Inline correction → instant re-reconcile | Management dashboard with real data |
| 20 | CSV / JSON export; DLQ, retries with backoff, retry action | Playwright tests for key flows |
| 21 | **Buffer: OCR and extraction fixes on the failing golden cases** | Same |
| 22 | Plug in the client's trained classifier; metrics to `ml_evaluations` | Classifier health card on dashboard |
| 23 | RBAC and tenant-isolation test suites (AC-10, AC-11) | Accessibility and empty-state polish |
| 24 | Performance and index tuning, load check | End-to-end dry run, fix list |
| 25 | Golden dataset sign-off against AC-1 to AC-16, runbook | Demo script and demo data |

### Cut List If You Fall Behind (in this order)

1. PDF audit report → keep CSV / JSON only (PDF comes later).
2. Department bottleneck chart on the dashboard → keep the KPI cards.
3. Classifier health card → show numbers in a table.
4. Supplier-template editing UI → keep templates automatic, no editor.
5. Scanned-document polish → ship digital PDFs and Excel first, scans as "best effort, flagged for review."

**Never cut:** auth, RLS, the audit chain, line alignment, the correction loop, the golden dataset.

### Realism Check

If only one person writes code, 25 days is not realistic for this scope. Plan **35–40 days**, or ship the cut list above as the MVP.

## 9. Version and Tooling Policy (Fixes Gap 7)

- Pick the **current stable** Next.js, Python 3.12+, PostgreSQL 16+, Redis 7+ on day 1; do not copy version numbers from older docs.
- Commit lockfiles (`uv.lock`, `pnpm-lock.yaml`); Dependabot or Renovate weekly.
- Tooling: `ruff`, `mypy`, `pytest`, `pre-commit`; ESLint, TypeScript strict, Playwright.
- CI on every push: lint, types, unit and integration tests, contract tests, golden-set evaluation, dependency scan, secret scan.
- Migrations are backward compatible (add first, remove later) so workers and API can deploy separately.

## 10. AGENTS.md Rules (Step 06, Starter)

Put this at the repository root so AI coding tools follow the same rules:

1. **No payment logic, ever.** Do not add endpoints, jobs or code that pay, release or approve payment.
2. **ML is classification only.** Do not add ML to extraction or reconciliation without an approved decision record.
3. **Money is `Decimal` in Python and a string in JSON.** Never `float`.
4. **Reconciliation code lives in `packages/reconciliation`**, has no network or database imports, and every rule has a test.
5. **Every table has `org_id` and an RLS policy.** Every new table ships with a cross-tenant test.
6. **Every state change goes through the audit writer** in the same transaction.
7. **Pipeline tasks are idempotent.** Re-running a task must not create duplicates.
8. **Never guess a field.** Failed validation produces `NEEDS_CHECK`.
9. **Contract first:** change the OpenAPI spec, regenerate types, then implement.
10. **No new dependency** without a license check and a note in `docs/decisions/`.
11. **Do not commit real supplier documents or secrets.** Use anonymized fixtures.
12. **Definition of done for a task:** tests pass, types pass, audit event added where state changes, docs updated.

## 11. Decisions Needed From You

1. **LLM extraction fallback:** off for the MVP (recommended), or allowed behind a flag for failed documents?
2. **Label set:** accept the six labels in Section 3.1, especially treating `DELIVERY_CHALLAN` as supporting evidence only?
3. **OCR engine:** run a one-day bake-off of PaddleOCR vs Docling on your real scans before committing.
4. **Team reality:** one coder or two? This decides 25 days vs 35–40 days.
5. **Data:** how many real linked PO + GRN + invoice sets can you get for the golden dataset in week 1?
