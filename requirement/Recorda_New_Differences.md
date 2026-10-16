# Recorda – New Product Requirements (Delta Only)

Only product-building items that were **not** in the earlier requirements, architecture or free-MVP docs.

---

## 1. Exception Case (new core object)

Earlier docs had *findings* and *reviews*. Recorda turns every mismatch into a **case**:

| Field | Example |
|---|---|
| Case ID | `EX-1048` |
| Status | `UNDER REVIEW` (plus defined resolution states) |
| Type | Quantity discrepancy |
| Owner | Stores / Finance |
| Amount under review | Rs. 5,000 |
| Evidence checklist | PO ✓  GRN ✓  Invoice ✓ |
| Comments | Free-text notes and attached evidence |

**Case lifecycle:** Mismatch → Case created → Owner assigned → Evidence attached → Review → Resolution → Audit history

**Behavior rule:** the system flags an exception and starts an investigation process; it never assumes wrongdoing.

## 2. Workflow (7 steps)

| # | Step | What it does |
|---|---|---|
| 1 | Ingest | Receive PO / GRN / Invoice |
| 2 | Understand | Extract text and **classify** document type |
| 3 | Structure | Capture transaction fields |
| 4 | Connect | Match related records |
| 5 | Reconcile | Compare business facts |
| 6 | **Manage (new)** | Create and assign the exception |
| 7 | Resolve | Evidence and audit history |

## 3. ML Scope (narrower than my earlier design)

| Layer | Handles |
|---|---|
| **ML** | Document classification only: *document text → TF-IDF → classifier* |
| **Rules** | Quantity mismatches, price differences, missing documents, **duplicate invoices** |
| **Human** | Partial-delivery context, valid exceptions, final resolution approval |

Change from earlier docs: ML for extraction, item matching, LightGBM scoring and anomaly detection moves out of the MVP. TF-IDF + Logistic Regression is a valid baseline for the classifier.

## 4. User Roles and Views

| Role | Question it answers |
|---|---|
| Procurement | What did we order? |
| Stores | What actually arrived? |
| Finance | What are we being billed for? |
| **Management (new)** | **What is still unresolved?** |

## 5. Management Dashboard (new)

Product metrics to display:
- Documents processed
- Transactions reconciled
- Open vs resolved exceptions
- Average resolution time
- Amount under review

ML metrics to track for the classifier: accuracy, precision, recall, F1 score, confusion matrix.

## 6. MVP Feature List (from the deck)

Authentication · Document upload · Text extraction (OCR) · Trained ML classifier · Field extraction · Transaction matching · Reconciliation rules · Exception workflow · Comments and evidence · Resolution states · Audit history · Management dashboard

## 7. Scope Limits (new non-goals)

Not an ERP replacement, accounting platform, inventory system, payment gateway, fraud investigation engine or generic AI agent. Recorda owns one problem: **reconciliation and exception control.**

## 8. Local Requirement

Amounts shown in **PKR (Rs.)**.

---

## Docs That Need Updating

| Doc | Change |
|---|---|
| Requirements | Add exception case, owner assignment, comments, resolution states, dashboard, product metrics |
| Architecture / free MVP | Add `exceptions`, `exception_comments`, `assignments` tables and dashboard queries; narrow ML to the classifier |
