# Document Import SOP (Unified Entry Point)

## Purpose

This SOP guides an AI agent through importing raw financial documents
from any location into the workspace. "Import" means: read the file,
determine its source type, rename it to the canonical convention,
generate an envelope-wrapped JSON sidecar, and move both to
`$WORKSPACE/<source>/YYYY/`.

This is **Phase 1** of the two-phase pipeline. Phase 2 (ingest) is a
separate step that reads sidecars and populates the database.

## Workflow

```
User says: "import these files from <DIR>"
        |
        v
[Determine source]    Agent reads file content, determines type:
                      - CC statement PDF -> cc/
                      - Medical receipt/EOB/invoice -> hsa/
                      - Tax form (W2, 1099, 1098, ...) -> tax/
                      - Amazon zip export -> amazon/
        |
        v
[Per-source import]   Agent follows the source-specific SOP:
                      - prompts/cc/import.md
                      - prompts/hsa/import.md
                      - prompts/tax/import.md
                      (Amazon uses `housebook-amazon import <zip>`)
        |
        v
[User review gate]    Agent presents summary of what was imported
                      User reviews, resolves ambiguities, says "go"
        |
        v
[Phase 2: ingest]     housebook-<source> ingest --dry-run
                      housebook-<source> ingest
```

## Source Detection Heuristics

Read the file (pdftotext for PDFs, direct for XLSX/CSV/ZIP):

| Signal | Source | Confidence |
|--------|--------|-----------|
| Statement period, card last4, transaction list | CC | High |
| Medical provider, patient name, CPT codes, EOB | HSA | High |
| "W-2", "1099", "1098", tax year, EIN | Tax | High |
| ZIP containing CSV files with Amazon order columns | Amazon | High |
| Ambiguous (e.g., pharmacy charge on CC statement) | CC | Use amount: CC statement has many txns |

When uncertain, ask the user rather than guessing.

## Common Rules (all sources)

1. **Dates use hyphens**: `2025-01-29`, never underscores
2. **No ingest from this SOP** — stop after import, wait for user
3. **No housebook-sync** — syncing is a separate user-triggered operation
4. **Escalate ambiguity** — if unsure about type, entity, or amount, ask
5. **Sidecar envelope v1** — all sidecars use the unified envelope format
   (see `src/housebook/core/sidecar.py`)

## Per-Source Details

Each source has domain-specific rules for naming conventions, field
extraction, and sidecar schemas. Follow the relevant SOP:

- **Credit cards**: `prompts/cc/import.md`
- **HSA medical docs**: `prompts/hsa/import.md`
- **Tax documents**: `prompts/tax/import.md`
- **Amazon**: No SOP needed — run `housebook-amazon import <zip> --profile <name>`
