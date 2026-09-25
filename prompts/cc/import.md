# CC Statement Import SOP

## CRITICAL: Strict Formatting & Idempotency
- **DATES MUST USE HYPHENS** (e.g., `2025-01-29`), **NEVER UNDERSCORES**.
- Filenames and sidecars are used for deduplication. Any variation in formatting (like using `_` instead of `-`) will cause the system to treat the file as new, leading to **duplicate transactions** in the database.
- **Always check the DB before filing.** Use the extracted statement end date to query `processed_files` and ensure this period isn't already covered.

## Purpose

This SOP guides an AI agent through importing credit-card statement
PDFs from any location. The output is a set of standardized files
paired with JSON sidecars in `$WORKSPACE/cc/YYYY/`. This is
**Phase 1** — you do NOT run `housebook-cc ingest` from this SOP.

## Workflow Overview

```
Agent receives file path(s) from user
        |
        v
[Phase 1: import]     Agent follows THIS SOP
                      -> identifies if file is consolidated (multi-statement)
                      -> splits consolidated files at exact boundaries
                      -> reads PDF via pdftotext
                      -> extracts issuer, period, account, transactions
                      -> renames file to canonical convention
                      -> writes envelope-wrapped JSON sidecar
                      -> moves source file to $WORKSPACE/cc/YYYY/
                      -> runs pre-flight validation
                      -> STOPS — does not run ingest
        |
        v
[User review gate]    Agent presents summary; user approves
        |
        v
[Phase 2: ingest]     housebook-cc validate
                      housebook-cc ingest --dry-run
                      housebook-cc ingest
```

## Handling Consolidated Files (Multi-Statement PDFs)

Some issuers (Synchrony/Lowes/TJMax) staple multiple months into one PDF.
1. **Detect**: Check for multiple "Page 1 of X" headers or multiple "Account Summary" blocks.
2. **Locate Boundaries**: Do NOT assume fixed page counts. Search for every occurrence of "Page 1 of" to find the start of each new statement.
3. **Split**: Use `pdfseparate` and `pdfunite` to create individual 1-period PDFs.
4. **Note on Synchrony**: These PDFs often contain invisible "image" pages. Always verify the split results by checking that each new PDF starts with "Page 1" and ends with the correct final page number.

## DB-assisted mode (for PDFs of previously ingested statements)

If this PDF was **already ingested** by the old parser pipeline
(check via `SELECT * FROM transactions WHERE original_file LIKE
'%<old-filename>%'`), you can short-circuit extraction:

1. Pull transactions from the DB (same set the old parser extracted).
2. Read only the PDF's **first page** for summary fields (account
   last4, balances, due date, exact statement period).
3. Build the sidecar from DB + page-1 data.

This avoids re-extracting from the PDF body and guarantees the new
sidecar matches the existing ground truth exactly.

## From-scratch mode (for new PDFs not in the DB)

1. Extract full text via `pdftotext -layout <file> -`.
2. Parse transactions per page; build the transactions array.
3. Read the first page for summary fields.
4. Build the sidecar from scratch.

## Per-issuer format quirks

Before parsing, read **`$WORKSPACE/config/cc/issuers.json`** to
understand the issuer's PDF layout. Key quirks documented there:

### Year inference — the BoA bug

BoA statements print the period as `"December 12 - January 11, 2025"`
with only one year (the closing year). **The critical rule**:

> If `start_month > end_month`, the start year is `end_year - 1`.

Example: "December 12 - January 11, 2025" → start = **2024**-12-12,
end = 2025-01-11. NOT 2025-12-12 → 2025-01-11.

Other issuers (Amex, Fidelity) print explicit per-side years.

### Pre-flight check (mandatory)

After building the sidecar `data` block and **before writing it**,
verify:

1. **`start <= end`** — catches the BoA year-inference bug.
2. **Period span is 20–40 days** — catches multi-month or
   zero-length period errors.
