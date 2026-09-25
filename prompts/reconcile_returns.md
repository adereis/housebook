# SOP: Reconcile Returns, Refunds & Cancellations

## Objective
Identify purchase↔refund pairs (credit card cancellations, Amazon returns)
and link them so neither side inflates spending reports. Both transactions
stay in the database for auditability but are hidden from the UI.

## When to run
After every `monthly_audit.md` cycle, or whenever the Agent notices
negative-amount transactions during audit.

## Prerequisites
- Complete the monthly audit first — transactions should be categorized
  and verified before linking.
- Refunds/credits are already auto-categorized as `Transfers & Refunds`
  by the ingestion pipeline (any negative amount).

## Step 1: Identify refund candidates

Look for negative-amount transactions (they appear as `Transfers & Refunds`):

```
housebook-audit pending --json | jq '[.[] | select(.amount < 0)]'
```

Or query verified refunds that haven't been linked yet:

```sql
SELECT id, date, description, amount, source
FROM transactions
WHERE amount < 0
  AND linked_transaction_id IS NULL
ORDER BY date DESC
```

## Step 2: Match each refund to its original purchase

For each refund, find the corresponding purchase by matching:

1. **Merchant name** — same or similar description
2. **Amount** — refund magnitude equals purchase amount (full refund) or
   is less (partial refund)
3. **Date proximity** — purchase precedes refund, typically within 1-60 days
4. **Source** — usually the same credit card, but cross-card is possible

```sql
SELECT id, date, description, amount, source
FROM transactions
WHERE amount > 0
  AND linked_transaction_id IS NULL
  AND description LIKE '%<merchant>%'
ORDER BY date DESC
```

### Amazon returns
Amazon refunds have descriptions like `"Amazon REFUND: <product>"`.
Match against the original `"Amazon: <product>"` purchase by product name.

### Credit card credits
Hotel/airline cancellations often appear as the same merchant with a
negative amount on a later statement. Check metadata for confirmation
numbers or booking references when available:

```
housebook-audit pending --json | jq '.[] | select(.id == <id>) | .metadata'
```

## Step 3: Link the pair

For **full refunds** (amounts cancel exactly or within a small FX delta):

```
housebook-audit link <purchase_id> <refund_id>
```

This creates a bidirectional link — both transactions are excluded from
spending views while remaining in the database.

### FX adjustment tolerance

International transactions may have small differences between the
charge and refund due to currency conversion on different dates.
These are acceptable to link — the net delta is typically under $5
and can be audited anytime via `housebook-audit linked`, which shows
the net for each pair.

### What NOT to link

- **Cash-back rewards / statement credits / signup bonuses** — these are
  standalone credits, not purchase reversals. Leave them as
  `Transfers & Refunds` (already hidden). Do not link them to anything.
- **Payment transactions** — credit card payments from bank accounts are
  already handled by the Transfers & Refunds category.

## Step 4: Review linked pairs

```
housebook-audit linked
housebook-audit linked --json
```

Verify each pair makes sense. If a link was made in error:

```
housebook-audit unlink <transaction_id>
```

## Step 5: Flag unmatched negatives for review

After linking all clear matches, compile a list of negative-amount
transactions that remain unlinked:

```sql
SELECT id, date, description, amount, source
FROM transactions
WHERE amount < 0
  AND linked_transaction_id IS NULL
  AND category = 'Transfers & Refunds'
ORDER BY date DESC
```

Classify each into one of these buckets and present to the user:

| Type | Example | Action |
|------|---------|--------|
| **Partial refund** | -$70 refund for a $100 purchase | Flag for user — we don't yet have a policy for these |
| **Cash-back / credit** | Statement credit, signup bonus | No action needed (standalone credit) |
| **Payment** | Credit card payment from bank | No action needed |
| **Unmatched refund** | Refund with no visible purchase | Flag for user — the purchase may be outside the ingested date range |

**Do not silently skip partial refunds or unmatched negatives.** Always
report them so we can build policy from real-world examples.

## Step 6: Edge cases

| Scenario | Action |
|----------|--------|
| Full refund, same card | `housebook-audit link <purchase> <refund>` |
| Full refund, different card | Same — link works across sources |
| Partial refund | Flag for user review (no established policy yet) |
| Multiple refunds for one purchase | Flag for user review |
| Refund with no matching purchase | Flag for user review |
| Chargeback / dispute credit | Treat as full refund if resolved |

## Step 7: Report

After linking, re-run `housebook-audit summary` to confirm spending
totals look correct. The post-audit report to the user MUST include:

1. Linked pairs (purchase ↔ refund, with amounts)
2. Unmatched negatives that need user attention (partial refunds,
   orphaned credits, anything ambiguous)
3. Cash-back/payment credits that were left as-is (for awareness)

## Appendix: Amazon charges on a credit card (CSV duplicates)

Amazon orders are tracked from the Amazon CSV exports, which are the
**source of truth** for Amazon purchases. The CSV row is the one that
gets counted; it stays visible and verified.

**Normally, Amazon-Chase card statements are not ingested** — every
transaction on that card duplicates the CSV, so ingesting the statement
just creates work. The case this appendix covers is *exceptional*: a
card statement *was* ingested (e.g. historical pre-2025 Amazon-Chase
mixed-use, or Amazon-merchant charges appearing on a different card),
so it now contains Amazon-merchant rows that double-count with the
CSV. Those bank-side rows must be hidden.

**Operation:** mark only the **bank-side** Amazon-merchant rows as
`RECONCILED` (which hides them from spending views) and recategorize
them to `Transfers & Refunds`. **Do not touch the Amazon CSV rows** —
they stay visible as the counted truth. Do **not** link them as
purchase↔refund pairs; they are not reversals.

Identify Amazon-origin card rows deterministically — a row is Amazon if:
- its `metadata` contains an `amazon_order_id`, **or**
- its description matches Amazon merchant patterns (`Amazon`, `AMZN`,
  `Kindle`, `Amazon Prime`, `Amzn.com`).

```sql
UPDATE transactions
SET status='RECONCILED',
    category='Transfers & Refunds',
    needs_review=0
WHERE source = '<card>'
  AND status != 'RECONCILED'
  AND id IN ( <ids matched above> );
```

This is a **bulk** reconciliation keyed on the order-id / description
signal — not a 1:1 match against specific CSV rows. It's acceptable
because the CSV is the source of truth for Amazon; a few card charges
with no CSV counterpart (older than the export window) are tolerated as
a small, known error — but **flag the count of orphans for user
review** rather than silently leaving them visible.

**Before bulk-marking:**
- Confirm no medical/HSA or other non-Amazon rows are caught (they
  won't match the Amazon patterns — verify the count).
- Confirm none of the target rows are already verified non-RECONCILED
  with a specific category, so you don't clobber prior audit work
  (`SELECT COUNT(*) ... WHERE status NOT IN ('UNVERIFIED','RECONCILED')`).
- **Never set the Amazon-source row to `RECONCILED`** — that hides the
  truth side and under-counts spending.
