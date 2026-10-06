# Credit Card Module

CC statements follow the same import→sidecar→ingest pattern
as HSA. The historical backlog (77 PDFs) has been imported;
new statements go through the same flow.

| Aspect | Value |
|--------|-------|
| DB table | `transactions` (shared with Amazon) |
| Workspace dir | `cc/{2024,2025,2026}/` (year dirs); `cc/_inbox/` (raw acquired PDFs, transient) |
| Config | `config/cc/issuers.json` |
| Acquire SOP | `prompts/acquire.md` (shared browser-drive) + `prompts/cc/acquire.md` (Phase 0) |
| Import SOP | `prompts/cc/import.md` |
| CLI | `housebook-cc` |
| Web route | `/spending` |

## Agent workflow for CC

```
# Phase 0 (optional): Acquire new statements — user-initiated, attended
# Agent drives the user's Chrome (claude-in-chrome) to download missing
# statement PDFs into cc/_inbox/ (prompts/acquire.md, then
# prompts/cc/acquire.md).
# Output is a raw PDF only; it then enters Phase 1 unchanged.

# Phase 1: Import new statements
# User provides file path(s) (e.g. cc/_inbox/); agent follows
# prompts/cc/import.md to rename, create sidecars, move to cc/YYYY/.
# STOP for user approval.

# Phase 2: Validate + ingest
housebook-cc validate                 # structural checks (mandatory gate)
housebook-cc ingest --dry-run
housebook-cc ingest

# Phase 3: Audit (cross-cutting)
housebook-audit pending --source Amex
housebook-audit verify <ids> --category "..."

# Data quality
housebook-cc check
housebook-cc summary --year 2025
```

## CC sidecar schema

Each sidecar wraps statement-level metadata plus a `transactions[]`
array inside the v1 envelope. Key fields in the `data` block:

- `issuer`, `account.last4`, `account.name`
- `statement_period.{start, end}` — ISO dates
- `balances.{opening, closing}`, `payment.{due_date, minimum_due}`
- `transactions[]` — each has `{date, description, amount, category,
  metadata, page}`
- `tx_count_db`, `tx_total_db` — reconciliation fields (set during
  DB-assisted import; null for from-scratch mode)
- `currency` (optional, ISO 4217, default `USD`), `fx_source`, and
  `transactions[].fx_rate` — see *Foreign-currency statements*
- `transactions[].installment` (optional `{number, of}`) and
  `excluded_transactions[]` (lines left out, each with a `reason`) —
  see *Occasional cards*

One sidecar is one database transaction: every transaction row,
provenance field, and the `processed_files` marker commits together.
An unexpected mid-statement failure rolls everything back, and the
writer reservation is acquired before the idempotency check so two
processes cannot ingest the same statement concurrently.

## Validators (defense-in-depth)

`cc/schema.py` runs structural checks on every sidecar before it
reaches the DB. These were written in direct response to a real bug
found during the bulk-import pass:

| Check | What it catches |
|-------|-----------------|
| `start <= end` | BoA single-year-format year-inference bug |
| Period span 20–40 days | Multi-month or zero-length period errors |
| Tx dates within `[start-14d, end+14d]` | Year-off-by-1 on individual transactions |
| `tx_count_db == len(transactions)` | Internal logic bugs in import |
| `sum(amounts) ≈ tx_total_db` | Amount-drift from rounding or missing rows |
| Non-USD: `fx_source` set, every row has a positive `fx_rate` | A foreign statement landing in the ledger unconverted |
| USD: no row has an `fx_rate` | A sidecar that forgot its `currency` |
| Excluded lines: each has a `reason`; imported + excluded = closing − opening | A line dropped by mistake hiding among deliberate exclusions |
| Installment parcel: `n/m` in the description; date may precede the period by `n − 1` extra cycles | Parcels dropped as duplicates of one another; year-off parcel dates |

The 14-day grace handles real-world posting lag (car rentals, hotels,
international merchants). Year-inference bugs are 330+ days off —
well outside the grace.

## Foreign-currency statements

The ledger has no currency column, and every view and total sums
`transactions.amount` as dollars. A foreign statement is therefore
converted **at ingest**, not stored in its own currency.

