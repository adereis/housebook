# HSA Shoebox Module

The HSA Shoebox maintains an audit-proof ledger of qualified medical
expenses paid out-of-pocket, designed to support tax-free HSA
reimbursement years or decades later. It is **completely separate**
from both expenses and tax — different tables, different inputs,
different CLI.

| Aspect | Expenses | Tax | HSA Shoebox |
|--------|----------|-----|-------------|
| DB tables | `transactions` | `tax_documents` | `hsa_expenses`, `hsa_documents`, `hsa_providers`, `hsa_audit_log` |
| Workspace dir | `cc/YYYY/`, `amazon/<profile>/` | `tax/YYYY/` | `hsa/YYYY/` (envelope-wrapped sidecars + source files) |
| CLI | `housebook-audit` | `housebook-tax` | `housebook-hsa` |
| Web route | `/spending` | `/tax` | `/hsa` |

## HSA expense status lifecycle

| Status | Meaning |
|--------|---------|
| `UNREIMBURSED` | Paid out-of-pocket, available for future HSA withdrawal |
| `PENDING` | Selected for a reimbursement batch, not yet withdrawn |
| `REIMBURSED` | Withdrawn from HSA, no longer available |

## HSA source types

| Source | Set by | Meaning |
|--------|--------|---------|
| `receipt` | Ingestor (REC sidecar) | Provider receipt / payment confirmation |
| `eob` | Ingestor (EOB sidecar) | Insurance Explanation of Benefits |
| `invoice` | Ingestor (INV sidecar) | Provider bill (unpaid) |
| `cc_stub` | Scanner | Auto-detected from CC transaction; needs receipt |
| `manual` | User/Agent | Manually entered |

Account-level documents (STMT, TAX, HIST, PLAN) create `hsa_documents`
entries only — no `hsa_expenses` row.

## Evidence level (IRS audit-readiness)

| Level | Badge | Meaning | Reimbursable? |
|-------|-------|---------|---------------|
| `stub` | `[ ]` | Missing documentation (CC charge only, or single unmatched doc) | No |
| `weak` | `[?]` | Consolidated payment / needs math proof | No |
| `ready` | `[✓]` | IRS-ready: corroborated 1:1 match | **Yes** |
| `strong` | `[✓+]` | Ironclad: 3+ corroborating sources | **Yes** |

Only `ready` and `strong` expenses count toward "available for
withdrawal." The Agent sets this via
`housebook-hsa verify <ids> --evidence-level ready`.

**Math proof guard:** `verify --evidence-level ready` (or `strong`)
will refuse if the expense is linked to a consolidated CC charge
whose amount doesn't match the sum of all linked expenses. Fix the
amounts or link missing expenses first. The guard also covers a link
being created *in the same call* — `verify <id> --transaction-id X
--evidence-level ready` is checked against transaction X, not against
the row's previous (possibly absent) link.

A blocked row is left **entirely** untouched: no field updates and no
audit-log entries, even for flags passed alongside `--evidence-level`
in the same command. `verify` reports `Verified N of M` when some rows
were blocked.

**Demo data follows this table.** `housebook-demo-seed` writes a
shoebox through `src/housebook/demo_hsa.py`, and it derives each demo
expense's level from the sources on file using this table. Change the
rules here and `demo_hsa.evidence_level` must change with them;
`tests/test_demo_seed.py` checks the math proof and file hashes.

## Document sidecar convention

Documents are pre-processed by an AI agent into structured sidecars.
Filename: `YYYY-MM-DD__Entity__DocType__Patient__Amount__Tags.pdf`
Sidecar: same basename with `.json` extension.

Every sidecar wraps its source-specific data in the unified
**v1 envelope** (see `src/housebook/core/sidecar.py`):

```json
{
  "schema_version": "1",
  "source": "hsa",
  "source_file": {
    "path": "hsa/2025/...",
    "sha256": "...", "size_bytes": 0, "mime_type": "..."
  },
  "classified_at": "...", "classified_by": "agent",
  "data": { ... HSA-specific block ... }
}
```

The HSA-specific `data` block carries the historical fields:
date, entity, doc_type, patient, amount, financials, tags, items,
line_items.

The ingestor validates the envelope, requires `source: "hsa"`, and
records provenance (`source_file_path`, `source_file_sha256`,
`sidecar_path`) on every `hsa_documents` row. Source files without
sidecars — and sidecars whose envelope fails validation — are
flagged as errors. Run the import SOP (`prompts/hsa/import.md`)
first.

## Agent workflow for HSA

The HSA workflow has **two distinct phases separated by a human
review gate**: import (AI agent judgment) → STOP → user approval
→ ingest (deterministic code).

**Do not run `housebook-sync` as part of this workflow.** Sync is a
separate, infrequent, user-triggered operation.

