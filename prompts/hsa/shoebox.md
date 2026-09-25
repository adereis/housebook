# HSA Shoebox: Design Plan & Agent SOP

## Strategy Overview

The HSA Shoebox strategy: pay medical expenses out-of-pocket, save
receipts, let HSA investments compound tax-free, then reimburse yourself
years or decades later. The IRS has no time limit on reimbursement as
long as (a) the expense was for qualified medical care, (b) it was
incurred after the HSA was established, (c) it wasn't already reimbursed
by insurance, and (d) you have documentation to prove it.

This module maintains an audit-proof ledger of qualified medical expenses
with hard links to source documents, designed to survive 30+ years of
IRS scrutiny.

---

## IRS Audit Requirements (Publication 969)

The IRS requires five things per HSA-reimbursable expense:

| # | Requirement | Primary Source | Supporting Source |
|---|-------------|---------------|-------------------|
| 1 | Provider Name | Receipt, EOB | CC Statement |
| 2 | Date of Service | Receipt, EOB | — |
| 3 | Description of Service | Receipt, EOB | — |
| 4 | Proof of Eligibility | EOB | — |
| 5 | Proof of Payment | CC Statement | Receipt |

No single document covers all five. The system correlates EOBs,
receipts, and CC statements to build "audit-proof" entries.

**Two-Factor Verification**: An EOB + CC statement is often stronger
than a standalone receipt — the EOB proves eligibility and the CC
statement proves payment.

**Caveats the Agent must handle:**
1. **Statement Balance Trap**: One CC payment may cover multiple
   visits (1-to-N). Multiple `hsa_expenses` rows can share the
   same `transaction_id`.
2. **Timing Mismatches**: Service date (EOB) and payment date (CC)
   may differ by weeks. Track both `service_date` and `payment_date`.
3. **Ambiguous Payees**: A pharmacy name on a CC statement doesn't
   prove what was purchased. Retail/pharmacy purchases require
   itemized receipts.

## Document Sidecar Convention

Documents are pre-processed by an AI agent (Claude/Codex/Gemini) into a
standardized format before ingestion. This eliminates OCR/heuristic
parsing in the ingestor.

### Filename Format
```
YYYY-MM-DD__Entity__DocType__Patient__Amount__Tags.pdf
```

### JSON Sidecar
Each `.pdf` is paired with a `.json` file containing parsed metadata:

```json
{
  "date": "2025-03-11",
  "entity": "Insurer",
  "doc_type": "EOB",
  "patient": "John",
  "amount": 95.00,
  "tags": ["City-Hospital"],
  "claim_id": "1234567890",
  "financials": {
    "billed": 350.00,
    "discount": 255.00,
    "plan_paid": 0.0,
    "patient_responsibility": 95.00
  }
}
```

**Multi-item support:** For documents covering several independent
expenses (one-to-many), use the `items` list. Each entry creates
a separate record in the database, all linked to the same file.

```json
{
  "date": "2024-12-31",
  "entity": "Maple-Dental",
  "doc_type": "TAX",
  "items": [
    { "date": "2024-02-06", "patient": "Sterling", "amount": 150.00 },
    { "date": "2024-04-03", "patient": "Penny", "amount": 120.00 }
  ]
}
```

### Document Types

| Code | Name | Creates Expense? | Purpose |
|------|------|-----------------|---------|
| REC | Receipt | Yes | Provider bill / payment confirmation |
| EOB | Explanation of Benefits | Yes | Insurance confirmation of what you owe |
| INV | Invoice | Yes | Provider bill (unpaid) |
| STMT | Statement | No | HSA account statement (custodian) |
| TAX | Tax Document | No | 5498-SA, 1099-SA |
| HIST | Historical Ledger | No | HSA card usage history |
| PLAN | Payment Plan | No | Installment plan documentation |

STMT, TAX, HIST, and PLAN are account-level documents — they create
`hsa_documents` entries only (no `hsa_expenses` row).

### Directory Structure
```
$WORKSPACE_DIR/hsa/
├── YYYY/
│   ├── YYYY-MM-DD__Entity__DocType__Patient__Amount.json
│   └── YYYY-MM-DD__Entity__DocType__Patient__Amount.pdf
└── Reimbursements/   # Output directory (skipped during ingest)
```

