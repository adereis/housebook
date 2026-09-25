# Tax Module

The tax pipeline uses the same unified import→sidecar→ingest
pattern as HSA and CC. Each tax document (W2, 1099, etc.) gets a
sidecar with form-specific `form_data` fields.

| Aspect | Value |
|--------|-------|
| DB table | `tax_documents` |
| Workspace dir | `tax/2025/` (year dirs) |
| Import SOP | `prompts/tax/import.md` |
| Audit SOP | `prompts/tax/audit.md` |
| Extract helper | `prompts/tax/extract_info.md` |
| Config | `config/tax/forms.json` (future) |
| CLI | `housebook-tax` |
| Web route | `/tax` |

## Tax document types

| `document_type` | `category` | Key fields in `form_data` |
|-----------------|------------|--------------------------|
| W2 | Income | `federal_tax_withheld`, `state_tax_withheld` |
| 1099 | Income/Interest | `dividends`, `capital_gains`, `interest` |
| 1099-R | Retirement | `taxable_amount`, `distribution_code` |
| 1098 | Deduction | `outstanding_principal`, `real_estate_taxes` |
| 1099-HC | Health | (none — compliance doc) |
| 1095-C | Other | (none — coverage proof) |
| BR-TAX-REPORT | Income | `total_income`, `interest_income`, `pfic_*`, `fbar_*` |
| BR-FTC | Tax Paid | `total_ftc`, `irrf_*` |

## Agent workflow for tax

```
# Phase 1: Import new documents
# User provides file path(s); agent follows prompts/tax/import.md
# STOP and wait for user approval.

# Phase 2: Validate + ingest
housebook-tax validate                # structural checks
housebook-tax summary --year 2025     # quick orientation
housebook-tax check                   # data quality
housebook-tax list --year 2025        # flat listing

# Machine-readable
housebook-tax summary --year 2025 --json
housebook-tax check --json
```

All subcommands accept `--json` for agent consumption and `--db`
to override the database path.

One sidecar is one database transaction. This matters for the Brazilian
multi-document workbook: `BR-TAX-REPORT`, `BR-FTC`, their provenance,
and the `processed_files` marker either all commit or all roll back.
Success output is deferred until the commit succeeds.

## Tax estimate scope (`housebook-tax estimate`)

The estimator's parameter tables are **2025, Married-Filing-Jointly,
Massachusetts** — brackets, standard deduction, LTCG thresholds, NIIT
threshold, and the flat state rate. They live in the immutable registry
`tax/parameters.py`, selected only by an exact `(year, status, state)`
key. The engine **refuses** anything outside that registry with an
explanatory error; there is no nearest-year fallback. Supporting another
year or filing status means adding a complete, sourced entry — never
relaxing the guard, which exists because
the tool previously returned confidently-labeled wrong figures (2025
brackets under a "2019" heading, MA's rate labeled "CA").

Within scope, note two things the numbers depend on:

- **Preferential income stacks on top of ordinary income.** Qualified
  dividends and long-term gains are taxed by *where they land* in
  total taxable income (0% / 15% / 20%), not at a flat rate — and the
  deduction shelters them once ordinary income is exhausted. A modest
  filer's long-term gains can legitimately owe $0.
- **Documents with a NULL amount contribute nothing** and are listed
  under `unknown_amounts` (the CLI leads with an INCOMPLETE warning).
  Extract those amounts before trusting the total. `housebook-tax check`
  reports them as `missing_amount` **errors**, distinct from a genuine
  `$0` (`zero_amount`, a warning).

### Accepted multi-year direction & known estimator gaps

The engine will support multiple years and reconcile immutable estimate
snapshots against federal/state filed-return evidence. Each
year/status/jurisdiction remains fail-closed until its own sourced
parameters and tests exist; do not reuse a neighboring year. IRS/state
transcripts are reconciliation artifacts in a separate evidence layer,
never duplicate income rows in `tax_documents`. Full design and download
guidance: `docs/architecture/tax-security-roadmap.md`.

**Do not treat the estimator's output as filing-grade until the gaps
below are resolved and a year is compared against filed evidence.**

**1. Capital-loss carryover only offsets long-term gains.** (confirmed)
`estimate.py` gates the offset on `net_lt > 0`, so when there are no
long-term gains only the `min(carryover, 3000)` ordinary offset
applies. Verified: a $50,000 carryover against $50,000 of *short-term*
gains applies just **$3,000**, overstating taxable income by $47,000
(~$10–11k of phantom tax at 22–24%). A carryover should offset capital
gains of both types before the $3,000 ordinary-income limit.

**2. NIIT base uses gross, pre-carryover figures.** (confirmed)
`net_inv` sums the raw `st_gains` / `lt_gains` aggregates rather than
`net_st` / `net_lt`, so losses already netted out are taxed again at
3.8%. It also folds in `br_income` (the whole `BR-TAX-REPORT` total)
as investment income wholesale, which it is not.

**3. `1098` real-estate taxes are ingested but never used.** (confirmed)
Only `amount` (mortgage interest) is read from 1098 rows; SALT is
`min(state_withheld, cap)`. This doc lists `real_estate_taxes` as a
captured `form_data` field, so the data is collected and silently
dropped — understating itemized deductions and potentially flipping a
filer to the standard deduction incorrectly.

## Known gotchas

- **Brazilian Tax Report** is the only non-US tax document
  ingested. It's a structured XLSX that produces **two** DB rows
  from one file (`BR-TAX-REPORT` + `BR-FTC`). The sidecar uses
  the `documents[]` array for this case.
- **JPG/PNG tax docs** (e.g., W2 scans) rely on OCR via
  `pytesseract`. Extraction quality varies — when the amount can't
  be read, write `"amount": null` in the sidecar (never `0`, which
  asserts the form reports zero). NULL propagates to the DB and
  surfaces as a `missing_amount` error in `check` and under
  `unknown_amounts` in the estimate; the Agent should verify manually.
- **Deduplication is path-based only** — if the same file exists
  under different directory paths, it will be ingested twice.
  Run `housebook-tax check` after any workspace reorganization.
