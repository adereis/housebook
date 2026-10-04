# Document Acquisition SOP (Phase 0 — Browser-Drive)

## Purpose

This SOP covers what every **acquisition** run shares, whatever the
source: driving the user's own Chrome (via the `claude-in-chrome`
tools) to log into a site and download documents the ledger is
missing. Acquisition is **Phase 0**, the step before import. Its only
output is what a human produces by downloading by hand: **raw,
un-renamed files on local disk**, staged in the source's inbox.

A per-source SOP says what to fetch, where to stage it, and where
that source keeps its per-site navigation notes. Read it alongside
this one:

| Source | Per-source SOP | Fetches | Inbox |
|--------|----------------|---------|-------|
| Credit cards | `prompts/cc/acquire.md` | Statement PDFs | `$WORKSPACE/cc/_inbox/` |
| HSA | `prompts/hsa/acquire.md` | Insurer EOB PDFs | `$WORKSPACE/hsa/_inbox/` |

```
[Phase 0: acquire]  THIS SOP + the per-source SOP → raw files in <source>/_inbox/
        |
        v
[Phase 1: import]   prompts/import.md → the source's import SOP
        |
        v
[Phase 2: ingest]   housebook-<source> ingest — UNVERIFIED rows
```

## CRITICAL: Mode of operation — attended, headful, your own session

This SOP runs **only** in attended, human-in-the-loop mode, and it is
**user-initiated** (the user explicitly asks to fetch documents). It
is NOT a cron job and MUST NOT be run unattended.

- **Headful, real Chrome, the user's own profile, the user's home IP.**
  Large institutions fingerprint browsers and flag headless
  automation, so this is the configuration that reliably gets
  through. Reuse the user's existing logged-in session — do **not**
  open a fresh headless context.
- **The human completes the login and MFA.** Some sites send a
  one-time code on every login, with no way to remember the device.
  When a login or step-up challenge appears, **pause and ask the user
  to complete it**, then continue. Never attempt to read, guess, or
  automate the OTP.
- **No credentials are stored, ever.** Acquisition relies on the
  user's live browser session. Do not save usernames, passwords, OTP
  secrets, or a separate cookie jar to disk, a sidecar, or the DB.
- **ToS awareness.** Some sites' terms restrict automated or agent
  access. For example, Chase's public Digital Services Agreement
  names AI agents explicitly: it holds the user responsible for any
  agent they give access, and says an agent must identify itself as
  one. Whether to drive a given site is the user's call, made per
  site, so raise it before the first run. Keeping the run attended,
  low-frequency (monthly), and inside the user's own session limits
  the exposure. If the user is uncomfortable with a site, fetch from
  it manually instead.

## Hard invariants — what the acquirer MUST NOT do

The acquirer's **sole output** is raw files in the source's inbox. To
keep the dedup contract and the deterministic core intact, it must
NOT:

1. **Rename** files to the source's canonical name — that is the
   import SOP's job, and for most sources the canonical name is part
   of the dedup key.
2. **Author sidecars** — the v1 envelope is built by the import SOP.
3. **Write into the source's year directories** directly, or touch
   the DB, `processed_files`, or any source table — all downstream of
   import.
4. **Run `housebook-<source> ingest`, `housebook-audit`, or
   `housebook-sync`** — those are separate, later, user-gated steps.

If you find yourself doing any of the above inside Phase 0, stop: you
have crossed out of acquisition. When the user has asked for the
whole flow, finish acquisition first, then follow the import SOP and
its gates.

## Driving the browser (per site)

Before the first site, touch a marker file in your scratch directory.
Every check of `~/Downloads` below is
`find ~/Downloads -maxdepth 1 -type f -newer <marker>`.

1. Call `tabs_context_mcp` first to see the user's current tabs. Only
   reuse a tab if the user asks; otherwise open a new one.
2. **Use a fresh tab for each site.** Sending a tab that was just on
   one bank to another bank's site has tripped a fraud filter: a
   block page appeared before the login form. A fresh tab loaded
   normally. Keep finished tabs open until the end. Closing the
   selected tab can detach the extension's tab group, after which
   calls fail with "not in the same group" until you call
   `tabs_context_mcp` again.
