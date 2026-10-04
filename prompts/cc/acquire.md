# CC Statement Acquisition SOP (Phase 0)

## Purpose

This SOP guides an AI agent through **acquiring** credit-card
statement PDFs that the ledger is missing. The browser mechanics,
the mode of operation, and the hard invariants are shared by every
source and live in **`prompts/acquire.md`** — read it first. This
file adds only what is specific to card statements: deciding which
periods to fetch, the inbox, and where issuer notes live.

```
[Phase 0: acquire]  prompts/acquire.md + THIS SOP → raw PDFs in $WORKSPACE/cc/_inbox/
        |
        v
[Phase 1: import]   prompts/cc/import.md — rename, sidecar, move to cc/YYYY/
        |
        v
[Phase 2: ingest]   housebook-cc ingest — UNVERIFIED rows
        |
        v
[Phase 3: audit]    prompts/monthly_audit.md
```

## Step 1 — Decide WHAT to fetch (coverage gaps)

Do not blindly re-download everything. Compute the gap first:

```bash
housebook-ingest list --latest        # latest statement per source
housebook-ingest list --source <Issuer>   # one card in detail, with gap flags
housebook-cc summary                   # per-card row/period overview
```

For each in-scope card, determine the missing closing months (the
periods not yet covered in `processed_files`). Fetch only those. A
re-download of an already-covered period is harmless — the import
SOP's `processed_files`/period pre-check makes it a no-op — but
skipping covered periods is faster and lighter.

**Not every gap is missing data.** Store cards, and any card the user
rarely uses, issue no statement for a month without activity. A card
can go a year between statements. Before reporting a gap as missing,
check the issuer's own list, including the previous year's, for
whether a statement exists at all. The per-issuer `cadence` note
records which cards behave this way.

## Step 2 — Drive each issuer

Follow *Driving the browser* in `prompts/acquire.md`, one fresh tab
per issuer, reading the issuer's `navigation` block first (see
below). For each missing period, download the statement PDF and
confirm it landed on disk.

## Step 3 — Stage in the inbox

Move each PDF into `$WORKSPACE/cc/_inbox/` per *Staging in the inbox*
in `prompts/acquire.md`.

- **Issuers name files by the bare closing date.** Several issuers
  save `YYYY-MM-DD.pdf`, so two cards can produce the same name; that
  is why the move uses `mv -n`. When you must rename, use
  `<issuer-hint>-<last4>-<closing-date>.pdf`, never the canonical
  `YYYY-MM__<Issuer>__<Last4>__<Start>_to_<End>.pdf` import name.
- The inbox holds no sidecars, and `housebook-cc ingest` walks `cc/`
  for `*.json`, so a raw PDF waiting there is never ingested.

## Step 4 — Hand off

Summarize per card which periods were downloaded and which were
skipped as already covered, then hand off as `prompts/acquire.md`
describes: import starts from `prompts/import.md` on
`$WORKSPACE/cc/_inbox/` once the user says "go".

## Per-issuer navigation notes (living)

Each issuer's notes live in `$WORKSPACE/config/cc/issuers.json`,
under a `navigation` block beside the PDF-format fields, in the shape
`prompts/acquire.md` documents (`list_path` is the route to the
statement list). The loaders read only `name` and `aliases`, so the
block is free-form text for the agent. With fictitious values:

```jsonc
// $WORKSPACE/config/cc/issuers.json  (LIVE config — not in repo)
{
  "issuers": [
    {
      "name": "Acme-Card",
      "aliases": ["Acme Card"],
      // ...existing PDF-format fields...
      "navigation": {
        "verified": "2030-01-15",
        "login_url": "https://bank.example/login",
        "list_path": "Statements & Documents, under Account services",
        "download": "Download icon on each row; saves YYYY-MM-DD.pdf",
        "pdf_password_rule": "None",
        "cadence": "Closes on the 14th; no statement in inactive months"
      }
    }
  ]
}
```

This file is **live workspace config**, never committed. A
`config/cc/issuers.example.json` template may document the *shape*
of the `navigation` block with fictitious values only.
