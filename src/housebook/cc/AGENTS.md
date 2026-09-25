# Credit Card Module

CC statements follow the same import→sidecar→ingest pattern
as HSA. The historical backlog (77 PDFs) has been imported;
new statements go through the same flow.

| Aspect | Value |
|--------|-------|
| DB table | `transactions` (shared with Amazon) |
| Workspace dir | `cc/{2024,2025,2026}/` (year dirs); `cc/_inbox/` (raw acquired PDFs, transient) |
| Config | `config/cc/issuers.json` |
| Acquire SOP | `prompts/cc/acquire.md` (browser-drive, Phase 0) |
| Import SOP | `prompts/cc/import.md` |
| CLI | `housebook-cc` |
| Web route | `/spending` |

## Agent workflow for CC

```
# Phase 0 (optional): Acquire new statements — user-initiated, attended
# Agent drives the user's Chrome (claude-in-chrome) to download missing
# statement PDFs into cc/_inbox/ (prompts/cc/acquire.md).
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

The 14-day grace handles real-world posting lag (car rentals, hotels,
international merchants). Year-inference bugs are 330+ days off —
well outside the grace.

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
sidecars ("Home-Goods" vs the alias "Home Goods") otherwise becomes
two distinct sources and silently fragments one card's history — a
real bug that split Home-Goods into 55 + 13 rows.

Resolution is **exact-match only** (no substring fallback like the
HSA `ProviderResolver`), because "Chase" is a substring of
"Chase-Amazon" and substring matching would wrongly merge two real
cards. An unknown issuer passes through unchanged — a new card can be
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
in the root `AGENTS.md` for the full lifecycle and its two traps
(the 365-day `apply-rules` window and the wording/category mismatch).
