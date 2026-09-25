# HSA Document Import SOP

## Purpose

This SOP guides an AI agent through importing raw medical documents
from any location. The output is a set of standardized files paired
with JSON sidecars in `$WORKSPACE/hsa/YYYY/`. This is **Phase 1**
— you do NOT run `housebook-hsa ingest` from this SOP.

## Workflow Overview

```
Agent receives file path(s) from user
        |
        v
[Phase 1: import]     Agent follows THIS SOP
                      -> renames file to canonical convention
                      -> writes a JSON sidecar with the new envelope
                      -> moves source file to $WORKSPACE/hsa/YYYY/
                      -> escalates to user on any ambiguity
                      -> STOPS here — does not run ingest
        |
        v
[User review gate]    Agent presents summary; user approves
                      Resolves any AMBIGUOUS items
        |
        v
[Phase 2: ingest]     housebook-hsa ingest --dry-run
                      housebook-hsa ingest
```

## Step 1: Scan Source Files

List all files at the path provided by the user. Group them by
apparent source:

- **Insurance EOB bulk export**: A folder containing `contents.pdf`,
  `contents (1).pdf`, ..., plus a `Claim_Summary_*.csv`. These are
  one-EOB-per-PDF, reverse-indexed to the CSV rows.
- **Provider receipts/bills**: Individual PDFs from hospitals,
  dental offices, pharmacies, labs, etc.
- **HSA custodian statements**: HSA account statements or tax forms.
- **Insurance EOBs**: Individual EOB PDFs (not bulk).
- **Junk** (delivery photos, unrelated screenshots, marketing PDFs):
  skip these files entirely and report them to the user.
- **Unknown**: Anything that doesn't fit the above — flag as
  `AMBIGUOUS` and escalate (Step 6).

## Step 2: Classify Each Document

For each file, determine:

1. **Document type** (use `pdftotext -layout <file> -` to read content):

| Code | When to use |
|------|-------------|
| `REC` | Provider bill showing services rendered and amount owed/paid |
| `EOB` | Insurance "Explanation of Benefits" — shows billed, discount, plan paid, patient responsibility |
| `INV` | Invoice/bill that hasn't been paid yet (balance due) |
| `PLAN` | Payment plan agreement — shows total liability and installment schedule |
| `STMT` | HSA account statement showing contributions, balance, transactions |
| `TAX` | HSA tax forms: 5498-SA (contributions), 1099-SA (distributions) |
| `HIST` | Historical transaction ledger from the HSA custodian |

2. **Entity** — The issuing organization. Read the canonical names
   and aliases from **`$WORKSPACE/config/hsa/providers.json`**.
   Use the `canonical_name` (hyphenated) in the filename.

   If a provider is not in the config, create a new canonical name
   using Title-Case-Hyphenated format and **add it to
   `config/hsa/providers.json`** with its aliases so the ingestor
   can resolve it.

   **STRICT ALIAS VERIFICATION**: You MUST verify that the exact string you use for the clinical tag or entity exists in `config/hsa/providers.json` either as a `canonical_name` or an `alias`. If it does not, you must explicitly add it to the config file before filing.

3. **Patient** — Who received the service. Read the list from
   **`$WORKSPACE/config/hsa/patients.json`**. Use the `name` field
   (first name).

   If unclear from the document, **escalate** rather than guess
   `Unknown`. Multi-patient summary docs MUST be escalated unless
   the per-line patient is unambiguous.

4. **Date** — **Always use the Date of Service**, not the statement,
   billing, or payment date. ISO-8601.

   For **INV** documents, the service date is in the line-item detail
   (e.g., "Date of Service: 2/20/2026"), not the statement date
   printed at the top. For **REC** documents, use the appointment or
   service date, not the payment date.

   **Why this matters**: The dedup window is 45 days. Slow-billing
   providers (e.g., large hospital networks) generate statements 2–3
   months after service. Using the statement date puts the INV
   outside the dedup window and creates a duplicate expense instead
   of linking to the existing EOB.

   Fall back to statement date only for account-level documents
   (STMT, HIST, TAX) that have no specific service date.

5. **Amount** — Patient responsibility in dollars. `0.00` if not
   applicable or unclear.

6. **Tags** — Optional, separated by `__` in filename:
   - **Clinical tag** (EOB/STMT): The actual healthcare provider
     when entity is the insurer. E.g., an insurer's EOB with tag
     `City-Hospital` means the insurer issued the document, but
     City Hospital provided the service.
   - **Status tag**: `DECLINED` for rejected claims, `VOID` for
     cancelled. These will be skipped during ingestion.
   - **Category tag**: `Annual`, `Monthly`, `5498-SA`, `1099-SA`.

## Step 3: Rename and File

### Filename format
```
YYYY-MM-DD__Entity__DocType__Patient__Amount__Tags.pdf
```

Examples:
```
2025-03-11__Insurer__EOB__John__95.00__City-Hospital.pdf
2024-02-06__Family-Dental__REC__John__60.71.pdf
2024-12-31__HSA-Custodian__STMT__John__0.00__Annual.pdf
2024-12-31__HSA-Custodian__TAX__John__0.00__5498-SA.pdf
```

### Destination directory
Move to `$WORKSPACE/hsa/YYYY/` where YYYY matches the date.
Create the year directory if it doesn't exist.

## Step 4: Generate JSON Sidecar (envelope schema_version 1)

For each renamed file, create a `.json` sidecar with the same
basename. **Every sidecar uses the unified envelope** — a shared
header plus a source-specific `data` block.

### Envelope (always present)

