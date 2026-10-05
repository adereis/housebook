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
housebook-hsa merge <expense> <stub>   #    or merges manually
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

Before running the real scan, review the dry run for false positives
and non-eligible merchants, and propose `exclusion_patterns` for
them. Check `hsa_audit_log` for stubs the user already soft-deleted:
their reasons are precedent for what the user considers ineligible.
Charges under a workspace's `min_amount_by_category` are listed as
held back, not stubbed. If a held-back charge looks worth
documenting, raise it with the user rather than lowering the
minimum on your own.

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
- **Exact amount**: equal to the cent (`AMOUNT_TOLERANCE` in
  `hsa/matching.py`)
- **Date proximity**: stub date from 5 days before to 45 days after
  the expense service date (covers posting and billing lag). For a
  monthly refill at the same price, that window also reaches the
  next month's charge. Pair each receipt with the charge nearest the
  provider's lag, not with the first one listed.
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
housebook-hsa merge <expense_id> <stub_id>
```

`merge` keeps its first argument and deletes the second, so the
service record goes first. Reversed, it would keep the bare card stub
and delete the documented receipt or EOB. Merge transfers payment
metadata (`transaction_id`, `payment_method`, `payment_date`) from
the CC stub into empty fields of the service record. The stub is
marked deleted; the expense inherits its payment proof. Patient and
provider are never copied, so the stub's placeholder patient cannot
overwrite a real one.

The candidate matcher ranks pairs by each provider's
`expected_billing_lag_days`. When one EOB matches a run of
same-amount charges (a recurring copay, for example), pick the
charge at the provider's observed lag, not the matcher's top score.
If the observed lag differs from the config, fix the config.

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

### 4b'. Card charge larger than the receipt (unclaimed remainder)

Signs: one receipt and one CC charge from the same provider on
matching dates, with the charge a little higher. A pharmacy checkout
that also paid for supplies or a store item is the common case. The
candidate matcher requires equal amounts, so it never proposes these.
Merge them by hand, and confirm the cause with the user before
declaring the remainder.

```bash
housebook-hsa merge <receipt_id> <stub_id>
housebook-hsa verify <receipt_id> --evidence-level ready \
    --unclaimed 3.45 --unclaimed-reason "Supplies; no itemized receipt"
```

The receipt's amount is what gets claimed. The remainder balances the
math proof and never counts toward the reimbursable total. Prefer an
itemized receipt when the remainder is worth claiming: it becomes its
own expense on the same charge, and nothing is left unclaimed.

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
| Receipt + larger CC charge, remainder declared unclaimed | ready | `[✓]` |
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

CC stubs arrive with no patient. Merging into a receipt or EOB keeps
the document's patient. A stub promoted to `ready` on its own (a plan
installment, for example) needs `--patient`, and only when evidence
names one: cardholder data, or a plan or account document. Otherwise
leave it blank and list it for the user; `check` warns about any
reimbursable expense without a patient.

Two `verify` side effects to keep in mind. First, `--notes` replaces
the notes field rather than appending to it. To add a line to a
merged record, pass its existing notes plus the new line, or the
merge trail is lost; the audit log keeps the old value if that
happens. Second, every `verify` call clears `needs_review`. Use it
only on rows you have actually reviewed.

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
- **Lagged (7-10 days)**: mental health platforms
- **Monthly (25-35 days)**: recurring supplies, slow-billing providers

When you identify a consistent lag for a new provider, update
`config/hsa/providers.json` — don't hardcode it in the SOP.
