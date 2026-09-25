# Housebook: Project & Development Guidelines

## 🤖 Agent Standard Operating Procedure (SOP): Workspace & Sync Management

Because this repository strictly enforces a **Stateless Repo Architecture**, all sensitive data (`hsa/`, `cc/`, `tax/`, `amazon/`, `data/`, `config/`) lives in an external workspace, typically synced to Google Drive via `rclone`.

When acting as an AI assistant on this repository, **you must follow this SOP:**

> **`housebook-sync` is a high-impact, infrequent operation.** Do not run it as a routine
> step or "just to be safe." Only sync when the user explicitly asks, or when a specific
> task clearly requires bringing in new remote data. When in doubt, ask first.

1. **Initialization:** Whenever the user asks to process new files, run the pipeline, or update data, verify that `HOUSEBOOK_WORKSPACE_DIR` is set in the `.env` file and that the directory exists locally.
2. **Pre-Flight Check (Status only):** Run `housebook-sync status` to view the sync dashboard — it shows database change counters (local/anchor/remote), pending push/pull file counts, recent sync history with machine names, and a safety recommendation. Only proceed to pull if (a) the user explicitly requested a sync, or (b) there is a clear reason to believe the remote has newer data needed for the current task.
3. **Pulling safely:** `housebook-sync pull` uses `rclone sync` to propagate remote renames and deletions. `data/backups/` is protected via pull excludes. Do not pull unless there is a concrete reason.
4. **Import:** The user provides file path(s) from any location. Follow the unified import SOP (`prompts/import.md`) to determine the source type, rename, create sidecars, and move files to `$WORKSPACE/<source>/YYYY/`. For Amazon zip exports, use `housebook-amazon import <path-to-zip> --profile <name>`. After import + user review, run `housebook-<source> ingest`.
5. **Safety Check (Dry Run):** After successful execution, run `housebook-sync push --dry-run` and read the output.
6. **Conflict Resolution & Push:**
    - If the push will only update the SQLite database, JSON configs, and generated PDFs/reports: run `housebook-sync push` autonomously.
    - If the push intends to **delete** historical raw data or overwrite a file you suspect the user just modified elsewhere: **STOP and ask the user for permission** to proceed with the push.

## Agent Workflow: Import → Ingest → Audit → Push

The standard workflow for processing new financial data:

```
# 1. Sync workspace only when explicitly requested or clearly needed
housebook-sync status          # Dashboard: counters, history, recommendation
housebook-sync pull            # Only if remote has new data you need
housebook-sync push --dry-run  # Preview before pushing

# 2. Coverage check — what statements are missing?
housebook-ingest list --latest          # Latest file per source
housebook-ingest list --source Amex     # One source in detail
housebook-ingest list                   # All files with gap detection

# 3. Import + ingest (see prompts/import.md for the unified SOP)
# User provides files from any path (e.g., ~/Downloads)
# Agent determines source type from content and follows per-source rules
housebook-cc ingest --dry-run           # After import pass
housebook-cc ingest
housebook-amazon import <zip> --profile <name>  # Amazon zip
housebook-amazon ingest

# 4. Audit: review and verify new transactions
housebook-audit apply-rules             # Best-effort category guesses
housebook-audit detect-trips --json     # Find trip candidates
housebook-audit calibrate                    # Understand existing patterns
housebook-audit pending                      # See what needs review
housebook-audit pending --json               # Machine-readable with metadata
housebook-audit trips                        # Check trips for assignment
housebook-audit create-trip "Name" --start YYYY-MM-DD --end YYYY-MM-DD \
    --type personal --location "Place"       # Create trip if needed
housebook-audit verify <ids> --category "X"  # Batch-verify transactions
housebook-audit verify <ids> --trip <id>     # Assign to trip
housebook-audit summary                      # Post-audit report

# 4b. Reconcile returns & cancellations (after audit)
housebook-audit link-amazon-refunds --dry-run   # Auto-link Amazon 1:1 by Order ID
housebook-audit link-amazon-refunds              # Apply
housebook-audit link <purchase_id> <refund_id>  # Manual pair (other sources or multi-line)
housebook-audit linked                          # Review all linked pairs
housebook-audit unlink <id>                     # Undo a link

# 4c. Projects (user-initiated; see prompts/projects.md,
#     and prompts/projects_import.md for bulk import from a hand sheet)
housebook-audit match-project                   # Sweep every OPEN project for candidates
housebook-audit assign <ids> --project <id>     # Tag verified rows (no status change)
housebook-audit verify <ids> --project <id>     # Audit + assign fresh rows in one step
housebook-audit add-manual "Workmanship" --amount 9500 \
    --category "Home & Garden" --date YYYY-MM-DD --project <id>  # Off-ledger cost
housebook-audit edit-project <id> --budget N --start … --end …   # Amend a project
housebook-audit projects                        # Open projects: spend vs budget

# 5. Sync back to Drive
housebook-sync
```

