# CC Statement Acquisition SOP (Phase 0 — Browser-Drive)

## Purpose

This SOP guides an AI agent through **acquiring** credit-card statement
PDFs by driving the user's own Chrome browser (via the
`claude-in-chrome` tools) to log into each issuer and download the
missing statement documents. This is **Phase 0** — the step that runs
*before* import.

Its only output is the same artifact a human produces today by
downloading from a bank site: **a raw, un-renamed statement PDF on
local disk**. From there, the unchanged import flow takes over
(`prompts/import.md` → `prompts/cc/import.md`).

```
[Phase 0: acquire]  THIS SOP — drive browser, download raw PDFs → $WORKSPACE/cc/_inbox/
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

## CRITICAL: Mode of operation — attended, headful, your own session

This SOP runs **only** in attended, human-in-the-loop mode, and it is
**user-initiated** (the user explicitly asks to fetch statements). It
is NOT a cron job and MUST NOT be run unattended.

- **Headful, real Chrome, the user's own profile, the user's home IP.**
  This is the only configuration that reliably passes issuer bot
  detection (Chase/Amex run Akamai-class fingerprinting that flags
  headless automation in milliseconds). Reuse the user's existing
  logged-in session — do **not** open a fresh headless context.
- **The human taps the MFA OTP.** Several issuers (e.g. Citi) force an
  SMS one-time code on every login with no remember-device bypass.
  When a login or step-up challenge appears, **pause and ask the user
  to complete it**, then continue. Never attempt to read, guess, or
  automate the OTP.
- **No credentials are stored, ever.** Acquisition relies on the user's
  live browser session. Do not save bank usernames, passwords, OTP
  secrets, or a separate cookie jar to disk, a sidecar, or the DB.
- **ToS awareness.** Some issuers' terms restrict automated/agent
  access (Chase's Digital Services Agreement names AI agents
  explicitly). Keeping this attended, low-frequency (monthly), and
  inside the user's own session is what keeps it indistinguishable from
  ordinary use. If the user is uncomfortable for a given issuer, fetch
  that one manually instead.

## Hard invariants — what the acquirer MUST NOT do

The acquirer's **sole output** is a raw PDF at a path. To keep the
dedup contract and the deterministic core intact, it must NOT:

1. **Rename** files to the canonical
   `YYYY-MM__<Issuer>__<Last4>__<Start>_to_<End>.pdf` form — that is
   the import SOP's job and the canonical name is the dedup key.
2. **Author sidecars** — the v1 envelope is built by `prompts/cc/import.md`.
3. **Write into `$WORKSPACE/cc/<YYYY>/`** directly, or touch the DB /
   `transactions` / `processed_files` — all downstream of import.
4. **Run `housebook-cc ingest`, `housebook-audit`, or `housebook-sync`** —
   those are separate, later, user-gated steps.

If you find yourself doing any of the above, stop: you have crossed out
of Phase 0.

## Workflow

### Step 1 — Decide WHAT to fetch (coverage gaps)

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

### Step 2 — Drive the browser (per issuer)

1. Call `tabs_context_mcp` first to see the user's current tabs. Only
   reuse a tab if the user asks; otherwise open a new one.
2. Read the issuer's **navigation profile** from
   `$WORKSPACE/config/cc/issuers.json` (login URL, the path to the
   "Statements & Documents" page, last4 reliability, and the
   PDF-password rule — see *Per-issuer navigation notes* below).
3. Navigate to the issuer login page. If not already authenticated,
   **pause and ask the user to log in and complete MFA** in the
   browser, then resume.
4. Navigate to the statements/documents page. Use **semantic**
   navigation (`read_page` / `find` for link text like "Statements",
   "Documents", "Download PDF") rather than brittle hard-coded
   selectors — issuer layouts change a few times a year.
5. Enumerate the available statement periods; cross-reference the gap
   list from Step 1. For each **missing** period, trigger the PDF
   download.
6. Confirm each file actually landed (non-zero bytes, `%PDF` header).

> **Avoid dialogs.** Do not trigger JavaScript `alert`/`confirm`
> dialogs — they freeze the extension. If a "Delete"/destructive
> control is near the download link, navigate around it.

### Step 3 — Normalize into the inbox

Move each downloaded PDF into the staging inbox **with its original /
issuer-given filename unchanged**:

```
$WORKSPACE/cc/_inbox/
```

- `_inbox/` lives under `$WORKSPACE` (never the repo tree) and holds
  **only raw, un-imported PDFs** — no sidecars, so `housebook-cc ingest`
  (which walks `cc/` for `*.json`) never picks it up.
- It is transient: the import SOP **moves** each PDF out of `_inbox/`
  into `cc/<YYYY>/` and empties it. A non-empty `_inbox/` means
  "imported pending."

**Amex (and any password-protected PDF):** Amex statement PDFs are
encrypted with the **11-digit account number minus the leading zero**.
Decrypt before depositing (e.g. `qpdf --password=<pw> --decrypt in.pdf
out.pdf`) so `pdftotext` works during import. If the password rule is
unknown for an issuer, leave the file encrypted and flag it for the
user rather than guessing.

### Step 4 — Hand off and STOP

After all in-scope cards are fetched:

1. Present a summary in conversation: per card, which periods were
   downloaded (and which were skipped as already-covered), and the
   inbox path.
2. **STOP.** Do not import or ingest from this SOP. Hand off by
   following `prompts/import.md` on `$WORKSPACE/cc/_inbox/` once the
   user says "go".

## Failure handling — fail loudly

Per the project's no-silent-failure rule:

- **Downloaded 0 PDFs for a card that should have had new statements**
  → **warn**, don't shrug. State which card, what you saw on the page,
  and whether login/MFA/layout was the blocker. Do not fabricate or
  infer statement contents.
- **Login/MFA could not be completed** → pause and ask the user; do not
  retry credentials automatically.
- **Page layout unrecognizable / selectors all miss** → stop after 2–3
  semantic attempts (per the browser-automation rabbit-hole rule), tell
  the user what changed, and offer to fetch that card manually.

## Escalation rules — when to ASK rather than proceed

| Condition | Why |
|---|---|
| Issuer login requires MFA | Human must complete it; never automate OTP |
| A CAPTCHA / interactive challenge appears | Cannot/should not be auto-solved; ask the user |
| Repeated failed logins risk a fraud hold/lockout | Stop — protect the user's account; ask before retrying |
| Issuer ToS makes the user uncomfortable | Fetch that card manually instead |
| Encrypted PDF with unknown password rule | Don't guess; flag for the user |
| Downloaded count ≠ expected gap count | Surface the discrepancy before handing off |

## Per-issuer navigation notes (living)

Keep issuer-specific navigation knowledge in
`$WORKSPACE/config/cc/issuers.json` alongside the existing PDF-format
quirks, under a per-issuer `navigation` block, e.g.:

```jsonc
// $WORKSPACE/config/cc/issuers.json  (LIVE config — not in repo)
{
  "Amex": {
    "navigation": {
      "login_url": "https://www.americanexpress.com/",
      "statements_path": "Account Services → Statements & Activity → Statements",
      "pdf_password_rule": "11-digit account number minus the leading zero",
      "mfa": "app/SMS step-up on new device"
    }
  }
}
```

This file is **live workspace config**, never committed. A
`config/cc/issuers.example.json` template may document the *shape* of
the `navigation` block with fictitious values only.