## Evidence Level Tracking

Each `hsa_expenses` row has an `evidence_level` field (migration 014)
that tracks how audit-proof the expense is:

| Level | Badge | Meaning | IRS-ready? |
|-------|-------|---------|------------|
| `stub` | `[ ]` | Missing documentation | No |
| `weak` | `[?]` | Consolidated payment / needs math proof | No |
| `ready` | `[✓]` | Corroborated 1:1 match | **Yes** |
| `strong` | `[✓+]` | Ironclad: 3+ corroborating sources | **Yes** |

Only `ready` and `strong` expenses count toward "available for
withdrawal." Setting `ready` or `strong` via `housebook-hsa verify`
is guarded by a math proof check for consolidated payments.

---

## Implemented Features

### Database Schema (migrations 012-014)

Seven tables: `hsa_expenses`, `hsa_documents`, `hsa_providers`,
`hsa_audit_log`, `hsa_reimbursements`, `hsa_reimbursement_items`.

Key columns on `hsa_expenses`: service_date, provider, patient,
patient_responsibility, amount_billed, insurance_paid, category,
transaction_id (FK to CC transactions), source, status,
evidence_level, needs_review, notes.

### Config Files (in workspace config/)

- **`config/hsa/providers.json`** — Provider alias mapping with
  `canonical_name`, `category`, `aliases[]`, and optional
  `expected_billing_lag_days` for candidate ranking.
- **`config/hsa/patients.json`** — Family member registry with
  patient ID, relationship, and `hsa_eligible_since` date.

### Ingestor (JSON Sidecar)

Reads AI-pre-processed JSON sidecars as the sole ingestion path.
No OCR fallback — PDFs without sidecars are flagged as errors
(the import SOP must be run first).

Key behaviors:
- REC/EOB/INV → create `hsa_expenses` + `hsa_documents`
- STMT/TAX/HIST → create `hsa_documents` only (account-level)
- $0 patient responsibility → document only, no expense
- DECLINED/VOID tags → skipped entirely
- EOBs use clinical tag (not insurer entity) as provider
- Cross-document dedup at ingest: links new documents to
  existing expenses matched by provider, patient, amount
  (±$0.50), and date (within 45 days)

### CC Scanner

Scans `transactions` for medical-category expenses and keyword
matches, creates `cc_stub` entries in `hsa_expenses`. Amazon
transactions are excluded. Deduped by `transaction_id`.

### CLI: `housebook-hsa`

| Command | Purpose |
|---------|---------|
| `summary` | Totals by year/patient/category with readiness breakdown |
| `list` | Flat list with evidence badges, doc counts |
| `check` | Data quality: duplicates, missing docs, math proofs, integrity |
| `scan` | Create CC stubs from medical transactions |
| `verify` | Set category, patient, provider, evidence level, transaction link |
| `link-doc` | Attach a document (workspace-relative paths enforced) |
| `candidates` | Auto-match CC stubs to service records (amount, date, alias) |
| `merge` | Consolidate a CC stub into a verified service record |
| `providers` | List known providers and aliases |

### Reconciliation Tools

- **`candidates`**: Matches pending CC stubs to service expenses
  using exact-cent amount matching, 45-day date proximity, and
  provider alias resolution. Ranks by billing lag config.
- **`merge`**: Safely consolidates a CC stub into a service record,
  transferring documents, updating payment metadata, and logging
  all changes to the audit trail.
- **Math proof guard**: `verify --evidence-level ready` refuses if
  the expense is linked to a consolidated CC charge whose amount
  doesn't match the sum of all linked expenses.

### Web UI

- HSA Shoebox tab with KPI cards (reimbursable vs total)
- Sortable table with semantic evidence badges ([ ], [?], [✓], [✓+])
- Date range presets (All, YTD, Last 12 mo, year buttons)
- Detail modal with inline PDF viewer for documents and CC statements
- Soft delete with audit trail, review toggle

### SOPs

- **`prompts/hsa/import.md`**: AI agent imports raw documents from
  any location, renames per convention, generates JSON sidecars.
  References `config/hsa/providers.json` and `config/hsa/patients.json`.
