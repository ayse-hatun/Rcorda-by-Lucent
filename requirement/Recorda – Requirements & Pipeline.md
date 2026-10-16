# Recorda – Requirements & Pipeline Specification

**Version:** 2.0 (Updated Product Specification)  
**Core Purpose:** Reconciliation & Exception Control for Procurement, Stores, and Finance.

---

## 1. Product Summary

**Recorda** reconciles three operational documents against each other:
1. **Purchase Order (PO)** – What was ordered (Procurement)
2. **Goods Received Note (GRN)** – What actually arrived (Stores)
3. **Supplier Invoice** – What is being billed (Finance)

When differences or missing documents occur, Recorda **flags an exception and initiates a structured investigation case**. It never assumes wrongdoing, never makes silent decisions, and never triggers automatic payments.

**Primary Behavior Rule:** The system flags exceptions to assist human operators in investigating and resolving discrepancies with clear evidence.

---

## 2. Core Entities & The Exception Case

Earlier drafts used generic findings; Recorda formalizes every discrepancy as an **Exception Case**:

### The Exception Case Object (`EX-XXXX`)
| Attribute | Description & Example |
| :--- | :--- |
| **Case ID** | Unique reference (e.g., `EX-1048`) |
| **Status** | `NEW` · `UNDER REVIEW` · `PENDING EVIDENCE` · `RESOLVED - ACCEPTED` · `RESOLVED - CREDIT NOTE` · `RESOLVED - RE-INVOICED` · `REJECTED` · `CLOSED` |
| **Type** | Quantity Discrepancy · Price Variance · Missing Document · Duplicate Invoice · Total Mismatch |
| **Owner** | Assigned department or user: `Stores` · `Finance` · `Procurement` |
| **Amount Under Review** | Discrepancy amount in **PKR (Rs.)** (e.g., `Rs. 12,500`) |
| **Evidence Checklist** | Document verification: `PO [✓]` · `GRN [✓]` · `Invoice [✓]` |
| **Comments & Evidence** | Collaborative thread with timestamps, reviewer comments, and attached proof (delivery slips, email confirmations) |
| **Audit History** | Immutable, append-only chronological log of all assignments, edits, and decisions |

### Case Lifecycle
$$\text{Mismatch Detected} \longrightarrow \text{Case Created} \longrightarrow \text{Owner Assigned} \longrightarrow \text{Evidence Attached} \longrightarrow \text{Review} \longrightarrow \text{Resolution} \longrightarrow \text{Audit Log}$$

---

## 3. Scope Limits & Non-Goals

Recorda has a laser-focused operational scope:
- **NOT an ERP replacement** or accounting ledger.
- **NOT an inventory system** or warehouse management platform.
- **NOT a payment gateway** or auto-disbursement engine.
- **NOT a fraud investigation engine** or generic AI chatbot.
- **Recorda owns one problem:** **Three-way reconciliation and exception management.**

---

## 4. The 7-Step Workflow

Recorda operates on a disciplined 7-step pipeline:

```mermaid
flowchart LR
    S1[1. Ingest] --> S2[2. Understand]
    S2 --> S3[3. Structure]
    S3 --> S4[4. Connect]
    S4 --> S5[5. Reconcile]
    S5 --> S6[6. Manage]
    S6 --> S7[7. Resolve]
```

| # | Stage | Function & Scope |
|---|---|---|
| **1** | **Ingest** | Receive files (PDF, scan images PNG/JPG, Excel XLSX/CSV) via UI upload or folder ingest. Immutable storage with SHA-256 fingerprinting. |
| **2** | **Understand** | Extract raw text (native parser for text PDFs/Excel; OCR for scans) and **classify** document type (`PO`, `GRN`, `Invoice`) via trained ML classifier. |
| **3** | **Structure** | Parse and capture structured transaction fields (Document Number, Date, Supplier, PO Ref, Line Items: SKU/Desc, Qty, Unit, Unit Price, Line Total, Document Total). |
| **4** | **Connect** | Match and group related documents into a transaction bundle using PO reference (fallback: Supplier + Date proximity + line overlap). |
| **5** | **Reconcile** | Compare facts using **deterministic rules**: Quantity check (PO vs GRN, GRN vs Invoice), Unit Price check (PO vs Invoice), Duplicate invoice check, and Missing document check. |
| **6** | **Manage** | Automatically generate and route an **Exception Case (`EX-XXXX`)**, calculate amount under review (Rs.), assign department owner (Stores/Finance/Procurement), and generate evidence checklist. |
| **7** | **Resolve** | Reviewer interface: inspect side-by-side evidence, collaborate in comment threads, attach supplemental proofs, record resolution state, and export audit packages. |