3. Read the site's **navigation profile** from the workspace config
   the per-source SOP names (login entry point, the path to the
   document list, the download control, PDF-password rule).
4. Navigate to the login page. If not already authenticated, **pause
   and ask the user to log in and complete MFA** in the browser, then
   resume. The extension asks permission per domain, and login and
   PDF hosts are often separate domains from the main site. A
   "Permission denied" error means the user must allow the new
   domain. If the standard login page is broken, the user may sign in
   through another entry point, such as a co-branded partner site.
   Record that in the profile.
5. Navigate to the document list. Use **semantic** navigation
   (`read_page` / `find` for link text) rather than brittle
   hard-coded selectors — site layouts change a few times a year.
   Filter panels often hold changes until an explicit **Apply**
   control is clicked, and long lists often load more rows only as
   you scroll. Before trusting a list, compare its row count with the
   total the page states.
6. Enumerate the available documents and cross-reference the gap
   list from the per-source SOP. Download one first and confirm it
   before doing the rest, so a surprise costs one click, not twenty.
7. **Confirm each file landed. The file is the only success signal.**
   Check `~/Downloads` against the marker for non-zero bytes and a
   `%PDF` header. A click that "succeeded" proves nothing, and
   neither does a page-side script that reports success.

> **Avoid dialogs.** Do not trigger JavaScript `alert`/`confirm`
> dialogs — they freeze the extension. If a "Delete"/destructive
> control is near the download link, navigate around it. Leave cookie
> banners alone unless they block the page; accepting one is the
> user's decision.

### When a click produces no file

- **Look for a two-step control.** Many sites hide the real link in
  an overlay or menu that the first click opens. A document tile may
  reveal separate view and download links, or a download icon may
  open a small format menu. Take a screenshot after the first click
  and look for it before retrying. The accessibility tree may list
  the inner link while it is still hidden, and clicking its ref then
  only opens the overlay.
- **An empty network log is not proof of failure.** Some sites build
  the PDF inside the page (a `data:` or blob URL), so no file request
  ever appears even when the download works. Check `~/Downloads`.
- **Never use Chrome's PDF viewer download button.** When a link
  opens the PDF in a viewer tab, that button opens a native Save
  dialog the agent can neither see nor operate. Save by script
  instead: `fetch(url, {credentials: 'include'})`, require `r.ok` and
  a PDF content type, then click an `<a download="<name>.pdf">`
  pointing at the blob. This re-reads the same document in the
  user's session and saves straight to `~/Downloads`. It only works
  if the URL is reusable, so check the status. Tell the user before
  the first scripted save of a session. Close each viewer tab
  afterward.
- **Chrome drops repeated scripted downloads silently.** The first
  download a page starts by script goes through. Later ones are
  blocked by Chrome's "automatic downloads" guard, with no error in
  the page, and the script still reports success. Once a site is
  blocked, even a fresh page load stays blocked. Ask the user to
  allow the site, either from the blocked-download icon at the right
  end of the address bar or under
  `chrome://settings/content/automaticDownloads`. That setting is
  theirs to change, never yours. After each batch, count the files
  on disk and re-run only the missing ones.
- **Stop after 2–3 tries at the same action** and ask the user to
  click once by hand. That one click tells you whether the site
  rejects automated input or is simply broken.

### Links inside web components

Some sites render each link as a custom element whose real `<a>`
lives in the element's shadow root, with the label slotted in from
outside. Three consequences:

- A search for anchors by their text finds nothing, because the
  `<a>` itself holds only a `<slot>`. Find the custom element by its
  label instead, then read `element.shadowRoot.querySelector('a')`.
- A scripted `.click()` on the custom element may do nothing, while a
  real click opens the document. Reading the `href` and fetching it
  avoids the question.
- **Pair each link with its row structurally.** Read the URL from
  inside the same row or card whose text names the document, and
  name the saved file from that text. Never infer which document a
  file is from the order of clicks or downloads. Order-based pairing
  is how a whole run of files ends up shifted by one.

### Tool limits that shape a run

