# Amazon Module

Amazon exports are already-structured CSV data. Unlike CC/Tax,
the import step is deterministic (unzip → identify profile →
write manifest sidecar) with no AI extraction needed.

| Aspect | Value |
|--------|-------|
| DB table | `transactions` (shared with CC) |
| Workspace dir | `amazon/{sterling,penny,...}/` (profile dirs) |
| CLI | `housebook-amazon` |
| Reconcile-cleanup SOP | `prompts/amazon/reconcile_cleanup.md` |
| Web route | `/spending` (filtered by source=Amazon) |

## Agent workflow for Amazon

```
# Import a new zip export
housebook-amazon import <zip> --profile <name>
housebook-amazon ingest --dry-run
housebook-amazon ingest
housebook-amazon summary
```

`housebook-amazon import` unzips the export, filters out non-data
files (delivery photos, etc.), and writes a `_manifest.json`
sidecar in the profile directory. No AI involvement needed.

## What the ingestor reads

`housebook-amazon ingest` walks each profile directory and reads
**four CSVs** (file-level idempotency via `processed_files`):

| CSV | Shape | Net per DB row |
|-----|-------|----------------|
| `Your Amazon Orders/Order History.csv` | Multi-*line* per Order ID (one row per shipment line) | One row per shipment line (no aggregation) |
| `Your Amazon Orders/Digital Content Orders.csv` | Multi-*component* per Order ID (Price + Tax rows, plus Promotion/Coupon rows for discounts) | One row per Order ID, net = sum of `Transaction Amount` across the order's rows |
| `Your Amazon Orders/Digital Returns.csv` | Multi-*component* per Order ID, mirror of Digital Content Orders | One row per Order ID, amount = -(sum of `Transaction Amount`) (refund credit ⇒ negative DB amount) |
| `Your Returns & Refunds/Refund Details.csv` | One row per refund event | One row per refund |

Each CSV is an independent database transaction. Its transaction rows,
recoverable row-level errors, and `processed_files` marker commit
together; an unexpected failure rolls the whole CSV back without
affecting the other three export files. Success output is emitted only
after commit.

Digital orders use a different DB description prefix
(`Amazon Digital: <product>`) to distinguish them from physical
orders; digital refunds use `Amazon Digital Refund: <product>`.
Status is `UNVERIFIED` and `needs_review = 1` like every other
ingested row.

**Order ID is persisted on every ingested row** in
`transactions.metadata` as JSON:

```json
{"amazon_order_id": "<order-id>",
 "csv": "orders|digital|digital_refunds|refunds"}
```

The `csv` discriminator is needed because a refund row's Order ID
inherits the *original order's* prefix (`111-…` for physical,
`D01-…` for digital) — the prefix alone cannot tell you "this row
came from Refund Details" vs. "this row came from Order History."
Query with `json_extract(metadata, '$.amazon_order_id')`.

**Order ID prefix is opaque** — physical orders have used many
prefix formats over the years (`002-`, `058-`, `103-`, `104-`,
`107-`, `109-`, `111-` through `116-` are all real in this DB);
digital is always `D01-`. Treat anything not starting with `D01-`
as physical; do **not** regex on `^11[1-6]-`.

**Digital Content schema gotcha — discounts are in `Offer Type
Code`, not `Component Type`.** Despite earlier docs/notes implying
otherwise, the real CSV's `Component Type` column carries only two
values: `Price Amount` and `Tax`. Discounts (and free-with-promo
items) live in a *separate* column called `Offer Type Code`
(`Not Applicable` / `Promotion` / `Coupon`) with the corresponding
positive or negative amount on `Transaction Amount`. The ingestor
doesn't care which column encodes the discount — it just sums
`Transaction Amount` per `Order ID`. This means free downloads
(coupon == price) net to $0 and are correctly skipped.

