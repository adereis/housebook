# HSA EOB Acquisition SOP (Phase 0)

## Purpose

This SOP guides an AI agent through **acquiring** insurer Explanation
of Benefits (EOB) PDFs that the HSA shoebox is missing, from the
insurer's member portal. The browser mechanics, the mode of
operation, and the hard invariants are shared by every source and
live in **`prompts/acquire.md`** — read it first. This file adds what
is specific to EOBs: deciding which claims to fetch, checking each
file against its claim, the inbox, and where portal notes live.

```
[Phase 0: acquire]  prompts/acquire.md + THIS SOP → raw PDFs in $WORKSPACE/hsa/_inbox/
        |
        v
[Phase 1: import]   prompts/hsa/import.md — rename, sidecar, move to hsa/YYYY/
        |
        v
[Phase 2: ingest]   housebook-hsa ingest
        |
        v
[Phase 3: reconcile] prompts/hsa/reconcile.md
```

## Step 1 — Decide WHAT to fetch

### The claim number is the dedup key

Every EOB carries a claim number, and every filed EOB sidecar records
it as `data.claim_id`. Collect the filed set first:

```bash
python3 -c "
import glob, json, os
ws = os.environ['HOUSEBOOK_WORKSPACE_DIR']
ids = {json.load(open(f))['data'].get('claim_id')
       for f in glob.glob(f'{ws}/hsa/*/*__EOB__*.json')}
print(len(ids - {None}), 'claim ids on file')"
```

Do not dedup by the latest service date on file. Insurers process
claims weeks after the visit, so a claim for a visit before your
latest filed EOB can still be new. A lab visit in May can first
appear as a processed claim in July.

### Run a coverage pass over the insurer's whole list

Load the portal's claim list for the widest range it offers (often
24 months), for **every** family member, and compare it with what is
filed. When the list shows claim numbers, compare by claim number.
When it does not, compare the multiset of (service date, patient,
patient responsibility) against the filed sidecars. Count repeats:
two visits on the same day for the same amount are two claims.

Treat every mismatch as a question, not a gap to fill. Before
concluding that a claim is missing, read the text of the filed PDFs
around it. In one run, the "missing" claims were present but filed
under a neighboring claim's date and provider, because an older bulk
import had paired PDFs with rows by order. Re-reading the PDFs
exposed the misfiling.

### Fetch only what belongs in the shoebox

- **Claim types paid from another account stay out.** Read
  `special_notes` in `$WORKSPACE/config/user_profile.json` before
  choosing claim types. For example, a family that pays dental from
  an FSA keeps dental EOBs out of the HSA shoebox entirely.
- **A denied claim usually has no EOB.** The portal marks it "EOB not
  available." Nothing to fetch.
- **A claim still processing has no EOB yet.** Leave it for the next
  run and say so in the summary.
- **A $0.00 EOB is still worth filing.** It ingests as a document
  with no expense row, and it completes the claim record.

## Step 2 — Drive the portal

Follow *Driving the browser* in `prompts/acquire.md`, reading the
insurer's `navigation` block first (see below). Portal-specific
lessons that recur:

- **Set every filter, then Apply.** Member ("all"), date range, and
  claim type are separate filters, and the list does not change until
  the filter panel's Apply control is clicked.
- **Load the whole list.** The list adds rows as you scroll. Scroll to
  the end and wait until the row count matches the total the page
  states.
- **Download each EOB individually.** Pair every link with its claim
  row structurally and name the file from that row's text, as
  `prompts/acquire.md` describes under *Links inside web components*.
- **Avoid the bulk "download all" export.** It saves a stack of
  identically named PDFs (`contents.pdf`, `contents (1).pdf`, …) plus
  a CSV, and leaves you to pair them by order. If the user hands you
  one anyway, `prompts/hsa/import.md` says how to file it safely.

## Step 3 — Check every file against its claim

Before staging, read each PDF's text and confirm it is the claim
whose row you clicked:

```bash
pdftotext -layout <file>.pdf - | grep -E 'Claim # |services on|services provided by|What I owe'
```

The claim number must be new. The patient, service date, provider,
and amount owed must match the row. Any mismatch stops the run until
you understand it. A file named for one claim that holds another is
exactly the error this SOP exists to prevent.

## Step 4 — Stage in the inbox

Move each verified PDF into `$WORKSPACE/hsa/_inbox/` per *Staging in
the inbox* in `prompts/acquire.md`. Name script-saved files from the
row, for example `shield-eob-2030-03-09-penny-maple-dental-25.00.pdf`.

The HSA validation reports every PDF without a sidecar, and it does
not skip `_inbox/`. A raw EOB waiting there shows up as a "missing
sidecar" until the import SOP files it. That is the loud signal that
an import is pending, so import in the same session or tell the user
the inbox is not empty.

## Step 5 — Hand off

Summarize what was downloaded per patient and claim type, what was
skipped (already on file, denied, still processing, excluded type),
and anything the coverage pass found misfiled. Then hand off as
`prompts/acquire.md` describes: `prompts/hsa/import.md` on
`$WORKSPACE/hsa/_inbox/`, with its review gate, then ingest and
`prompts/hsa/reconcile.md`.

## Per-insurer navigation notes (living)

The insurer's portal notes live on its own entry in
`$WORKSPACE/config/hsa/providers.json`, under a `navigation` block in
the shape `prompts/acquire.md` documents. The provider resolver reads
only the canonical name, aliases, category, and billing lag, so the
block is free-form text for the agent. With fictitious values:

```jsonc
// $WORKSPACE/config/hsa/providers.json  (LIVE config — not in repo)
{
  "canonical_name": "Shield-Health",
  "category": "insurance",
  "aliases": ["Shield Health Plans"],
  "navigation": {
    "verified": "2030-01-15",
    "login_url": "https://member.shield.example",
    "list_path": "Claims > View claims and EOBs; filters need Apply",
    "download": "EOB link opens a viewer tab with a short-lived URL",
    "pdf_password_rule": "None",
    "cadence": "EOBs appear 2–5 days after a claim is processed"
  }
}
```
