# HSA Reconciliation & Evidence Review SOP

## Purpose

After ingestion, every `hsa_expenses` entry starts as `stub`.
This SOP guides the AI agent through reconciling expenses against
corroborating documents and CC transactions to build audit-proof
entries that meet IRS Publication 969 requirements.

## IRS Five-Point Criteria

Every HSA-reimbursable expense must prove:

| # | Requirement | Best Source | Backup Source |
|---|-------------|-------------|---------------|
| 1 | Provider name | Receipt, EOB | CC statement |
| 2 | Date of service | Receipt, EOB | — |
| 3 | Description of service | Receipt, EOB | — |
| 4 | Proof of eligibility | EOB | — |
| 5 | Proof of payment | CC statement | Receipt marked "paid" |

**Two-factor verification** (EOB + CC statement) is the strongest
defense — often better than a standalone receipt.

## Workflow Overview

```
housebook-hsa summary --json           # 1. Orient
housebook-hsa list --needs-review --json
housebook-hsa scan --dry-run --json    # 2. Create CC stubs
housebook-hsa scan
housebook-hsa candidates --json        # 3. Auto-match
                                      # 4. Agent reviews matches
housebook-hsa merge <src> <tgt>        #    or merges manually
housebook-hsa verify <ids> --evidence-level ready  # 5. Set levels
housebook-hsa check                    # 6. Quality check
housebook-hsa summary --json           # 7. Report
```

Steps 4 and 5 are where agent judgment matters — the rest is
mechanical. Do not run `housebook-sync` during this workflow.

## Step 1: Orient

```bash
housebook-hsa summary --json
housebook-hsa list --needs-review --json
```

Get a sense of scale: how many expenses need review, what's the
total unreimbursed amount, how many are already `ready`/`strong`.

## Step 2: Create CC Stubs

```bash
housebook-hsa scan --dry-run --json    # preview
housebook-hsa scan                     # create stubs
```

The scanner queries the `transactions` table for medical-category
expenses and keyword matches, then creates `cc_stub` entries in
`hsa_expenses`. Each stub links to its CC transaction via
`transaction_id`.

**CC provenance is now available**: each CC transaction has a
`source_file_path` pointing to the canonical statement PDF
(e.g., `cc/2025/2025-04__Amex__1111__...pdf`). This means
during reconciliation, you can look up exactly which statement
a stub came from and even view the source PDF via the `/hsa` UI
modal.

## Step 3: Auto-Match Candidates

```bash
housebook-hsa candidates --json
```

This matches pending expenses (receipts/EOBs) against CC stubs
using:
- **Exact amount**: within $0.50
- **Date proximity**: stub date within 14 days after expense
  service date (covers posting lag)
- **Provider alias resolution**: via `config/hsa/providers.json`
- **Billing lag heuristic**: prioritizes matches near the
  provider's `expected_billing_lag_days` (if configured)
- **Installment detection**: flags recurring CC stubs from the
  same provider at the same amount as `POTENTIAL_INSTALLMENT`

The output includes the CC statement source (`source_file_path`)
for each matched stub, so you can verify the match against the
original document.

## Step 4: Agent Reviews and Acts on Matches

For each candidate match, decide:

### 4a. Straightforward match (expense → CC stub, 1:1)

```bash
housebook-hsa merge <stub_id> <expense_id>
```

Merge transfers payment metadata from the CC stub to the service
record. The stub is marked deleted; the expense inherits its
`transaction_id` and payment proof.

### 4b. Consolidated payment (one CC charge → multiple services)

When one CC transaction covers multiple receipts/invoices (e.g.,
family dental visit) and a `cc_stub` exists for it:

```bash
housebook-hsa merge-many <stub_id> <expense_id_1> <expense_id_2> \
    --reason "Part of $240.00 consolidated payment"
```

The `merge-many` command transfers payment metadata from the CC stub
to all target service records and atomically deletes the CC stub so that
the math proof guard succeeds seamlessly.