---

## 5. Machine Learning Scope (Narrow & Pragmatic)

To ensure predictable behavior, explainability, and rapid delivery:

| Layer | Technology | Responsibilities |
| :--- | :--- | :--- |
| **Machine Learning** | **Document Classifier Only** (TF-IDF + Classifier / Client-Trained Model) | Classifies ingested documents into `PO`, `GRN`, or `Invoice`. Out of scope for MVP: ML extraction models, LightGBM scoring, autonomous agents. |
| **Deterministic Rules** | **Plain Python Module (`Decimal`)** | Calculates quantity differences, price variances against tolerances, missing GRN/PO flags, arithmetic checks, and duplicate invoice detection. Zero hallucinations. |
| **Human Operators** | **Reviewer Workspace** | Assesses partial deliveries, resolves legitimate variances, attaches supplier notes, and approves final resolutions. |

---

## 6. User Roles and Stakeholder Perspectives

Recorda structures views around the specific business question each role needs answered:

| Role | Core Business Question | Key Views & Actions |
| :--- | :--- | :--- |
| **Procurement** | *"What did we order?"* | Verifies PO terms, approves authorized price adjustments, checks supplier fulfillment. |
| **Stores** | *"What actually arrived?"* | Reviews GRN counts, investigates short/damaged deliveries, attaches signed delivery receipts. |
| **Finance** | *"What are we being billed for?"* | Inspects invoice amounts against verified receipts, requests credit notes, reviews tax/totals. |
| **Management** | *"What is still unresolved?"* | Monitors executive dashboard, unresolved exception volume, capital at risk, and department turnaround times. |

---

## 7. Management Dashboard & Metrics

A dedicated cockpit for executive and operational oversight:

### Product & Operational Metrics
- **Documents Processed:** Total throughput across PO, GRN, and Invoices.
- **Transactions Reconciled:** Count of complete 3-way transaction sets evaluated.
- **Open vs. Resolved Exceptions:** Split of active cases under review vs. resolved cases.
- **Average Resolution Time:** Mean hours/days taken from exception creation to resolution.
- **Amount Under Review:** Total monetary value currently contested or on hold in **PKR (Rs.)**.

### ML Classifier Performance Metrics
- **Accuracy, Precision, Recall, and F1 Score** tracked against ground-truth document types.
- **Confusion Matrix:** `PO` vs. `GRN` vs. `Invoice` vs. `Unknown` to monitor classification drift.

---

## 8. Functional Requirements Matrix

| ID | Requirement | Priority | Target Role |
| :--- | :--- | :--- | :--- |
| **FR-1** | Multi-format upload (PDF, Scan PNG/JPG, Excel) with hash deduplication | Must | All |
| **FR-2** | Text extraction via native parsing or OCR | Must | System |
| **FR-3** | Document classification using trained ML classifier model | Must | System |
| **FR-4** | Transaction field extraction with confidence scores & page/bbox coordinates | Must | System |
| **FR-5** | Automatic document grouping into transaction sets by PO reference | Must | System |
| **FR-6** | Three-way deterministic reconciliation rules (Quantity, Price, Duplicate) | Must | System |
| **FR-7** | Automatic creation of Exception Case (`EX-XXXX`) with owner assignment | Must | Management / Stores / Finance |
| **FR-8** | Exception management workspace with PO / GRN / Invoice evidence checklist | Must | Reviewers |
| **FR-9** | Collaboration thread: free-text comments, mentions, and file attachments | Must | Reviewers |
| **FR-10**| Defined resolution states (`Accepted`, `Credit Note`, `Re-Invoiced`, `Rejected`)| Must | Finance / Management |
| **FR-11**| Inline field value editing with mandatory before/after audit logging | Must | Reviewers |
| **FR-12**| Append-only audit log with SHA-256 event integrity chains | Must | Management / Compliance |
| **FR-13**| Management Dashboard with product metrics & ML classifier metrics | Must | Management |
| **FR-14**| All currency amounts formatted and tracked in Pakistani Rupees (**Rs. / PKR**) | Must | Finance / Management |
| **FR-15**| Export reconciled case package and exception summary (PDF / CSV) | Should | Finance |

---

## 9. Definition of Done (MVP)

An operator can ingest a Purchase Order, Goods Received Note, and Supplier Invoice; the trained classifier identifies each document; deterministic rules flag any quantity or price difference; an **Exception Case (`EX-XXXX`)** is created with the discrepancy amount in **Rs. (PKR)** and assigned to the relevant department; the reviewer inspects the 3-way evidence side-by-side, adds comments, attaches proof, and records a definitive resolution without any automated payments ever taking place.