```json
{
  "schema_version": "1",
  "source": "hsa",
  "source_file": {
    "path": "hsa/2024/2024-02-06__Family-Dental__REC__John__60.71.pdf",
    "sha256": "<sha256 hex of the source file>",
    "size_bytes": 123456,
    "mime_type": "application/pdf"
  },
  "classified_at": "<UTC ISO-8601, e.g. 2026-05-02T10:30:00Z>",
  "classified_by": "agent",
  "classifier_notes": null,
  "data": { ... see below ... }
}
```

Compute the SHA-256 with `sha256sum <file>` (or equivalent). The
`source_file.path` is **workspace-relative**, with forward slashes.

### `data` block — REC / INV documents

```json
{
  "date": "2024-02-06",
  "entity": "Family-Dental",
  "doc_type": "REC",
  "patient": "John",
  "amount": 60.71,
  "tags": []
}
```

### `data` block — EOB documents

Extract financials from the PDF text. Look for these patterns:
- "Amount Billed" → `financials.billed`
- "Cost Reduction" or "Discount" → `financials.discount`
- "What your plan paid" → `financials.plan_paid`
- "What I owe" → `financials.patient_responsibility`
- "Claim #" → `claim_id`

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

### `data` block — PLAN documents

`PLAN` is an account-level document (no `hsa_expenses` row is created).
Extract the installment schedule to help the Agent link installments later.

```json
{
  "date": "2025-07-01",
  "entity": "City-Hospital",
  "doc_type": "PLAN",
  "patient": "John",
  "amount": 900.00,
  "tags": [],
  "plan_details": {
    "total_liability": 900.00,
    "installment_amount": 75.00,
    "installment_count": 12,
    "first_payment_date": "2025-07-15",
    "frequency": "monthly"
  }
}
```

### `data` block — STMT / HIST documents

Include `line_items` array. For HIST, extract transaction history
and attempt reconciliation by matching date + amount to existing
files in `$WORKSPACE/hsa/`:

```json
{
  "date": "2026-12-31",
  "entity": "HSA-Custodian",
  "doc_type": "HIST",
  "patient": "John",
  "amount": 0.0,
  "tags": [],
  "line_items": [
    {
      "date": "2026-02-17",
      "status": "Approved",
      "type": "Credit Card (* 1234)",
      "amount": 40.00,
      "reconciled_to": "2026-02-17__Provider__REC__John__40.00.pdf"
    }
  ]
}
```

### `data` block — multi-item documents (Summaries / Ledgers)

If a single document covers multiple independent expenses (a Maple
Dental yearly "TAX" summary or a multi-patient invoice), use the
`items` array. Each item can override the top-level `date`, `patient`,
and `entity`.

```json
{
  "date": "2024-12-31",
  "entity": "Maple-Dental",
  "doc_type": "TAX",
  "items": [
    { "date": "2024-02-06", "patient": "Sterling", "amount": 150.00 },
    { "date": "2024-04-03", "patient": "Penny", "amount": 120.00 },
    { "date": "2024-06-06", "patient": "Buck", "amount": 95.00 }
  ]
}
```

## Step 5: Handle Insurance Bulk EOB Exports

Some insurers provide EOBs as a bulk download with a specific
structure:

1. A folder containing `Claim_Summary_*.csv` and multiple
   `contents.pdf`, `contents (1).pdf`, ... files.
2. The CSV has columns: `Service Date`, `Provider`,
   `Patient Responsibility`, etc.
3. **Critical**: The PDFs are reverse-indexed to CSV rows.
   `contents.pdf` → last CSV row, `contents (1).pdf` →
   second-to-last row, and so on.

Process:
1. Parse the CSV to get claim metadata
2. Match each PDF to its CSV row using reverse indexing
3. Rename and generate sidecars per the standard format
4. Delete the CSV and empty source folder after processing

## Step 6: Escalation rules — when to ASK rather than guess

The agent MUST stop and ask the user — do not silently file —
when any of these conditions hold:

| Condition | Why it must be escalated |
|---|---|
| `Patient` cannot be unambiguously determined from the document | The wrong patient invalidates HSA eligibility |
| Document covers multiple patients but per-line patient is unclear | Splitting the wrong way creates duplicate or missing expenses |
| Provider name doesn't match any alias in `config/hsa/providers.json` AND the document doesn't establish enough context to add a new entry | Bad provider mapping silently shards an expense across two canonical names |
| Patient-responsibility amount on the doc differs from the EOB by ≥ $5 | A real discrepancy that must be reconciled, not papered over |
| `pdftotext` returns no text and OCR (`tesseract`) confidence is low | Filing an unreadable doc loses information silently |
| Document type cannot be determined (REC vs EOB vs INV) | Each type drives different ingestion logic |
| File is potentially HSA-related but the structure doesn't match any known category | Better to flag AMBIGUOUS than misfile |

For each escalation: skip the file, present the question to the
user in conversation, and list what was skipped in the summary.

Never use `Unknown` as a fallback patient or provider — it
suppresses real ambiguity instead of surfacing it.

## Step 7: Junk handling

Files that are clearly NOT HSA documents (delivery photos, random
screenshots, marketing PDFs) should be skipped and reported to the
user. Do not move or delete them from the source location.

## Step 8: STOP. Do not ingest.

Present a summary in conversation listing what was imported (filename,
type, provider, patient, amount) and any files that were skipped or
ambiguous. Wait for user approval before proceeding to Phase 2.

If anything was ambiguous:

> The following need your input before I can proceed: ...

## After user approval — Phase 2: Ingest

```bash
housebook-hsa ingest --dry-run     # preview
housebook-hsa ingest               # commit to DB
housebook-hsa summary              # verify totals
```

The ingest step is deterministic — it reads sidecars, validates the
envelope, dedupes by (provider, patient, amount, date), and records
provenance (`source_file_path`, `source_file_sha256`,
`sidecar_path`) on every row.