**Skipped at ingest:** orders whose any-row `Price Currency Code`
is non-USD (e.g., BRL purchases on a secondary profile); orders
whose net amount is $0 (free downloads, gift redemptions); orders
with unparseable `Transaction Amount` values (logged via
`ingestion_errors`). Inconsistent currency within one order
(rare/malformed) also skips the order.

**Intentionally not ingested as new transactions:**

- `Your Returns & Refunds/Replacement Orders.csv` — a 2-column
  mapping (`Order ID → Replacement Order ID`). The replacement
  Order ID itself already appears in `Order History.csv` at
  `Item Subtotal=0, Total Cost=0` and is correctly skipped by
  the zero-amount filter in `_ingest_orders`. The mapping file
  carries no amounts or dates, so there is nothing to count;
  it is metadata-only.
- `Your Subscriptions & Payment Plans/Monthly Payment Balance.csv`
  and `Monthly Payment Plans.csv` (BNPL installments). The gross
  sale price is already counted by the one Order History row, so
  ingesting installment charges as additional transactions would
  double-count. Instead, the existing Order History row is tagged
  with `metadata.is_bnpl=true` (plus `installment_count` and
  `downpayment`), and the reconciler's **pass 0 (BNPL)** pairs the
  gross CSV row against N bank-side installment charges. Partial
  coverage is supported — first N of M installments still match;
  the rest land on a later reconcile run.

`Buy With Prime Orders.csv` is also skipped — it carries only
logistics fields (tracking IDs, dates) with no amounts.

## Refund-row schema quirks

These two quirks govern how refund rows behave and must be respected
by any refund-matching logic:

**Refund Details rows carry order-level amounts mis-attributed to
one line.** A refund row labeled `"Amazon REFUND: USB Type C Cable"`
at −$180.00 may actually total ~$65 across two items at the CSV
order level — the ingestor picks one product name to attach to the
whole-order refund amount. So **never** rely on a refund row's
description or amount to match a single purchase line; match by
Order ID instead.

**Returns Status has more rows than Refund Details.** The two CSVs
disagree in count because: (a) some returns issue a replacement
instead of a financial refund; (b) digital refunds go to
`Digital Returns.csv`; (c) Refund Details has coverage gaps that
Returns Status fills. Before deleting any "excess" refund row that
has no current CSV match, diff Returns Status against Refund Details
first — some excess rows are real refund events that simply predate
current export coverage.

## Order-date vs ship-date decoupling

Amazon books the **order date** in `Order History.csv` /
`Digital Content Orders.csv` when the order is placed, but the
**bank charge posts only when the item actually ships**. Anywhere
Amazon decouples these — Subscribe & Save scheduled shipments,
backorder, pre-order, delayed-fulfillment third-party sellers,
drop-ship — the bank-vs-CSV date gap can run several weeks. This is
why a card charge and its CSV counterpart can legitimately sit
weeks apart, and why widening the reconcile date window recovers
real matches (see `prompts/amazon/reconcile_cleanup.md`).

## Amazon ↔ bank reconciliation (exceptional path)

**Design principle:** the Amazon CSV exports are the **source of
truth** for Amazon purchases. The CSV row is the one that gets
*counted* in spending — it stays visible and verified. Card
statements are secondary; for Amazon-merchant charges they are
duplicates of the CSV and must not double-count.

**Routine operation:** the Amazon-Chase card is used exclusively
for Amazon and Amazon-adjacent purchases (Prime, Audible, Whole
Foods — all already in the CSV exports). So its statements are
**not normally ingested**: doing so would only duplicate what the
CSV already covers. Run `housebook-amazon ingest`, reconcile returns
within the CSV, and you have the complete, correctly-netted story.

**When reconcile is needed (exceptional):** if a card statement
*has* been ingested and contains Amazon-merchant rows, those rows
are duplicates of the CSV and must be hidden. This happens for:
- Historical periods when the Amazon-Chase card was used for
  general expenses (pre-2025) and statements were ingested for the
  non-Amazon transactions on them.
- Any future case where Amazon-merchant charges appear on a
  different card and that card's statements were ingested.