`housebook-sync` with no arguments does bidirectional sync: pulls new
inputs from remote, then pushes local DB changes. It uses SQLite's
file header change counter for conflict detection — if the remote DB
was modified since your last sync, it aborts with a clear error
showing which machine last pushed and when (from the sync journal).
Use `housebook-sync pull` / `housebook-sync push` for manual control.

**The workspace has no default.** Every command exits unless
`HOUSEBOOK_WORKSPACE_DIR` names an existing directory
(`config/settings.py::resolve_workspace`). The old fallback to the repo
root put real data inside the checkout. `./test.sh` points each run at a
fresh empty workspace, so tests can never reach the real ledger, and
`housebook-demo-seed` pins its own `demo-workspace/`.

`housebook-sync` also refuses a workspace that contains the code
checkout (`.` or an ancestor such as `$HOME`). `rclone sync` mirrors
its source, so a pull there would delete `src/` and `.git/`, and a
push would upload the repo with `.env`. The root `.gitignore` ignores
every workspace subtree (`/cc/`, `/hsa/`, `/tax/`, `/amazon/`,
`/config/**`) for the same misconfiguration.

Each sync operation is recorded in a **sync journal**
(`data/finance.sync_journal`) that tracks timestamp, direction,
machine hostname, and file count. The journal is synced to remote,
so `housebook-sync status` on any machine shows cross-machine history.

The `housebook-audit` CLI enforces the transaction status lifecycle
automatically: `verify` sets `AGENT_VERIFIED` and clears `needs_review`.
No raw SQL is needed for standard audit operations.

See `prompts/monthly_audit.md` for the full audit SOP with categorization
rules, edge cases, and user-specific nuances.

### Return & Cancellation Reconciliation

After the monthly audit, run `prompts/reconcile_returns.md` to pair
refunds with their original purchases. Linked transactions are hidden
from spending views (both the charge and the refund) but remain in the
database. The `linked_transaction_id` column creates a bidirectional
link between the two sides.

**Only link full refunds** where amounts cancel exactly. Partial refunds,
unmatched negatives, and ambiguous credits must be flagged for user
review — there is no blanket policy for these yet.

### Projects (user-initiated grouping)

Projects are the **supervised-retrieval** counterpart to trip detection.
Where `detect-trips` *discovers* clusters unprompted, a project is
**declared by the user** ("we're renovating the master bath"); the agent
stores its matching criteria (`match_keywords`, `match_categories`) on the
`projects` row, and `match-project` *retrieves* candidate transactions to
score against that target. Full SOP: `prompts/projects.md`.

Key invariants (note how they differ from the filters below):

- **Projects GROUP, they do not EXCLUDE.** Unlike `linked_transaction_id`
  / `RECONCILED` / `CC Payment`, a `project_id` never hides a transaction
  from spending views — a renovation is real spending. Projects add a
  *reporting/budget* dimension (`project-summary` nets signed amounts over
  the same spending-view predicate so linked returns net out).
- **Off-ledger costs go through `manual_expenses`, not the source ledger.**
  Cash/check craftsmanship and lump material totals are not on any
  statement; record them with `add-manual ... --project <id>` (one-time
  manual expenses carry a `project_id`). `project-summary` and `projects`
  roll project-linked manual expenses into the total alongside
  transactions — so a project total = assigned transactions (net) +
  linked manual expenses. Never hand-insert one-offs into `transactions`;
  the source ledger stays immutable (see `migrations/002_manual_expenses`).
- **`match-project` is high-signal.** A candidate must hit a keyword OR a
  configured category to surface; amount and known-vendor only boost an
  already-qualifying row. With no id it sweeps **every `open` project**;
  pass an id to scope to one. It echoes each project's window + criteria.
- **Necessary, not sufficient.** Same rule as trip assignment: a match
  inside the window is a signal, not a verdict — confirm the charge truly
  belongs to the project before assigning. The matcher proposes; the agent
  decides.