- **`prompts/hsa/reconcile.md`**: Tool-first workflow using `candidates`
  and `merge` commands to cross-reference EOBs, receipts, and
  CC transactions. Sets evidence levels per IRS 5-point criteria.

---

## Next: Reimbursement Engine

### Batch Withdrawal Selection (FIFO)

Query: "Give me $5,000 of tax-free reimbursement."
Algorithm:
1. Sort unreimbursed expenses by service_date ASC (FIFO)
2. Accumulate until target amount reached
3. Present selection for user approval
4. On approval: set status → PENDING, create `hsa_reimbursement` record

### Withdrawal Packet Generator

Generate a single PDF containing:
1. **Cover sheet**: Summary table of expenses in this batch
   - Date, Provider, Patient, Amount, Document reference
   - Total reimbursement amount
   - HSA account details
2. **Supporting documents**: All linked receipts/EOBs appended
3. **Attestation**: "I certify these expenses are qualified medical
   expenses not previously reimbursed."

File stored at `hsa/Reimbursements/YYYY-MM-DD_withdrawal_$AMOUNT.pdf`

### Liquidity Dashboard

CLI + web UI showing:
- Total unreimbursed balance (your "tax-free ATM")
- Breakdown by year, patient, category
- Growth over time chart

---

## Future: Reporting & Integrity

### Annual Snapshot Reports

Year-end PDF/JSON report:
- Total medical spending by category
- Insurance vs. out-of-pocket breakdown
- Shoebox growth (cumulative unreimbursed balance)
- New expenses added this year
- Reimbursements taken this year

### Document Integrity Verification

Periodic job: re-hash all linked documents, compare to stored hashes.
Flag any mismatches (file was modified or corrupted).

CLI: `housebook-hsa check --integrity`

### Data Portability Export

Export entire HSA dataset as:
```
export/HSA-YYYY-MM-DD/
├── index.csv              # All expenses with metadata
├── index.json             # Same data in JSON
├── documents/             # All source files, organized by year
│   ├── 2024/
│   │   ├── receipt_001.pdf
│   │   └── eob_001.pdf
│   └── 2025/
└── README.txt             # Self-describing format documentation
```

This ensures the data remains useful even without the application.

### Audit Trail

The `hsa_audit_log` table captures every modification to `hsa_expenses`.
Application-level logging (not DB triggers) ensures all changes through
CLI, web UI, or Agent are recorded with:
- Who made the change (agent/user/system)
- What changed (field, old value, new value)
- When it changed
- Why (optional reason field)

---

## Agent SOP: HSA Audit

After ingestion, the Agent should:

1. **Review stubs**: `housebook-hsa list --status UNREIMBURSED --needs-review`
2. **Cross-reference**: For each stub, check if a provider receipt exists
3. **Resolve providers**: Map unknown providers to canonical names
4. **Categorize**: Confirm or correct the medical category
5. **Assign patient**: Determine which family member received care
6. **Verify amounts**: Confirm patient_responsibility matches available docs
7. **Flag gaps**: Note expenses missing receipts — prompt user to collect them
8. **Mark verified**: `housebook-hsa verify <ids> --category medical`

### Medical Category Reference (IRS Publication 502)

Qualified medical expenses include:
- Doctor visits, hospital stays, surgery
- Prescription medications (not OTC unless prescribed)
- Dental: cleanings, fillings, crowns, orthodontia
- Vision: exams, glasses, contacts, LASIK
- Mental health: therapy, psychiatry
- Lab work, diagnostic tests, imaging
- Medical equipment (CPAP, wheelchair, etc.)
- Ambulance, medically necessary travel

NOT qualified:
- Cosmetic surgery (unless medically necessary)
- Health club/gym memberships
- Most OTC medications (aspirin, vitamins)
- Teeth whitening
- General health improvement programs

### IRS Documentation Requirements

Minimum proof per expense:
1. **What**: Description of the medical service
2. **When**: Date of service
3. **Who**: Provider name and patient name
4. **How much**: Amount you paid (patient responsibility)
5. **Proof you paid**: Receipt, canceled check, or CC statement

A provider receipt/bill showing items 1-4 is the primary document.
Item 5 can be supplemented by CC transaction cross-reference.
An EOB strengthens the record but is not strictly required if the
receipt clearly shows patient responsibility after insurance.