Both are **exceptional** and warrant a conversation with the user
before/after running reconcile. The full operational SOP — when to
widen the date window, safety checks, invariant queries, and known
residual issues — lives in `prompts/amazon/reconcile_cleanup.md`.

```
housebook-reconcile --dry-run          # Preview matches; writes nothing
housebook-reconcile                    # Hide bank-side Amazon dupes
housebook-reconcile --date-window 5    # Widen the date tolerance
housebook-reconcile --db <path>        # Override DB
```

**Behavior:** a match marks only the **bank-side** row as
`RECONCILED` (and recategorizes it to `Transfers & Refunds`) — the
spending-view filter (`status = 'RECONCILED'`) hides it. The
**Amazon CSV row is not touched**: it remains the visible,
counted source of truth. The matcher tracks consumed Amazon rows
so one CSV row cannot be claimed by multiple bank charges.

**Matching runs in three passes.** **Pass 0 (BNPL)** finds Amazon
CSV rows tagged `metadata.is_bnpl=true` (BNPL plans), then matches
bank rows whose amount equals the plan's downpayment, regular
installment, or **final installment** (which absorbs the rounding
remainder — $100 over 3 gives 33.33/33.33/**33.34**) and whose date
falls within the plan's expected schedule (order date − 7 days
through order date + 32 × (count+1)).
Partial coverage allowed. The pass runs first because the tag is
opt-in and has higher specificity than amount-based matching.

> **A plan holds at most 1 downpayment + (count−1) installments.**
> Finding more matching rows than that means an unrelated Amazon
> charge shares the installment amount inside the window, and nothing
> distinguishes it — so the whole plan is **refused** and its rows
> surface as orphans for you to resolve. Silently hiding a real
> expense from spending views is the worse error; an unmatched
> installment is merely visible work. (Before this cap, a plan whose
> downpayment predated ingested coverage could pull in one extra
> same-amount charge and hide it as a duplicate.)
**Pass 1 (aggregate-by-Order-ID)** groups Amazon CSV purchase rows
by `metadata.amazon_order_id` (refunds excluded from the sum since
they carry the original order's ID with opposite sign) and matches
a bank charge against the per-Order-ID total — this closes
multi-line physical orders that the fuzzy per-row matcher cannot.
**Pass 2 (fuzzy per-row)** is the historical matcher (amount within
1¢, date within `--date-window`, default 3 days) and handles
single-row orders plus rows where the CSV side has no Order ID
metadata. Each pass runs to completion before the next starts —
pass-0 matches consume the CSV row so pass-1 can't claim it again.

**Bank-row exclusion filter:** `amazon_exclusion_patterns` in the
reconciler config drops bank rows whose
description matches an Amazon keyword but is *not* an Amazon.com
purchase — the canonical case being an Amazon office cafeteria
(`AMAZON XYZ01 CAFE ANYTOWN`, matched by the pattern `XYZ01`).
Excluded rows are not orphan-reported. The list is empty by default,
because the cafeteria codes differ by building; add the codes that
appear on your own cards to the workspace reconciler config.

**Always run `--dry-run` first** — the fuzzy pass is still
amount+date and can mis-pair two unrelated same-amount charges
within the window on a high-volume card. The CLI summary prints
the split (`N match(es) (X bnpl, Y aggregate, Z fuzzy)`); aggregate
matches show `(N lines, <order-id>)` and BNPL matches show
`(bnpl, <order-id>)` so you can scan for surprises.

**Orphan bank-Amazon rows** — bank rows whose description matches
Amazon merchant patterns but that did *not* match any CSV row —
are the failure mode to surface to the user. Either (a) the
matching CSV row is missing (investigate the CSV export) or (b)
the bank row predates the CSV coverage window (acceptable). The
agent must report orphans rather than silently leave them visible
as duplicates.

> Distinct from `prompts/reconcile_returns.md`, which pairs a
> purchase with its *refund* (`housebook-audit link`). That is a
> different operation from Amazon-CSV duplicate reconciliation.