- **Output from the JavaScript tool is filtered and truncated.**
  Values that look like query strings or tokens come back as
  `[BLOCKED: …]`, and long output is cut after about a kilobyte.
  Keep signed URLs in page variables (`window.__x`) and return only
  compact summaries. Do comparisons inside the page, passing the
  reference data in, rather than printing a long list to compare
  locally.
- **Define helper functions with a direct `javascript_tool` call.**
  Escaping a script inside a `browser_batch` JSON payload has broken
  its regular expressions without an error.
- **Sessions expire.** If the user steps away, the next action lands
  on the login page, and every page variable is gone. Pause for the
  login again, then rebuild your page state.

## Staging in the inbox

Move each downloaded file into the source's inbox **with its
original, site-given filename unchanged**, using `mv -n`.

- **Names collide across sites and documents.** Many sites name every
  file the same way (`YYYY-MM-DD.pdf`, `contents.pdf`). `mv -n`
  refuses to overwrite, so a collision fails loudly instead of
  silently replacing a document.
- **Rename only when the name carries no meaning.** That means an
  opaque token from a viewer's Save dialog, or a file you saved by
  script. Use a raw descriptive form built from the row's own text
  (`<site-hint>-<doc-kind>-<date>-<distinguisher>.pdf`), never the
  canonical import name.
- The inbox lives under `$WORKSPACE` (never the repo tree), holds
  **only raw, un-imported files**, and is transient: the import SOP
  moves each file out and empties it. A non-empty inbox means
  "imported pending."

**Encrypted PDFs.** `pdfinfo` reports `Encrypted: yes` even for PDFs
that carry only an *owner* password, which restricts editing but not
opening. Those extract with `pdftotext` without any password and need
no decryption, so test extraction first
(`pdftotext -layout f.pdf - | wc -c`). Only a PDF that needs a *user*
password to open must be decrypted (`qpdf --password=<pw> --decrypt
in.pdf out.pdf`), and only with a rule recorded in the site's
profile. If no rule is recorded, leave the file encrypted and flag it
for the user rather than guessing.

## Hand off

After every in-scope site is done:

1. Present a summary in conversation: per site, what was downloaded,
   what was skipped as already on file, and the inbox path.
2. Close the tabs you opened.
3. **STOP**, unless the user asked for the whole flow. Then follow
   `prompts/import.md` on the inbox, with all of its gates.

## Failure handling — fail loudly

Per the project's no-silent-failure rule:

- **Downloaded nothing where new documents were expected** → **warn**,
  don't shrug. State which site, what you saw on the page, and
  whether login, MFA, or layout was the blocker. Do not fabricate or
  infer document contents.
- **Login/MFA could not be completed** → pause and ask the user; do
  not retry credentials automatically.
- **Page layout unrecognizable / selectors all miss** → stop after
  2–3 semantic attempts (per the browser-automation rabbit-hole
  rule), tell the user what changed, and offer to fetch manually.

## Escalation rules — when to ASK rather than proceed

| Condition | Why |
|---|---|
| Login requires MFA | Human must complete it; never automate OTP |
| A CAPTCHA / interactive challenge appears | Cannot/should not be auto-solved; ask the user |
| Repeated failed logins risk a fraud hold/lockout | Stop — protect the user's account; ask before retrying |
| A site's ToS makes the user uncomfortable | Fetch from that site manually instead |
| Chrome blocks repeated downloads | Allowing the site is the user's browser setting |
| Encrypted PDF with unknown password rule | Don't guess; flag for the user |
| Downloaded count ≠ expected gap count | Surface the discrepancy before handing off |

## Per-site navigation notes (living)

Keep site-specific navigation knowledge in live workspace config,
never in the repo: the per-source SOP names the file. Read the notes
before driving a site, and rewrite them after any run that found them
stale. Every source uses the same free-form shape, with fictitious
values here:

```jsonc
"navigation": {
  "verified": "2030-01-15",
  "login_url": "https://portal.example/login",
  "mfa": "Who signs in, where they land, shared logins",
  "list_path": "Clicks or URL from the landing page to the document list",
  "download": "The exact control, any two-step menu, the saved filename",
  "pdf_password_rule": "None / owner-only / the user-password rule",
  "cadence": "When new documents appear; which gaps are expected"
}
```