- The sidecar keeps the statement's own amounts and balances, so the
  import SOP's balance check still holds in that currency.
- Each row carries the `fx_rate` the agent looked up at import. The
  ingestor only divides and rounds half-up to the cent, so ingest
  stays offline and a re-ingest reproduces the same dollars.
- The row's `metadata.fx` keeps `{amount, currency, rate}`, so the
  conversion can be recomputed from the row alone. The web UI's
  transaction details show it.
- The duplicate check runs on the converted dollars, because that is
  what the DB holds.

A `currency` column was rejected. Every spending query, trip total and
project total would have had to convert, and a single forgotten
conversion would silently add reais to dollars. Converting once, at
the boundary, keeps every consumer unchanged. `prompts/cc/import.md`
says where the BRL rate comes from (Banco Central do Brasil PTAX).

## Occasional cards (imported for a trip only)

A card the user does not track month to month can still be imported
for a bounded stretch, such as a foreign card used on a single trip.
Its `cadence` note in `issuers.json` marks it occasional, so the
acquire SOP never reports its untracked months as gaps. Two sidecar
features make such a partial import honest:

- **`excluded_transactions`.** Its statements still print regular
  charges tracked elsewhere (a subscription kept as a recurring
  manual expense). Those lines are listed with a `reason` and
  skipped at ingest. Once any line is excluded, the validator
  requires imported + excluded lines to equal closing − opening
  balance. That is the only proof that the listed lines are all that
  was left out. It is not required of every sidecar, because about a
  third of the historical ones (DB-assisted imports that predate
  full-statement extraction) do not satisfy it.
- **`installment`.** A purchase split into parcels prints each parcel
  on a later statement under the purchase date. The marker widens the
  early date bound by one billing cycle per earlier parcel, instead
  of dropping the date check. It also requires `n/m` in the
  description, so parcels of one purchase stay distinct for the
  cross-statement duplicate check. A parcel billed after the import
  window closes is recorded as a one-time manual expense on the
  trip (`add-manual --trip`).

## Per-issuer quirks

`config/cc/issuers.json` documents format-specific gotchas that the
import SOP must handle. The most critical:

**BoA single-year format**: Prints `"December 12 - January 11, 2025"`
with only the closing year. Rule: if `start_month > end_month`,
start year = end_year - 1. This is codified in both the SOP and
the validator.

## Issuer canonicalization

The DB `source` column is the issuer name. It is **not** taken
verbatim from the sidecar — the ingestor runs `data.issuer` through
`IssuerResolver` (`cc/issuers.py`), which maps any known alias to the
canonical `name` in `config/cc/issuers.json` (normalizing `-`→space
and case). This exists because the same card written two ways across
sidecars ("Acme-Home" vs the alias "Acme Home") otherwise becomes
two distinct sources and silently fragments one card's history. This
was a real bug: one store card's rows ended up split across two
sources.

Resolution is **exact-match only** (no substring fallback like the
HSA `ProviderResolver`), because one issuer's name can be a substring
of another card's ("Summit" inside "Summit-Shop"), and substring
matching would wrongly merge two real cards. An unknown issuer passes through unchanged — a new card can be
added before `issuers.json` is updated.

`housebook-cc validate` / `check` also pass the resolver to
`validate_data_block`, which **flags** (does not block) a sidecar
whose `issuer` is a known alias rather than the canonical name, so
the sidecar gets cleaned up to canonical form. The ingestor
canonicalizes regardless, so the DB is correct either way.

## Config files

- **`config/cc/issuers.json`** — Per-issuer canonical `name` +
  `aliases`, period format, last4 reliability, and PDF quirks. The
  import SOP reads this before parsing any PDF; the ingestor and
  validator read it via `CC_ISSUERS_JSON` to canonicalize `source`
  (see *Issuer canonicalization* above).

## Categorization & spending-view behavior

The CC *ingestor* does NOT categorize — it is constructed with no
`Intelligence` and writes the sidecar's category or `Uncategorized`.
This has consequences for how `CC Payment` rows (excluded from
spending views) get set. See the **Spending View Filters** section
in the root `AGENTS.md` for the full lifecycle and its trap
(the 365-day `apply-rules` window).