3. **All transaction dates are within `[start - 14d, end + 14d]`**
   — catches year-off-by-1 on individual transactions. The 14-day
   grace handles car-rental / hotel / international posting lag.
4. **Transaction count and sum match expectations** — if DB-assisted,
   match the DB exactly. If from-scratch, verify against the
   statement's printed totals or page-bottom subtotals.

If any check fails, **stop and escalate** — do not write the sidecar.

You can run `housebook-cc validate` to check all sidecars at once
after the import pass.

## Canonical filename

```
YYYY-MM__<Issuer>__<Last4>__<Start>_to_<End>.pdf
```

- `YYYY-MM` is the statement **closing** month.
- `<Issuer>` from `config/cc/issuers.json` or the DB's `source`
  field (with spaces → hyphens).
- `<Last4>` from the PDF's first page. Use `XXXX` if unreadable
  AND the issuer doesn't always print it (Synchrony store cards).
  **Escalate** if unreadable for Amex/BoA/Fidelity — those always
  print it.
- `<Start>` and `<End>` are ISO dates from the statement period.

Examples:
```
2025-04__Amex__1111__2025-03-05_to_2025-04-03.pdf
2025-01__BoA__2222__2024-12-12_to_2025-01-11.pdf
2025-08__Vault-HSA__3333__2025-07-20_to_2025-08-19.pdf
2026-02__Home-Goods__4444__2026-01-15_to_2026-02-14.pdf
```

Destination: `$WORKSPACE/cc/<YYYY>/` where YYYY = end_year.

## Sidecar format (v1 envelope)

```json
{
  "schema_version": "1",
  "source": "cc",
  "source_file": {
    "path": "cc/2025/<canonical>.pdf",
    "sha256": "<sha256 hex of the source PDF>",
    "size_bytes": 123456,
    "mime_type": "application/pdf"
  },
  "classified_at": "<UTC ISO-8601 Z>",
  "classified_by": "agent",
  "classifier_notes": null,
  "data": {
    "issuer": "Amex",
    "account": {
      "last4": "1111",
      "name": "CARDHOLDER NAME"
    },
    "statement_period": {
      "start": "2025-03-05",
      "end": "2025-04-03"
    },
    "balances": {
      "opening": 2400.00,
      "closing": 3150.00
    },
    "payment": {
      "due_date": "2025-04-28",
      "minimum_due": 65.00
    },
    "transactions": [
      {
        "date": "2025-03-07",
        "description": "MERCHANT NAME CITY ST",
        "amount": 20.00,
        "category": null,
        "metadata": null,
        "page": null
      }
    ],
    "tx_count_db": null,
    "tx_total_db": null
  }
}
```

- `category` may be null — the audit SOP (`monthly_audit.md`)
  handles categorization after ingest.
- `metadata` is free-form JSON for extra context the PDF provides
  (e.g., Amex's merchant category, flight details, passenger names).
- `page` is 1-indexed. Populate if you can; null is acceptable.
- `tx_count_db` and `tx_total_db`: set when DB-assisted (for
  reconciliation). Null for from-scratch mode.

## Escalation rules — when to ASK rather than guess

| Condition | Why |
|---|---|
| `start > end` after year inference | BoA-style bug — never guess, always stop |
| Account last4 unreadable for Amex/BoA/Fidelity | These always print it; unreadable = PDF issue |
| Zero transactions extracted from a non-empty PDF | Format changed; don't force it |
| Transaction total doesn't match statement's printed total | Parser mistake or multi-page split error |
| Statement period spans > 40 days or < 20 days | Wrong period extraction |
| Issuer can't be determined from PDF | Don't guess — ask |

## Completion

After all PDFs are processed, **STOP** — do not run
`housebook-cc ingest`. Present a summary in conversation listing
what was imported (filename, issuer, period, tx count), then wait
for user approval.

## After user approval — Phase 2: Ingest

```bash
housebook-cc validate            # structural checks pass
housebook-cc ingest --dry-run    # preview
housebook-cc ingest              # commit to DB
housebook-cc summary             # verify totals
```
