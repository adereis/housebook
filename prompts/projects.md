# SOP: User-Initiated Projects

## Objective
Track a discrete, user-declared spending effort (a renovation, an event,
a vehicle purchase) by grouping its transactions under a **project**, so
total spend can be reported against an optional budget. Projects are the
**supervised-retrieval** counterpart to trip detection: the user declares
the project; you store its matching criteria; the matcher retrieves
candidate transactions for your review.

**Projects GROUP spending — they do not exclude it.** A renovation is
real spending and stays in every spending view. The project is a
reporting/budget lens, not a filter.

## When this runs
- **Prospective:** the user says "we're starting a project to renovate the
  master bath." Create an **open** project (no `--end`); it is re-scanned
  every audit cycle.
- **Retrospective:** the user says "last fall we renovated the laundry."
  Create a project with a **bounded window** (`--start`/`--end`) and run
  the matcher immediately over history. For a **bulk import from a
  hand-tracked spreadsheet**, follow the dedicated runbook
  `prompts/projects_import.md` (description-based matching, source-of-truth
  rows, expected statement-coverage gaps).

## Step 1: Interview the user (only as needed)
Gather enough to scope the project and seed its matchers. Ask only what you
can't infer:
- **Scope / name:** what is being done; propose a clear, specific name
  (e.g. "Master Bath Reno", not "Home stuff").
- **Window:** when did/does work start? Is it ongoing (open) or finished
  (set `--end`)?
- **Budget:** is there a planned budget to track against? (optional)
- **Known vendors:** any contractors, stores, or one-off suppliers you
  should watch for by name.

## Step 2: Seed the matching criteria
Read `$WORKSPACE/config/project_types.json` (template:
`config/project_types.example.json`) and pull the seed `keywords` and
`categories` for the project's `type`. Merge in any user-named vendors.
Keywords match case-insensitively against the transaction **description**;
categories against the transaction **category**.

```
housebook-audit create-project "Master Bath Reno" \
    --type renovation --start 2025-09-01 --budget 12000 \
    --location "Master Bathroom" \
    --keywords "HOME DEPOT,LOWES,MAPLE TILE,VAULT PLUMBING" \
    --categories "Home Improvement,Professional Services"
```
Omit `--end` for an open/ongoing project. Criteria live on the project row
and can be edited later (re-create, or `verify` rows directly).

## Step 3: Match candidates
```
housebook-audit match-project            # sweep EVERY open project
housebook-audit match-project 3 --json   # one project, machine-readable
```
The matcher is **high-signal**: a candidate must hit a keyword OR a
configured category to appear at all. Amount (`large_amount`) and
`known_vendor` only *boost* an already-qualifying row. Each candidate
carries a `signals` list explaining why it surfaced and a `score`.

## Step 4: Review — necessary, not sufficient
A match inside the window is a **signal, not a verdict** — exactly like the
trip rule that a date inside a trip window is necessary but not sufficient.
A Home Depot charge during a bathroom reno may really be an unrelated
garden-hose replacement. Confirm intent before assigning.

Judgment notes:
- **`generic_category` signal:** the row matched only on a broad bucket
  (Uncategorized / Miscellaneous / Shopping & Retail). Scrutinize harder.
- **Obscure vendor not surfacing:** a one-off supplier with no keyword
  won't appear. Either assign it directly by id, or add its name to the
  project's keywords so it (and future charges) score in.
- **Split charge** (one store run, half reno / half routine): assign by
  dominant intent, or leave it for the user — do not force it in.
- **Returns:** a refund (negative amount) from the project's vendors will
  surface too. If it cancels a specific purchase, pair it via
  `prompts/reconcile_returns.md` (`housebook-audit link`); the project's net
  spend then reflects the return. Standalone partial refunds: flag for the
  user.

## Step 5: Assign
```
housebook-audit verify 8120,8125-8130 --project 3 --category "Home Improvement"
```
`verify` sets `AGENT_VERIFIED`, clears `needs_review`, and stamps
`project_id`. Once at least one row from a vendor is assigned, the matcher
learns it (`known_vendor` boost) on the next run.

## Step 5b: Record off-ledger costs (craftsmanship, lump totals)
Real projects incur costs that never hit a card statement — cash/check
labor, contractor invoices, lump material totals from a hand-tracked
sheet. These are **not** in `transactions` and must not be hand-inserted
there (the source ledger is immutable). Record them as project-linked
one-time manual expenses:
```
housebook-audit add-manual "Workmanship — Maple Tile Co." --amount 9500 \
    --category "Home & Garden" --date 2025-11-14 --project 3
```
`project-summary` and `projects` roll these into the project total
alongside assigned transactions. Use a real category (e.g. `Home &
Garden` for craftsmanship/services) so the by-category breakdown stays
meaningful.

## Step 6: Report
```
housebook-audit projects                 # open projects with spend/budget
housebook-audit project-summary 3        # net spend, budget, by-category
```
Net spend sums **signed** amounts (charges +, refunds −) over the visible
spending set, so linked returns net out correctly.

## Step 7: Close (when done)
```
housebook-audit close-project 3 --freeze-end
```
`--freeze-end` sets `end_date` to the last assigned transaction's date.
Closed projects drop out of the `match-project` sweep but remain fully
reportable (`projects --status closed`, `project-summary`).

## SOP maintenance
This is a living document. When a session reveals a new heuristic (a vendor
that should always seed a renovation, a category that produces false
positives), update `config/project_types.json` and this SOP immediately.
