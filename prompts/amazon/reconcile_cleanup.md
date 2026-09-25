# SOP: Amazon Bank-Reconcile Cleanup

This is the **operational** SOP for the exceptional Amazon ↔ bank
reconciliation path. For the architecture (design principle, the
three matcher passes, refund-row schema quirks, order-vs-ship date
decoupling) read `src/housebook/amazon/AGENTS.md` first.

Reconcile is an **exceptional** operation — the Amazon-Chase card is
not normally ingested because every charge on it duplicates the CSV.
Only run this when a card statement *has* been ingested and contains
Amazon-merchant rows that must be hidden as duplicates. Talk to the
user before and after.

## Standard sequence

```
# Always preview first
housebook-reconcile --dry-run

# The summary prints the split: N match(es) (X bnpl, Y aggregate, Z fuzzy)
# Aggregate matches show (N lines, <order-id>); BNPL show (bnpl, <order-id>).
# Scan for surprises, then apply:
housebook-reconcile
```

The default 3-day window plus the BNPL (pass 0) and
aggregate-by-Order-ID (pass 1) passes grab everything cleanly
matchable. After that, what remains is the residual orphan pile.

## Widening the date window (one-off residual cleanup)

Amazon's order-date/ship-date decoupling (see the module README)
means real matches can sit weeks apart. After the standard passes
have run, a wider window recovers delayed-shipment matches:

```
housebook-reconcile --dry-run --date-window 45 | tail -30
# Inspect each match; if all look like delayed shipments, apply:
housebook-reconcile --date-window 45
```

**45 days is the natural elbow.** Empirically, gains plateau between
window=45 and window=60 (0 new matches), with only marginal matches
in the 60–90 day range. The recovered gaps cluster as:

| Gap (days) | Pattern |
|---:|---|
| 11–14 | Subscribe & Save (coffee pods, supplements, alcohol) |
| 22–30 | Backorder fashion + S&S household supplies |
| 30–44 | Pre-order books, slow-ship electronics, long-tail S&S |

**Do NOT change the default window** in `reconciler_config.json`.
Wide windows are safe *only because* the narrow-window passes have
already grabbed the cleanly-matchable rows. Running window=45 on a
fresh dataset would admit far more amount-collision risk over 45
days of high-volume Amazon use.

### Safety check before applying a wide-window pass

- Aggregate matches (Order ID locked) at any gap are safe by
  construction — look for these first.
- Fuzzy matches at large gaps are the risk. The day-gap and product
  name should plausibly describe a shipping delay (no "matched a
  random same-amount Amazon row 6 weeks later" suspicion).
- Same-amount common values ($9.99, $19.99, etc.) at wide gaps need
  extra scrutiny.

## Invariant & diagnostic queries

```sql
-- Invariant: no Amazon CSV row should EVER be RECONCILED
SELECT COUNT(*) FROM transactions
WHERE source='Amazon' AND status='RECONCILED';
-- Expected: 0

-- Status distribution of Amazon rows
SELECT status, COUNT(*) n, ROUND(SUM(amount),2) sum
FROM transactions WHERE source='Amazon' GROUP BY status;

-- Bank-side RECONCILED counts
SELECT source, COUNT(*) FROM transactions
WHERE status='RECONCILED' GROUP BY source;

-- Orphan bank-Amazon rows (visible, not RECONCILED)
SELECT source, COUNT(*) n, ROUND(SUM(amount),2) total
FROM transactions
WHERE source != 'Amazon'
  AND status != 'RECONCILED'
  AND category != 'Transfers & Refunds'
  AND (description LIKE '%AMZN%' OR description LIKE '%AMAZON%')
GROUP BY source;
-- Office-cafeteria rows (e.g. AMAZON XYZ01 CAFE) show up here but
-- are NOT real orphans — they are excluded by the reconciler's
-- amazon_exclusion_patterns config. A query mirroring the reconciler
-- exactly needs `description NOT LIKE '%<pattern>%'` for each one.
```

> **SQLite gotcha:** `GROUP BY oid` (where `oid` is a SELECT alias)
> silently resolves to the table's `rowid` pseudo-column, breaking
> aggregation. Use `GROUP BY 1` or the full expression. The
> production matcher aggregates in Python (a plain dict), so it is
> immune — but ad-hoc analytical queries must mind it.

## Known residual issues (not currently scoped)

These three share a root cause — CSV rows that live on a **different
Amazon account's export** (gift orders, shared household) or predate
current export coverage. Resolution likely needs an Amazon `--all`
re-export, not a code change. Surface them to the user; do not
fabricate matches.

1. **Residual bank-Amazon orphans.** After all passes plus a
   window=45 cleanup, a residual pile of Chase-Amazon orphans
   remains. They use the core `AMAZON MKTPL` / `Amazon.com` merchant
   prefix (no Whole Foods / Fresh / Audible / Music / Kindle
   signatures), so their CSV counterparts are most likely on another
   account's export.

2. **"Excess" refund rows with no current CSV match.** Identified
   precisely by `source='Amazon' AND metadata IS NULL` (the Order ID
   backfill tagged every row with a current CSV match, so a NULL
   metadata is exactly a refund row with no match). These are
   physical `Amazon REFUND:` rows spanning 2007–2025; several recent
   dates fall *within* current coverage but still don't match —
   investigate whether a smaller `Refund Details.csv` was re-exported
   after those refunds, or whether descriptions/amounts were edited.
   **Caveat before any deletion:** the oldest rows are real history
   the CSV no longer covers — verify against Returns Status first.

3. **Multi-line refund cases** (one refund cancelling N purchase
   rows for the same Order ID). The single FK `linked_transaction_id`
   can't safely express these; they need a schema extension
   (refund-group ID) or status-based hiding. Report for user review
   per the "only full refunds where amounts cancel exactly" rule in
   `prompts/reconcile_returns.md`.
