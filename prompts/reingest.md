# Re-ingest / Repair Runbook

How to safely re-ingest a source to **correct historical data** in
`finance.db` — after an ingestor bug fix, a workspace reorganization,
or whenever you suspect the DB has drifted from the source files.
Applies to all four sources (cc, amazon, tax, hsa).

## When to use this

- An ingestor bug was fixed and previously-ingested rows are wrong or
  missing (e.g. an ingestor that once dropped in-file duplicates).
- Sidecars were **renamed or reclassified** and `processed_files` no
  longer matches what's on disk.
- You want to verify, then repair, DB faithfulness to the source files.

## Why re-ingest is safe (additive, never destructive)

Re-ingesting **only backfills rows the DB is missing**. It never
deletes, edits, or double-inserts an existing row, because of two
layers of dedup:

1. **File level** — `is_file_processed(path, hash)` short-circuits any
   sidecar/CSV whose path+content hash is already recorded. Re-running
   without clearing `processed_files` is a no-op.
2. **Row level** —
   - CC & Amazon: `Database.transaction_exists(...)` is
     **multiplicity-aware** — the ingestor passes
     `max_duplicates=<occurrence within this file>` (and `profile` for
     Amazon), so the Nth identical line is skipped only when the DB
     already holds N copies.
   - Tax: a **content guard** in `tax/ingestor.py` skips the insert
     when a row with the same `(tax_year, document_type, issuer,
     amount)` already exists.

So a whole-source re-ingest reconciles the DB to the current source
files and stops there.

## Procedure

1. **Back up first** (always, before touching `finance.db`):
   ```
   cp "$WORKSPACE/data/finance.db" \
      "$WORKSPACE/data/backups/finance.pre-<reason>.$(date +%Y%m%d_%H%M%S).db"
   ```
2. **Clear file-level tracking** for the source you're repairing so its
   files reprocess. Paths in `processed_files` are **workspace-relative**
   (normalized via `_to_relative_path`). Clear only the source in scope:
   ```sql
   DELETE FROM processed_files WHERE file_path LIKE 'cc/%';
   -- or 'amazon/%', 'tax/%', 'hsa/%'
   ```
3. **Re-run the ingest:**
   ```
   housebook-cc ingest          # or housebook-amazon / housebook-tax / housebook-hsa
   ```
   On cc/amazon, add `--verbose` to print every row the dedup check
   suppresses. A non-zero **"duplicate row(s) skipped"** on a *fresh*
   ingest (one that should be inserting new rows) is a red flag — it
   means rows are being silently absorbed; investigate before trusting
   the result.
4. **Verify** (next section), then delete any stale `processed_files`
   orphans the rename left behind.

> **Trust the ingest summary line for "how many rows are new," not
> `processed_files.last_processed`.** Every ingest bumps
> `last_processed` to *now* for **all** files it walks, including
> unchanged ones it skips — so a recent timestamp does **not** mean a
> file contributed new rows, and you cannot reconstruct the new-vs-old
> split from that column. The authoritative count of rows actually
> inserted this run is the ingestor's own summary
> (e.g. `CC ingest: 34 statement(s), 840 transaction(s), 108
> unchanged.`). All four ingestors now report `new / unchanged`
> counts in this form; capture that line rather than querying the DB
> for it.

## Verifying faithfulness to the source

Compare **per-key multiplicity** of `(date, description, amount[,
profile])` between the source files and the DB.

> **CRITICAL: normalize the amount numerically, never by string.**
> `Decimal`/`float` string forms differ — `"106"` vs `"106.0"`,
> `"-7"` vs `"-7.0"` — and a string-keyed comparison fabricates
> phantom shortfalls on every whole-dollar amount. Canonicalize **both
> sides** with `f"{float(x):.2f}"`. (The ingestor's own dedup compares
> numerically: `amount = ? OR ABS(amount - ?) < 0.005`.)

A clean repair shows **shortfall = 0** and **over-source = 0**. Keys
present only in the DB ("DB-only") are normal — they are rows from an
older export no longer in the current file (the DB accumulates).

## processed_files hygiene

A rename/reorg produces two distinct, simultaneous signatures:

| Signature | Meaning | Fix |
|-----------|---------|-----|
| **Orphan** | `processed_files` entry whose file is gone from disk | Harmless (can never re-trigger). Safe to `DELETE`. |
| **Untracked** | File on disk with no `processed_files` entry | Will (re)ingest on next run. If already represented in the DB, re-ingesting **self-heals** the tracking — the row-level guards prevent duplicates. |

Detect them:
- Orphans: for each `processed_files.file_path`, check
  `os.path.exists($WORKSPACE/<path>)`.
- Untracked: for each source file on disk, check membership in
  `processed_files`.

Run `housebook-<source> check` after any workspace reorganization.
