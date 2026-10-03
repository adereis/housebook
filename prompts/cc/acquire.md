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
  Large issuers fingerprint browsers and flag headless automation, so
  this is the configuration that reliably gets through. Reuse the
  user's existing logged-in session — do **not** open a fresh headless
  context.
- **The human taps the MFA OTP.** Some issuers send a one-time code on
  every login, with no way to remember the device. When a login or
  step-up challenge appears, **pause and ask the user to complete
  it**, then continue. Never attempt to read, guess, or automate the
  OTP.
- **No credentials are stored, ever.** Acquisition relies on the user's
  live browser session. Do not save bank usernames, passwords, OTP
  secrets, or a separate cookie jar to disk, a sidecar, or the DB.
- **ToS awareness.** Some issuers' terms restrict automated/agent
  access. For example, Chase's public Digital Services Agreement names
  AI agents explicitly: it holds the user responsible for any agent
  they give access, and says an agent must identify itself as one. Whether to
  drive a given issuer is the user's call, made per issuer, so raise
  it before the first run. Keeping the run attended, low-frequency
  (monthly), and inside the user's own session limits the exposure.
  If the user is uncomfortable for a given issuer, fetch that one
  manually instead.

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

**Not every gap is missing data.** Store cards, and any card the user
rarely uses, issue no statement for a month without activity. A card
can go a year between statements. Before reporting a gap as missing,
check the issuer's own list, including the previous year's, for
whether a statement exists at all. The per-issuer `cadence` note
records which cards behave this way.

### Step 2 — Drive the browser (per issuer)

Before the first issuer, touch a marker file in your scratch directory.
Every check of `~/Downloads` below is
`find ~/Downloads -maxdepth 1 -type f -newer <marker>`.

1. Call `tabs_context_mcp` first to see the user's current tabs. Only
   reuse a tab if the user asks; otherwise open a new one.
2. **Use a fresh tab for each issuer.** Sending a tab that was just on
   one bank to another bank's site has tripped a fraud filter: a block
   page appeared before the login form. A fresh tab loaded normally.
   Keep finished tabs open until the end. Closing the selected tab can
   detach the extension's tab group, after which calls fail with "not
   in the same group" until you call `tabs_context_mcp` again.
3. Read the issuer's **navigation profile** from
   `$WORKSPACE/config/cc/issuers.json` (login URL, the path to the
   "Statements & Documents" page, last4 reliability, and the
   PDF-password rule — see *Per-issuer navigation notes* below).
4. Navigate to the issuer login page. If not already authenticated,
   **pause and ask the user to log in and complete MFA** in the
   browser, then resume. The extension asks permission per domain,
   and login and PDF hosts are often separate domains from the main
   site. A "Permission denied" error means the user must allow the new
   domain. If the standard login page is broken, the user may sign in
   through another entry point, such as a co-branded partner site.
   Record that in the profile.
5. Navigate to the statements/documents page. Use **semantic**
   navigation (`read_page` / `find` for link text like "Statements",
   "Documents", "Download PDF") rather than brittle hard-coded
   selectors — issuer layouts change a few times a year.
6. Enumerate the available statement periods; cross-reference the gap
   list from Step 1. For each **missing** period, trigger the PDF
   download. Download one first and confirm it before doing the rest,
   so a surprise costs one click, not five.
7. **Confirm each file landed. The file is the only success signal.**
   Check `~/Downloads` against the marker for non-zero bytes and a
   `%PDF` header. A click that "succeeded" proves nothing.

> **Avoid dialogs.** Do not trigger JavaScript `alert`/`confirm`
> dialogs — they freeze the extension. If a "Delete"/destructive
> control is near the download link, navigate around it. Leave cookie
> banners alone unless they block the page; accepting one is the
> user's decision.

#### When a click produces no file

- **Look for a two-step control.** Many issuers hide the real link in
  an overlay or menu that the first click opens. A statement tile may
  reveal separate view and download links, or a download icon may open
  a small format menu. Take a screenshot after the first click and
  look for it before retrying. The accessibility
  tree may list the inner link while it is still hidden, and clicking
  its ref then only opens the overlay.
- **An empty network log is not proof of failure.** Some sites build
  the PDF inside the page (a `data:` or blob URL), so no file request
  ever appears even when the download works. Check `~/Downloads`.
- **Never use Chrome's PDF viewer download button.** When a link opens
  the PDF in a viewer tab, that button opens a native Save dialog the
  agent can neither see nor operate. Save by script in the viewer tab
  instead: `fetch(location.href, {credentials: 'include'})`, require
  `r.ok` and a PDF content type, then click an
  `<a download="<name>.pdf">` pointing at the blob. This re-reads the
  same document in the user's session, saves straight to
  `~/Downloads`, and only works if the URL is reusable, so check the
  status. Tell the user before the first scripted save of a session.
  Close each viewer tab afterward.
- **Stop after 2–3 tries at the same action** and ask the user to
  click once by hand. That one click tells you whether the site
  rejects automated input or is simply broken.

### Step 3 — Normalize into the inbox

Move each downloaded PDF into the staging inbox **with its original /
issuer-given filename unchanged**, using `mv -n`:

```
$WORKSPACE/cc/_inbox/
```

- **Names collide across issuers.** Several issuers name files by the
  bare closing date (`YYYY-MM-DD.pdf`), so two cards can produce the
  same name. `mv -n` refuses to overwrite, so a collision fails loudly
  instead of silently replacing a statement.
- **Rename only when the name carries no meaning.** That means an
  opaque token from a viewer's Save dialog, or a file you saved by
  script. Use a raw descriptive form such as
  `<issuer-hint>-<last4>-<closing-date>.pdf`, never the canonical
  import name.

- `_inbox/` lives under `$WORKSPACE` (never the repo tree) and holds
  **only raw, un-imported PDFs** — no sidecars, so `housebook-cc ingest`
  (which walks `cc/` for `*.json`) never picks it up.
- It is transient: the import SOP **moves** each PDF out of `_inbox/`
  into `cc/<YYYY>/` and empties it. A non-empty `_inbox/` means
  "imported pending."

**Encrypted PDFs.** `pdfinfo` reports `Encrypted: yes` even for PDFs
that carry only an *owner* password, which restricts editing but not
opening. Those extract with `pdftotext` without any password and need
no decryption, so test extraction first
(`pdftotext -layout f.pdf - | wc -c`). Only a PDF that needs a *user*
password to open must be decrypted (`qpdf --password=<pw> --decrypt
in.pdf out.pdf`), and only with a rule recorded in the issuer profile.
If no rule is recorded, leave the file encrypted and flag it for the
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
quirks, under a per-issuer `navigation` block. Read it before driving
an issuer, and rewrite it after any run that found it stale. The
loaders read only `name` and `aliases`, so the block is free-form
text for the agent. Shape, with fictitious values:

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
        "mfa": "Who signs in, where they land, shared logins",
        "statements_path": "Clicks from landing page to the statement list",
        "download": "The exact control, any two-step menu, the saved filename",
        "pdf_password_rule": "None / owner-only / the user-password rule",
        "cadence": "Closing day; whether inactive months are skipped"
      }
    }
  ]
}
```

This file is **live workspace config**, never committed. A
`config/cc/issuers.example.json` template may document the *shape* of
the `navigation` block with fictitious values only.