- **Assign the source-of-truth row, not the bank twin.** An Amazon purchase
  is in the DB twice: the verified `Amazon` CSV row (counted) and its
  `RECONCILED` bank twin (hidden). Tag the `Amazon` row — tagging the
  `RECONCILED` twin contributes **$0** to the project total while looking
  assigned. Likewise a returned item: assign the kept row, not the
  refunded one.
- **`assign` vs `verify` for grouping.** Use `assign <ids> --project N` to
  tag already-audited rows — it never rewrites `status`/`needs_review`
  (so it can't downgrade a `USER_VERIFIED` row) and needs no `--force`.
  `verify --project` is for the combined *audit-and-assign* step on fresh
  `UNVERIFIED` rows.
- **Retrospective import** (a hand-tracked sheet of a finished project) is a
  distinct workflow — see `prompts/projects_import.md`. Its core trap: sheet
  amounts are tax-inclusive/aggregated/estimated, so **match on product
  description, not amount**, and expect totals to fall short where in-store
  Lowes/Home-Depot statements were never ingested (leave those gaps honest,
  never fabricate).
- **Lifecycle:** `open` (re-scanned each cycle) → `close-project`
  (`--freeze-end` pins `end_date` to the last assigned tx). Closed
  projects leave the sweep but stay reportable.

The seed criteria per project type live in
`$WORKSPACE/config/project_types.json` (template
`config/project_types.example.json`); the agent bootstraps a new project
from there, then refines.

### Spending View Filters

**One canonical definition:** `core/spending.py::spend_filter(alias)`.
Both the dashboard projection (`core/dashboard.py`) and
`housebook-audit` build every spending query from it — never hand-write
the predicate again. It had been copied out by hand in nine places and
the copies had already drifted (four omitted the zero-amount clause, so
trip totals disagreed with the transaction lists that fed them).

`core/dashboard.py` also owns the shared read model for `/api/data` and
`/api/spending/data`: transaction filtering, recurring manual-expense
expansion, categories, and trip summaries. Keep the HTTP handlers in
`app.py` as parameter/status translation rather than duplicating that
projection there.

The spending views exclude transactions on four criteria:

| Filter | Purpose |
|--------|---------|
| `linked_transaction_id IS NOT NULL` | Hide linked purchase↔refund pairs |
| `status = 'RECONCILED'` | Hide bank-side Amazon duplicates (CSV stays visible as source of truth) |
| `category = 'CC Payment'` | Hide credit card payments (balance transfers) |
| `amount = 0` | Drop no-op rows (e.g. $0 gift redemptions) |

The category and status tests are written NULL-safe
(`category IS NULL OR category != …`). A bare `!=` evaluates to NULL —
not TRUE — for a NULL column, so such rows silently vanished from every
view and total. Both columns are nullable in the schema and the
re-ingest runbooks direct raw-SQL repair, so nothing prevents one.

**Refunds and credits are NOT filtered.** Negative amounts from
returns, cashback, and standalone credits remain visible and reduce
the spending total — this reflects actual net spending.

CC payments are excluded from spending views only once their
category is `CC Payment`. The CC *ingestor* does NOT categorize — it
is constructed with no `Intelligence` and writes the sidecar's
category or `Uncategorized`. **Consequence:** freshly-ingested,
not-yet-audited CC rows are `Uncategorized`, so the large payment
credits are not yet excluded from spending views — a just-ingested
card can show a misleading (even negative) spending total until it
is audited.

**How `CC Payment` actually gets set — and two traps.** It is *not*
applied by `CC_PAYMENT_PATTERNS` (that engine lives in
`core/intelligence.py` and is only consulted by the *ingestors'*
`Intelligence`, e.g. Amazon, which categorizes at ingest — the CC
ingestor has no `Intelligence`). The audit-time tool
`housebook-audit apply-rules` does **not** use that engine either: it
applies `rules.json` keyword→category mappings, and only to rows
whose `date >= one_year_ago` (a hard 365-day window) whose category
is still generic. So `CC Payment` is set during audit *only if*
`rules.json` has a matching keyword under a `CC Payment` category
**and** the row is within the last year. Traps:
- **365-day window:** a historical backfill (e.g. importing years of
  old statements) is almost entirely *outside* `apply-rules`' scope —
  it will categorize almost nothing. Tag those rows directly with
  `housebook-audit verify <ids> --category "CC Payment"`.
- **Wording / category mismatch:** the live `rules.json` payment
  keywords (`AUTOPAY`, `PAYMENT RECEIVED`, `PYMT`, …) map to
  `Transfers & Refunds` (which is *not* a spending-view exclusion)
  and do **not** match Chase's `Payment Thank You-Mobile` or
  `AUTOMATIC PAYMENT - THANK YOU`. Such credits stay `Uncategorized`
  and visible until manually verified as `CC Payment`.

Once categorized, `CC Payment` rows stay in the DB but are excluded
from all spending views. (Amazon, by contrast, categorizes at
ingest — though note that its ingest-time categorization was
**silently dead** until 2026-07: `housebook-amazon` constructed
`Intelligence` with the wrong argument and a bare `except` swallowed
the crash, so every Amazon row fell back to its default category. Rows
ingested before that fix are still `UNVERIFIED` and get their real
category at audit time, so no stored data needs repair — but don't
read an old Amazon row's category as evidence the engine agreed with
it.)

## Module Guide

Per-source detail lives in a module-level `AGENTS.md` next to the
code, with a one-line `CLAUDE.md` stub beside it that does
`@AGENTS.md` (mirroring this repo's root). Claude Code **auto-loads a
directory's `CLAUDE.md` on-demand** the moment you read any file in
that directory, and resolves its `@import` — so the module's deep
reference reaches context exactly when you're working that source and
stays out of it otherwise. The `AGENTS.md` holds the content so other
agents (e.g. Codex, Gemini) that read `AGENTS.md` directly get it too.
(Verified empirically: a bare `AGENTS.md` is *not* auto-loaded — only
`CLAUDE.md` is — and HTML comments are stripped from injected files.)
This root file is the always-loaded router: cross-cutting concerns
(the audit lifecycle, spending-view filters, trip detection, sync)
live here.

| Source | Module doc | CLI | Import SOP |
|--------|-----------|-----|------------|
| Credit cards | [`src/housebook/cc/AGENTS.md`](src/housebook/cc/AGENTS.md) | `housebook-cc` | `prompts/cc/import.md` |
| Amazon | [`src/housebook/amazon/AGENTS.md`](src/housebook/amazon/AGENTS.md) | `housebook-amazon` | (deterministic; no AI import) |
| Tax | [`src/housebook/tax/AGENTS.md`](src/housebook/tax/AGENTS.md) | `housebook-tax` | `prompts/tax/import.md` |
| HSA Shoebox | [`src/housebook/hsa/AGENTS.md`](src/housebook/hsa/AGENTS.md) | `housebook-hsa` | `prompts/hsa/import.md` |

- **Credit cards** — PDF statements → sidecars → `transactions`.
  Validators in `cc/schema.py` guard against year-inference bugs.
  The CC ingestor does **not** categorize (see *Spending View
  Filters* below for the `CC Payment` consequence).
- **Amazon** — already-structured CSV exports are the **source of
  truth**; the bank card that mirrors them is normally not ingested.
  Four CSVs ingested per profile; Order ID persisted in metadata.
  Bank-reconcile is an exceptional path — see
  `prompts/amazon/reconcile_cleanup.md`.
- **Tax** — W2/1099/1098 and the Brazilian tax report →
  `tax_documents`. Separate CLI and table from expenses.
- **HSA Shoebox** — an IRS-audit-proof ledger of out-of-pocket
  medical expenses with evidence levels, kept in its own tables and
  CLI. Completely separate from expenses and tax.

## Unified ingestion — all sources migrated

All four sources (HSA, CC, Tax, Amazon) follow the unified
import→ingest pattern:

```
$WORKSPACE/<source>/
  YYYY/ or      ← imported files + envelope sidecars
  <profile>/       (Amazon uses profile dirs instead of year dirs)

$WORKSPACE/config/<source>/...      ← LIVE per-source config (NOT in repo)
prompts/<source>/...                ← per-source SOPs (import, audit, …)
prompts/import.md                   ← unified entry point
prompts/reingest.md                 ← repair/re-ingest runbook (correcting historical data)
src/housebook/<source>/AGENTS.md   ← per-source module doc (content; read by Codex, Gemini et al.)
src/housebook/<source>/CLAUDE.md   ← one-line `@AGENTS.md` stub (Claude auto-loads on-demand)
src/housebook/<source>/    ← isolated module
```

> **Config lives in the workspace, not the repo.** The live config the
> CLIs read is `$WORKSPACE/config/<source>/*.json` (e.g.
> `$WORKSPACE/config/cc/issuers.json`). The repo's `config/` holds
> **only** `*.example.json` templates — don't expect real issuer/alias
> data there.

> **Correcting historical data?** See `prompts/reingest.md`. Clearing a
> source's `processed_files` rows and re-running its ingest is a safe,
> additive repair (file-level idempotency + multiplicity/content-aware
> row dedup mean it only backfills missing rows). That runbook also
> covers `processed_files` orphan-vs-untracked hygiene after renames
> and the amount-normalization trap when verifying faithfulness.

### File-level transaction boundary

Every source file is ingested atomically. `Database.transaction()` uses
`BEGIN IMMEDIATE` before the `processed_files` check, so concurrent
processes cannot both claim the same file. All source rows, provenance,
recoverable `ingestion_errors`, and the processed-file marker commit as
one unit. An unexpected exception or interruption rolls back the entire
file; a retry starts from a clean ledger. Keep new ingestors inside this
boundary rather than relying on row-level dedup for crash recovery.

## TODO — open decisions & deferred work

Raised by the 2026-07 architecture review and recorded for future
discussion. Cross-cutting items are described here; module-specific
ones live in the module doc so they surface when you work that module.

The accepted direction for multi-year tax evidence, authenticated LAN
access, and encrypted storage lives in
`docs/architecture/tax-security-roadmap.md`. Read it before changing
tax transcript schemas, authentication, sync conflict detection, or
workspace encryption.

**Web UI trust boundary.** Uvicorn refuses non-loopback binds.
`TrustedHostMiddleware` blocks DNS rebinding with a loopback-only
allowlist by default. In the default `disabled` auth mode, unsafe
requests that a browser marks as cross-origin (`Origin` other than the
dashboard's own, or `Sec-Fetch-Site` other than `same-origin`/`none`)
get 403, so a page on another site cannot drive the local dashboard;
curl and other non-browser clients are unaffected. For LAN access, `HOUSEBOOK_AUTH_MODE`
must be `proxy`: every path then requires both the identity header and
private credential written by the loopback authenticating proxy. All
POST/PUT/PATCH/DELETE requests additionally require an exact Origin
from `HOUSEBOOK_ALLOWED_ORIGINS`; there is no CORS middleware.
The proxy must strip client-supplied identity headers, authenticate all
paths (including static assets and health), and overwrite the private
credential. Do not treat a routable Uvicorn bind as a substitute.
An explicit local bypass may coexist with proxy mode, but only when both
the transport peer and requested Host are loopback; unsafe requests still
pass the exact-Origin check. The LAN hostname never qualifies for bypass,
even when requested from the server itself.
The privacy-safe Caddy/OAuth2 Proxy examples and live acceptance checklist
are in `deploy/lan/`; real hostnames, Gmail addresses, and credentials
belong only in the external `/etc/housebook` copies.

**Module-specific TODOs.** `tax/AGENTS.md` records three confirmed
modeling gaps (capital-loss carryover, NIIT base, unused 1098
real-estate taxes). The scope question is settled: the engine will be
multi-year and reconciled against filed evidence, one explicit
year/status/jurisdiction parameter set at a time.

### In-app help

The user-facing guide is a drawer, not a page: `templates/_help_drawer.html`,
included by `base.html` on every page and slid in from the right by the
header's Help button. It has one section per feature, in plain language, with
no CLI detail beyond what a user would ask the agent for. When a change
alters what a user sees or what a total includes, update the matching
section in the same commit.

- **Topics:** `app.py::HELP_SECTIONS` lists them in order. It is a Jinja
  global, so every template can render the topic bar. Section ids are
  `help-<anchor>` so they cannot collide with ids on the page.
- **Opening topic:** the drawer opens at `help_topic`, which defaults to
  the page's `active_module`. A module's anchor must therefore equal its
  name (`spending`, `tax`, `hsa`). A route can pass `help_topic` to
  override it; the trip page opens at `trips`.
- **Outside Vue, native `<dialog>`:** the drawer is static markup placed
  outside `#app`, driven by a few lines of plain JS in `base.html`,
  because each page mounts its own Vue app. `showModal()` supplies focus
  trapping, Escape, and focus return to the button. Closing adds a
  `closing` class and calls `close()` on `animationend`, so the drawer
  slides out as well as in. Reduced-motion users get no animation, and
  the drawer closes immediately.

### Dashboard asset build

Dashboard runtime assets are local and committed under
`src/housebook/static/`; production pages must not depend on a
CDN. Versions and integrity hashes are pinned by `package.json` and
`package-lock.json`. The generated CSS/vendor JavaScript files are
package data, not hand-edited source.

After changing template classes or upgrading a pinned UI dependency:

```bash
npm ci --ignore-scripts
.venv/bin/housebook-build-assets
./test.sh
```

`housebook-build-assets` compiles Tailwind from
`static/src/dashboard.css`, copies only the required browser bundles,
and refreshes their committed license texts. Node dependencies stay
untracked in `node_modules/`; application users do not need Node because
the built assets ship with the Python package.

## Project Overview
This project is an autonomous financial analysis system designed to be orchestrated by an AI Agent. It ingests credit card statements, Amazon order history, tax documents, and HSA medical receipts through a unified import→sidecar→ingest pipeline. An AI agent imports raw documents (PDFs, XLSX, CSVs) from any location, creating structured JSON sidecars in the workspace; deterministic Python code ingests the sidecars into a SQLite database.

## Architectural Philosophy: Agent-Orchestration
Unlike traditional applications that call AI APIs internally, this system uses the AI Agent (Claude Code, Codex, Gemini CLI, or similar) as the **Control Plane**.

1.  **Dumb Pipes**: Core logic resides in `src/housebook/`. Python scripts handle structured data (XLSX, CSV) and basic PDF text extraction. Everything ingested is marked as `UNVERIFIED`.
2.  **Agent Orchestration**: The AI Agent executes CLI tools (`housebook-cc ingest`, `housebook-audit detect-trips`, `housebook-hsa scan`, etc.), handles errors, and populates configuration files.
3.  **Interactive Audit**: The Agent performs final data correlation and deduplication by applying Standard Operating Procedures (SOPs) found in the `prompts/` directory. SOPs encode domain heuristics learned from past sessions — always read them before acting.
4.  **Proactive Metadata Mining**: During the configuration phase, the Agent proactively reads raw sources to extract metadata (Tax Year, Exchange Rates, Account IDs) before consulting the User.

## Transaction Status Lifecycle

Every transaction has a `status` field that tracks its verification state. This is a
**binding contract** — not a suggestion. Violating it silently skips the mandatory
Agent review step and produces unreliable data.

| Status | Set by | Meaning |
|--------|--------|---------|
| `UNVERIFIED` | Ingestors (scripts) | Best-effort parse; categories may be wrong |
| `AGENT_VERIFIED` | AI Agent via `monthly_audit.md` SOP | Agent has reviewed and confirmed |
| `USER_VERIFIED` | User via web UI | User has manually confirmed or corrected |
| `RECONCILED` | Reconciler | Bank tx matched to Amazon CSV duplicate |

**Hard rules:**
- **Scripts MUST set `UNVERIFIED` only.** No `AI_VERIFIED`, no auto-promotion based
  on confidence level or rule matches. The categorization engine's `(category,
  confidence)` tuple is useful metadata for the Agent to *prioritize* its audit
  (fast-track `"rule"` matches, scrutinize `"guess"` matches), but it must never
  affect status or suppress `needs_review`.
- **`prompts/monthly_audit.md` is mandatory**, not optional. Every ingest cycle must
  be followed by an Agent audit before the data is considered reliable.
- `needs_review = True` on all ingested transactions. The Agent clears this flag
  when it sets `AGENT_VERIFIED`. The user clears it via the UI when they correct a
  transaction (`USER_VERIFIED`).

## Development Standards

### Project-Wide Standards
- **Source Layout**: All core logic MUST remain within the `src/housebook/` package. Never add new scripts to the project root; instead, define them as `console_scripts` in `pyproject.toml`.
- **Module docs**: Per-source detail lives in `src/housebook/<module>/AGENTS.md`; a sibling one-line `CLAUDE.md` stub (`@AGENTS.md`) makes Claude Code auto-load it on-demand whenever you read a file in that module (verified — a bare `AGENTS.md` is not auto-loaded, so the stub is required). Because the doc is in context whenever you edit the module, keep it current as part of the same change. Do not put load-bearing text in HTML comments — they are stripped from injected files.
- **Separation of Concerns**: Core code remains deterministic. No embedded internal AI API calls.
- **Safety**: Sensitive financial data must never be committed. Always use `.example.json` templates.
- **Fictitious Data Only**: All test fixtures, SOP examples, docstrings, and
  committed sample data MUST use **clearly fictitious** names. Use the "Ledger
  Family" vocabulary from `housebook-demo-seed` (Sterling, Penny, Buck, Ally
  Ledger) for person names. Use fictitious providers (Maple Dental, Sunrise
  Home Care, Shield Health, Vault HSA, Acme Corp) — never real local
  providers, employers, or account fragments.
  **Renaming is not enough.** A fixture that keeps a real row's numbers is
  still that real row: its date + amount, an order/claim/reference ID, a
  card last4, or a statement's balances identify the original document as
  surely as a name does. When a bug surfaces on live data, reproduce its
  *shape* with invented values (e.g. "$100 split 33.33/33.33/33.34"), never
  its figures.
- **Leak scan (pre-commit)**: `housebook-leak-scan` compares the git index
  against the live workspace, opened read-only. It reports the PII
  denylist (`$WORKSPACE/config/pii-denylist.txt`), home location and known
  names, real identifiers (order, claim, ticket and bank reference codes),
  card last4 beside card context, a real date + amount within 4 lines,
  and distinctive standalone amounts from medical, tax, balance and
  off-ledger records. The pre-commit hook runs it on every commit; it
  skips cleanly when no workspace is configured. Intentional matches
  (the author's name in `LICENSE`) go in
  `$WORKSPACE/config/leak-scan-allow.txt` as `<path glob> <text>`, never
  in the repo. An invented fixture value that happens to equal a real
  one is simplest to fix by picking another value; allowlist a
  coincidence only when the value must stay. `--rev <commit>` scans any
  commit's tree, which is how to audit history. Names that live only in
  free text (a contractor on a manual expense) cannot be derived: add
  them to the denylist.
- **Imports**: Always use absolute package imports (e.g., `from housebook.core...`).
- **Running venv commands**: Use `.venv/bin/<command>` directly (e.g., `.venv/bin/housebook-audit pending`, `.venv/bin/python3 -c "..."`). Do NOT use `source .venv/bin/activate` — it triggers unnecessary confirmation prompts in AI agent tool harnesses. `./test.sh` handles PATH internally and needs no prefix.
- **Temporary Scripts**: Never create auxiliary or temporary scripts in the project root. Use the system `/tmp` directory or `~/tmp` for transient execution tasks.
- **Environment variables**: Do NOT prefix commands with `source .env` — `direnv` already loads `.env` via `dotenv_if_exists` in `.envrc`, so all env vars (e.g., `HOUSEBOOK_WORKSPACE_DIR`) are automatically available. Using `source` triggers unnecessary safety prompts in AI agent tool harnesses.
- **Wildcard Safety**: When performing cleanup or deletion in the workspace (especially in `cc/YYYY`, `hsa/YYYY`, or `tax/YYYY`), **NEVER use broad wildcards** (e.g., `rm *_*`). Always list the targets first or use extremely specific patterns to avoid deleting historical financial records.

### Git Commit Standards
We follow high-signal semantic commits with strict formatting for readability in CLI tools.
- **Header**: `<type>(<scope>): <subject>` (Subject MUST be < 50 characters).
- **Body**: Wrap lines at **72 characters**. Explain **Why** the change was needed and the **Rational** behind the implementation choices.
- **Bullet Points**: Use for granular lists of **What** changed.
- **Co-Authorship**: Commits implemented by an AI agent MUST include a
  trailing `Co-Authored-By:` line identifying the agent that actually
  authored them — attribute honestly, do not hard-code one vendor.
  Examples: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
  or `Co-Authored-By: Gemini CLI <gemini-cli@google.com>`.

### Testing & Validation
- **Requirement**: Run `./test.sh` before every commit.
- **CI**: `.github/workflows/test.yml` runs `./test.sh` on the oldest
  and newest supported Python, in a plain venv with only
  `pip install -e '.[dev]'`. A local venv uses `--system-site-packages`,
  so a system package can hide an undeclared dependency; CI is where
  that surfaces. Lint and test tools belong in the `dev` extra, runtime
  imports in `dependencies`.

### SOP Design Principles
- **SOPs are living documents**: When a detection or audit session reveals a new heuristic (e.g., "Egencia bills from Scottsdale, AZ but that's not the trip destination"), add it to the relevant SOP immediately. The SOP captures operational judgment that code cannot.
- **User profile complements SOPs**: `config/user_profile.json` holds user-specific facts (e.g., "Uber/Lyft usage is almost exclusively travel-related"). SOPs hold general procedures. Both must be consulted.
- **Deterministic code provides signals, Agent provides judgment**: CLI tools output structured data (candidates, flags). The Agent decides what to confirm, split, merge, or discard. Never auto-commit results without Agent review.

### Operational Guidelines
- **Demo-First Evaluation**: When testing new Agent prompts or SOP changes, prefer using the "Ledger Family" demo workspace (`housebook-demo-seed`) to ensure consistent, non-sensitive results before applying to live data.
- **Strict Determinism (No Hallucination):** Never extrapolate, infer, or "hallucinate" missing data just to make the ledger balance.
 If data is missing from the source statement, it must be recorded as `NULL`. The system is a ledger, not an imagination engine; missing data must fail loudly in the FIFO matching engine so it can be manually addressed.
- **User-Specific Context:** Always consult `config/user_profile.json` (specifically `special_notes`) to understand user-specific financial behaviors, dedicated accounts, or expected data gaps (e.g., dedicated credit cards for specific vendors). Do not spend time investigating expected data omissions documented in this profile.
- **Database Isolation**: NEVER run tests against `finance.db`. Use `tempfile` or `:memory:` SQLite databases. Mock all paths/constants that point to production data.
- **Smart Mocking**: Mock IO (exists, read_excel, open) while exercising primary transformation logic.
- **Trip detection heuristics**: The `detect-trips` location hint is a majority vote over the trailing 2-letter code of each charge, recognizing US states and ISO country codes (FR, GB, BR, etc.). Unambiguous codes decide whether the trip is domestic or abroad, and ties go abroad. Ambiguous codes (`CA`, `IN`, `CO`) are read in that context. A single home-airport charge does not turn an Italy trip into "MA", and one foreign-billed ride does not turn a San Diego trip into "NL". International trips are often higher-signal than domestic ones.
- **Apply-rules safety**: `housebook-audit apply-rules` only overrides generic categories (Uncategorized, Miscellaneous, Shopping & Retail). If a sidecar or prior audit set a specific category, it is respected as higher-authority.
- **Audit commands scope to recent data — mind it on backfills**: several
  commands limit their scope by default, which is correct for a *monthly*
  audit but would be misleading when **back-filling historical statements**.
  Each command now **echoes its effective scope** in its output and accepts
  an opt-in widening flag, but the defaults are unchanged — read the footer.
  The mechanisms differ (do not conflate them):
  - `apply-rules` — **365-day date window** (`date >= one_year_ago`) by
    default; on an old backfill it categorizes almost nothing. It now prints
    the floor date and the count of older rows skipped. Widen with
    `--since YYYY-MM-DD` or `--all` (no floor — full backfill pass).
  - `detect-trips` — **`--months` look-back** (default 12), now echoed in the
    header (`Scanning the last N months (since …)`); finds no old clusters
    unless widened with `--months N`.
  - `housebook-audit trips` / `trips --json` — **`--limit` count cap**
    (default 10), newest-first by `start_date`. This is *not* a date filter:
    older trips are simply truncated. The human output now prints
    `Showing X of N trips …` so the truncation is visible. Use `--limit 0`
    (or `--all`) for the whole table, or the overlap filters `--year YYYY` /
    `--since` / `--until` to pull just the slice a backfill needs.

  When auditing a historical import: pull the relevant trips with
  `trips --year`/`--since` (or `--all`) rather than trusting the default
  10-row listing; assign trip rows by **geographic signal** (out-of-region
  merchant location), not date-window overlap alone — many home-location
  charges fall inside a trip's dates but are routine home life — and
  categorize via direct `verify` (or `apply-rules --all`) rather than the
  default-windowed `apply-rules`.
- **Trip assignment uses location, not just dates**: A transaction's date falling inside a trip window is necessary but not sufficient. Confirm the merchant location matches the destination (QC/ON for a Toronto trip, VA/NC for a Charlotte drive, foreign-city transit for an overseas trip). Per the mandatory work-trip rule, any charge assigned to a `work` trip must also be set to `Work (Reimbursable)`.