**Math proof guard**: `housebook-hsa verify --evidence-level ready`
blocks if the sum of all expenses linked to a CC transaction
doesn't match the transaction amount. Fix amounts or link missing
expenses first.

### 4c. Payment plan (hospital installment billing)

Signs: multiple CC stubs from the same provider, same amount,
~30-day spacing. An unmatched EOB/INV with a much larger total.

```bash
housebook-hsa plan --master <eob_id> \
    --installments <stub_ids> --name "Provider Plan 2025"
housebook-hsa verify <stub_ids> --evidence-level ready \
    --notes "Installment — master EOB #<id> is service proof"
```

Master stays `stub` forever (liability summary, not cash out of
pocket). Installments are reimbursable.

### 4d. No CC match — standalone receipt marked "paid"

If a receipt explicitly says "PAID" or shows $0 balance, it's
self-contained payment proof. Set directly to `ready`:

```bash
housebook-hsa verify <id> --evidence-level ready \
    --notes "Receipt marked PAID; no CC match needed"
```

### 4e. Ambiguous — escalate

Leave `needs_review = 1` and note the ambiguity. Common cases:
- Amount matches but provider name doesn't resolve
- Multiple stubs match the same expense
- The CC charge is for a non-HSA service at a medical provider

## Step 5: Evidence Levels

After matching, set evidence levels. The decision tree:

| Source Combination | Level | Badge |
|---|---|---|
| EOB + Receipt + CC Statement | **strong** | `[✓+]` |
| EOB + CC Statement | ready | `[✓]` |
| Receipt (paid) + CC Statement | ready | `[✓]` |
| EOB + Receipt (paid) | ready | `[✓]` |
| CC stub only | stub | `[ ]` |
| Receipt only | stub | `[ ]` |
| EOB only | stub | `[ ]` |
| Consolidated payment (math proof passes) | ready | `[✓]` |
| Consolidated payment (math proof fails) | weak | `[?]` |

**Compliance cut-line**: only `ready` and `strong` count toward
the reimbursable total. Never reimburse `stub` or `weak`.

```bash
housebook-hsa verify <ids> --evidence-level ready
housebook-hsa verify <ids> --evidence-level strong
```

## Step 6: Quality Check and Report

```bash
housebook-hsa check --json
housebook-hsa summary --json
housebook-hsa list --needs-review --json
```

Report to the user:
- Total reimbursable amount (`ready` + `strong`)
- Expenses still needing documentation
- Action items: "Collect receipt from Provider X to reach `ready`"

## Narrowing CC Transaction Searches

When manually querying the `transactions` table for payment proof,
filter by category — do not scan all transactions:

```sql
SELECT id, date, description, amount, source,
       source_file_path
FROM transactions
WHERE category IN ('Health', 'Dental', 'Vision')
  AND source != 'Amazon'
  AND amount > 0
ORDER BY date DESC;
```

The `source_file_path` column now shows which CC statement PDF
each transaction came from (e.g., `cc/2025/2025-03__Amex__...`),
making it trivial to trace payment proof back to the original
document.

## Edge Cases

### $0 Amounts
Not eligible for HSA reimbursement. Verify for completeness but
they never reach `ready`.

### Multi-year reconciliation
HIST documents from the HSA custodian contain `reconciled_to`
pointers. Use these to verify that expenses paid directly from
the HSA card are NOT included in the shoebox.

### Expenses before HSA establishment
Check `config/hsa/patients.json` for `hsa_eligible_since` dates.
Expenses before this date are not HSA-eligible.

### Billing lag heuristics
Financial providers bill in cycles. Consult `config/hsa/providers.json`
for `expected_billing_lag_days` per provider:
- **Immediate (1-3 days)**: pharmacy, hospital Epic payments
- **Lagged (7-10 days)**: mental health platforms (Headway)
- **Monthly (25-35 days)**: recurring supplies, slow-billing providers

When you identify a consistent lag for a new provider, update
`config/hsa/providers.json` — don't hardcode it in the SOP.
