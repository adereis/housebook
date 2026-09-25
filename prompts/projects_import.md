# SOP: Retrospective Project Import (from a hand-tracked sheet)

## When this runs
The user hands you a spreadsheet of expenses they tracked **by hand** for
one or more past projects ("here are our renovations") and asks you to
create the projects and find the matching transactions already in the
database. This is distinct from `prompts/projects.md`, which covers the
*prospective* case (declare an open project, let `match-project` sweep new
data going forward). Here you are reconstructing finished projects from
existing history.

## The governing reality: the sheet is GUIDANCE, not source of truth
A hand-tracked sheet is a memory aid. Treat every figure as approximate:

- **Amounts rarely match the DB exactly.** The sheet often records
  tax-inclusive totals, aggregated multi-item orders, or round estimates;
  the DB stores **per-line, pre-tax** amounts (especially Amazon). So
  **match on product description, not amount.** Exact-amount matching is a
  fast first pass that will *miss most items* — fall back to keyword/
  description search in the project's date window.
- **Lines may be lumps or estimates** ("Misc materials $4,552", "Misc
  Amazon ~$250"). Do **not** invent manual rows to reconcile a lump to a
  total — confirm with the user first; they may be aggregates of items you
  also matched individually (double-count risk).
- The sheet total is a **budget/target**, not a sum to force-balance. Real
  project totals legitimately fall **short** of it (see gaps below).

## Step 1 — Create the projects
One `create-project` per project. Use the sheet total as `--budget` (it is
the user's own estimate/target). Set a generous window; you can tighten it
later with `edit-project --start/--end` once you see where purchases
actually fall (they often **precede** the "season" in the project name —
e.g. a "Spring 2023" reno whose first buys are Dec 2022).

## Step 2 — Match by amount AND description
For each sheet line, search the DB:
1. **Amount pass** (`ABS(ABS(amount) - X) < 0.02`) across the window —
   fast, catches exact hits.
2. **Description pass** — keywords from the item name (product, brand,
   material) across the window. This is the *reliable* pass.
Round/small amounts ($10, $19.99, $44.99) produce huge false-positive
lists — rely on description for those, or skip.

## Step 3 — Assign the SOURCE-OF-TRUTH row, never the bank twin
**This is the easiest way to get a project wrong.** An Amazon purchase
appears in the DB **twice**: the verified `Amazon` CSV row (source of
truth, visible, counted) and its bank-card twin (`Chase-Amazon` / `Amex`
"AMAZON MARKETPLACE") which the reconciler marked **`RECONCILED`** (hidden).
- Assign the **`Amazon`** row (or, for a direct store purchase like SVS or
  IKEA, the single card row).
- **Never** assign the `RECONCILED` twin — it is excluded from spending
  views, so it would contribute **$0** to the project while looking
  assigned. Check `status` and `json_extract(metadata,'$.amazon_order_id')`
  when a line shows two hits at the same amount/date.
- A returned item is a purchase+refund that net to zero (often `linked`);
  assign the **kept** item's row, not the returned one.

Use **`assign <ids> --project N`** (pure tagging — leaves `status`
untouched), not `verify --force`, when the rows are already audited. `verify
--project` is for the combined *audit-and-assign* step on fresh rows.

## Step 4 — Off-ledger costs (labor, lump materials)
Cash/check craftsmanship and contractor invoices never hit a statement.
Record them as project-linked one-time manual expenses — **only with the
user's confirmation of the amount** (the sheet figure is an estimate):
```
housebook-audit add-manual "Workmanship — <who>" --amount N \
    --category "Home & Garden" --date YYYY-MM-DD --project <id>
```
See `prompts/projects.md` Step 5b. Do **not** hand-insert these into
`transactions` (the source ledger is immutable).

## Step 5 — Gaps are expected; never fabricate them
You will not find everything, and that is correct, not a failure:
- **Statement-coverage gaps.** In-store **Lowes / Home Depot / appliance /
  specialty-store** purchases are frequently *not ingested* (the statements
  were never imported, or are no longer available). Per the project's
  strict-determinism rule, leave these as honest gaps — **do not invent
  manual entries** to fill them. A project total below the sheet's is the
  truthful outcome.
- Diagnose the gap so the user can act: report what's missing, the likely
  store, and that recovering it requires ingesting that statement. A useful
  tell — if a whole class of items is missing, check whether *any* charge
  from that source exists in the window at all (e.g. "zero non-Amazon
  charges ≥$100 in this window" ⇒ that card's statements aren't ingested).

## Step 6 — Report, save a resume doc, close when done
- Show `project-summary <id>` per project; explain found vs. missing.
- For multi-session hunts, save a **resume doc in the workspace** (never the
  repo — it holds real amounts): per-project totals, the still-missing items
  with amounts + likely source, and the resume commands. (Example produced
  this way: `$WORKSPACE/renovation_projects_hunt.md`.)
- `close-project <id> --freeze-end` only when the user agrees a project is
  final; it pins `end_date` to the last assigned transaction. Leave
  still-hunting projects **open**.

## Quick command reference
```
housebook-audit create-project "Name" --type renovation --start … --budget …
housebook-audit edit-project <id> --start … --end … --budget …   # amend
housebook-audit assign <ids> --project <id>                       # tag (no status change)
housebook-audit add-manual "Labor" --amount N --category … --date … --project <id>
housebook-audit project-summary <id>
housebook-audit close-project <id> --freeze-end
```