```
# Phase 1: Import raw documents (AI agent follows SOP)
# User provides file path(s); agent follows prompts/hsa/import.md:
#   - Read each file via pdftotext / OCR
#   - Determine type/entity/patient/amount; ESCALATE on ambiguity
#   - Rename per convention; generate envelope-wrapped sidecar
#   - Move to hsa/YYYY/
#   - STOP. Do NOT run ingest. Wait for user approval.

# Review gate: user reviews summary; resolves ambiguities;
# says "go" before Phase 2 begins.

# Phase 2: Ingest imported documents
housebook-hsa ingest --dry-run
housebook-hsa ingest

# Phase 3: Cross-reference with CC statements
housebook-hsa scan --dry-run
housebook-hsa scan

# Phase 4: Reconcile and set evidence levels
# Agent follows prompts/hsa/reconcile.md to:
#   - Match EOBs to receipts (same service, different perspective)
#   - Match expenses to CC transactions (payment proof)
#   - Set evidence_level based on corroborating sources
housebook-hsa list --needs-review --json
housebook-hsa candidates --json              # Auto-match stubs; flags POTENTIAL_INSTALLMENT
housebook-hsa plan --master <ids> --installments <ids> --name "..."
                                            # Link master liability to installments
housebook-hsa plan --list                    # List all payment plans
housebook-hsa plan --show <plan_id>          # Show plan progress
housebook-hsa merge <target_id> <source_id>  # Merge source into target (any types)
housebook-hsa delete <ids> --reason "..."   # Soft-delete duplicate/erroneous expenses
housebook-hsa verify <ids> --category medical
housebook-hsa verify <ids> --provider "Provider Name"
housebook-hsa verify <ids> --evidence-level ready

# Phase 5: Attach documents to CC stubs
housebook-hsa link-doc <expense_id> <file_path>

# 5. Data quality check
housebook-hsa check
housebook-hsa check --verify-hashes   # also re-hash every source file

# 6. Summary (reimbursable total vs. total unreimbursed)
housebook-hsa summary --year 2025
housebook-hsa summary --json
```

All subcommands accept `--json` for agent consumption and `--db`
to override the database path.

## CC medical expense scanner

The scanner (`housebook-hsa scan`) queries the `transactions` table
for medical-category expenses and keyword matches, then creates stub
entries in `hsa_expenses` with `source='cc_stub'`. Amazon
transactions are excluded. Stubs are deduplicated by `transaction_id`.

## Config files

- **`config/hsa/providers.json`** — Provider alias mapping
  (canonical name + category + aliases array). The Agent learns new
  aliases during audit and updates this file.
- **`config/hsa/patients.json`** — Family member registry with
  DOB and HSA eligibility dates.
- **`config/hsa/scanner.json`** — Exclusion patterns for the CC stub
  scanner. Substring patterns in `exclusion_patterns` are matched
  case-insensitively against transaction descriptions; any match
  suppresses stub creation. Use this to block non-HSA-eligible items
  that might still slip into "Health" (e.g., some non-eligible OTC
  items or cosmetic procedures). `medical_keywords` adds to the
  scanner's built-in keywords, which cover only generic words
  ("hospital", "clinic") and national chains. List your regional
  hospital network there when its card descriptor carries no generic
  word. See `config/hsa/scanner.example.json` for the structure.

## Cross-document deduplication

When multiple documents (EOB, invoice, receipt) describe the same
medical service, the ingestor deduplicates at the service level.
A new sidecar is matched against existing expenses using:
- Same provider (after alias resolution)
- Same patient
- Same patient_responsibility amount (within $0.50)
- Service date within 45 days

If a match is found, the document is linked to the existing expense
(no new `hsa_expenses` row). Output shows `~` instead of `+` to
indicate linking vs. creation.

**Not handled at ingest time**: physician-name EOBs vs.
facility-name receipts (e.g., "Dr. Smith" EOB vs. "City Hospital"
receipt). These resolve to different canonical providers
and require manual matching during reconciliation
(`prompts/hsa/reconcile.md`).

## Document integrity

Every source file gets a SHA-256 hash stored in `hsa_documents`.
`housebook-hsa check` verifies that each file still **exists**;
`housebook-hsa check --verify-hashes` additionally **re-hashes** every
source file and compares it against the hash recorded at ingest,
reporting a `hash_mismatch` error for any document whose content
changed. Re-hashing is IO-bound, so it is opt-in rather than part of
the routine check — run it before assembling a reimbursement packet.

All field-level changes to `hsa_expenses` are logged in
`hsa_audit_log` with who/what/when/why for IRS defense. Guards run
**before** any write, so a blocked `verify` leaves no audit-log
entry — the log never records a change that did not happen.

## Ingest atomicity

Each sidecar runs inside one `Database.transaction()`: expense rows,
document rows, and `processed_files` commit together. The original
reproduction—two successful items followed by an invalid third
amount—now leaves all three tables unchanged; correcting the sidecar
and retrying writes exactly three expense/document pairs. Success
messages are buffered until commit so rolled-back items are never
reported as durable.

## Shared HSA engines

Candidate scoring and installment detection live in `hsa/matching.py`.
They are pure transformations over expense/stub rows; `cmd_candidates`
owns only the database query, provenance lookup, and human/JSON output.

Candidate and installment matching now share the ingestor's
`ProviderResolver`, including its longest substring-alias behavior.
One resolver instance loads aliases and per-provider billing-lag config
once per command, so matching cannot disagree with ingest or reread the
config inside the expense-by-stub loop.

Consolidated-payment proof is likewise canonical in
`hsa/math_proof.py`. Both `verify` (including a transaction link proposed
in the same call) and `check` use the same post-update projection and
tolerance. `check` also reports links to transactions that no longer
exist instead of dropping them through an inner join.

`merge` and `merge-many` now share `_merge_expenses` for field,
document, note, review-state, source-deletion, and audit-log updates.
The command-specific guards remain explicit: one-to-one merge may
combine service records, while one-to-many requires a CC stub feeding
live service targets. Dry-run uses the same projection without writes.

## Full design plan

See `prompts/hsa/shoebox.md` for the complete design plan
including: IRS requirements, sidecar convention, evidence levels,
and future work (reimbursement packet generator, annual reporting).
