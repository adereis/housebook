# Multi-year tax and secure-workspace roadmap

Status: accepted direction; staged implementation
Decision date: 2026-08-28

This document records two product decisions:

1. Tax estimation will evolve from the current 2025/MFJ/MA scenario
   into a versioned, multi-year engine whose results can be reconciled
   against filed-return evidence.
2. The dashboard will eventually be available on the local network only
   through authenticated TLS, while the workspace is encrypted at rest
   locally and in cloud storage.

It deliberately does **not** authorize exposing the current app,
migrating remote files, deleting plaintext data, or adding application-
managed encryption keys.

## IRS evidence to acquire

Use the IRS Individual Online Account and retain the original downloaded
files. Do not print a browser page back to PDF or rewrite a downloaded
PDF: the source bytes and their SHA-256 are provenance.

For each filed year, acquire:

1. **Record of Account Transcript** — primary federal filed-return
   ground truth. It combines the Tax Return Transcript with the Tax
   Account Transcript, so it shows original return line items plus
   account activity/adjustments. Online availability is the current
   year and three prior years. Start with every filed year currently
   offered (normally 2023–2025 as of this decision date).
2. **Wage and Income Transcript** — source-form completeness control.
   It reports information returns received by the IRS, including W-2,
   1098, 1099, and 5498. Download one from **each spouse's own IRS
   account**: an individual account exposes only information returns
   issued in that person's name, even for a joint return. It is
   generally available for the current and nine prior years.
3. **Tax Account Transcript** — obtain for older years where Record of
   Account is unavailable, and whenever payments, penalties, amendments,
   or IRS adjustments matter. Online availability is generally current
   plus nine prior years.
4. **Original filed federal return PDF** from the preparer or tax
   software. A transcript is not a photocopy and may not preserve every
   attachment or presentation detail. Form 4506 is the paid fallback for
   an IRS copy; Form 4506-T requests transcripts unavailable online.
5. **Filed state return or state transcript.** IRS online information-
   return documents omit or gray out state/local fields. For
   Massachusetts, request/download the personal income return through
   MassTaxConnect; Form M-4506 is the fallback.

Relevant primary guidance:

- [IRS transcript types and availability](https://www.irs.gov/individuals/transcript-types-for-individuals-and-ways-to-order-them)
- [IRS transcript service FAQs](https://www.irs.gov/individuals/transcript-services-for-individuals-faqs)
- [IRS Form 4506-T](https://www.irs.gov/forms-pubs/about-form-4506-t)
- [Massachusetts filed-return copies](https://www.mass.gov/info-details/request-copies-of-filed-tax-returns-payments-and-records-from-ma-dor)

Known acquisition constraints must remain visible:

- Record of Account and Return transcripts cover only current plus three
  prior years online. Use Form 4506-T for older unavailable periods.
- Wage and Income transcripts are limited to about 85 information
  documents online; use Form 4506-T if generation is refused.
- Current-year wage data can populate after the year begins and may be
  incomplete. It is reconciliation evidence, never permission to ignore
  a payer-issued correction or missing form.

## Tax evidence model

IRS/state transcripts must **not** become ordinary `tax_documents`
income rows. Doing that would count the same W-2/1099 twice: once from
the payer form and once from the transcript.

The future model has four layers:

1. **Source documents** — current `tax_documents`; payer-issued W-2,
   1099, 1098, Brazilian report, and similar inputs used for estimates.
2. **Filed-return evidence** — separate transcript/return artifacts with
   original file hash, subject, year, transcript type, retrieval date,
   as-of date, and raw extracted facts.
3. **Versioned estimate** — immutable result snapshot recording engine
   version, parameter-set version, input document IDs/hashes, filing
   status/state, and calculation output.
4. **Reconciliation** — explicit comparisons among estimate, original
   filed figures (`per return`), IRS-computed figures (`per computer`),
   later account adjustments, and state return. Differences are findings,
   not values silently substituted into the estimator.

Transcript extraction must preserve the printed IRS labels and source
line/code. Normalized facts are projections; the original PDF and raw
sidecar remain authoritative evidence.

## Multi-year estimator boundary

Tax parameters move out of global constants into an immutable registry
keyed by `(tax_year, filing_status, jurisdiction)`. A parameter set must
identify its authoritative source and effective/version date. Federal
and state calculations are separate strategies; adding a federal year
must not imply that a state's rules for that year exist.

The estimator continues to fail closed when any requested year/status/
state parameter set is absent. “Multi-year” means multiple explicitly
implemented and tested rule sets—not reusing the nearest year.

Implementation order:

1. **Implemented:** 2025/MFJ/MA now lives in an immutable registry with
   equivalent calculations. Estimate responses identify the parameter
   set, sources, filing-comparability status, and known gaps.
2. Ingest representative IRS and Massachusetts artifacts before fixing
   a transcript schema; real samples should determine the extraction
   vocabulary.
3. Add historical parameter sets one year at a time, starting with years
   for which filed evidence is available.
4. Correct the already-confirmed capital-loss, NIIT, and real-estate-tax
   gaps under tests before calling a year filing-comparable.
5. Store estimate snapshots and reconcile them against filed evidence.

No estimator output is filing advice or filing-grade merely because it
matches one transcript. Reviews and transcripts are evidence, never
authority for unmodeled tax law.

## LAN authentication boundary

The backend remains bound to loopback. A reverse proxy is the only LAN-
facing process; clients never reach Uvicorn directly.

Initial supported deployment:

- Caddy terminates HTTPS for a dedicated private DNS name (shown as
  `housebook.example.test` in committed examples). Private DNS resolves
  the live name to the LAN host; it is not exposed through router port
  forwarding.
- OAuth2 Proxy delegates authentication to Google with only the basic
  OpenID Connect identity scopes. It authorizes an explicit file of
  personal Gmail addresses and covers **all** paths, including
  `/static`, PDFs, health endpoints, and APIs.
- Caddy strips client-supplied identity/credential headers, copies the
  authenticated email from OAuth2 Proxy, writes a private shared
  credential, and proxies to `127.0.0.1:8000`.
- The application accepts that contract only in
  `HOUSEBOOK_AUTH_MODE=proxy`. It requires the private
  credential and a syntactically valid email on every request, and an
  exact allowed Origin on every mutating request. Existing HSA audit
  events record the asserted email.
- The Caddy internal CA root is installed explicitly on each authorized
  client, or a real locally-resolved domain uses a publicly trusted
  certificate. No plain HTTP session is supported.
- The host firewall limits the proxy to the intended LAN. Router port
  forwarding and public exposure are out of scope.
- Uvicorn refuses non-loopback binds; clients cannot bypass the proxy.

Account-free access may remain available at the direct loopback URL. In
proxy mode this is an explicit bypass requiring both a loopback transport
peer and an exact loopback Host; the private LAN hostname never bypasses
Google authentication. Mutations through either path retain exact-Origin
validation.

OAuth2 Proxy owns the session cookie and must set `Secure`, `HttpOnly`,
and `SameSite=Lax`. The app's exact Origin check is the independent CSRF
boundary for mutations; authentication does not replace it. The Google
client secret, OAuth2 Proxy cookie secret, private proxy credential, and
live email allowlist stay outside both repository and workspace.

The repository-side profile and live-host acceptance checklist are in
`deploy/lan/`. All committed hostnames and identities there are reserved,
fictitious examples; deployment substitutes live values only in external
configuration.

Primary security guidance:

- [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [OWASP session management guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP CSRF prevention guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [Caddy local HTTPS](https://caddyserver.com/docs/running#local-https-with-systemd)
- [OAuth2 Proxy Google provider](https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/google/)

## Encryption-at-rest boundary

Encryption is a storage/deployment concern, not a new database domain.
The application continues to receive one ordinary **decrypted mount** as
`HOUSEBOOK_WORKSPACE_DIR`. This keeps SQLite, PDF tools,
backups, migrations, and source modules independent of the encryption
implementation.

Recommended layers:

1. **Cloud now:** create a new, dedicated `rclone crypt` remote wrapping
   a new Google Drive path, with standard filename and directory-name
   encryption. Rclone encrypts/decrypts on the client and leaves remote
   objects encrypted. Encrypt `rclone.conf` or inject its password from
   a secret manager; the obscured password alone is not protection.
2. **Local next:** place the workspace on an OS-managed encrypted volume
   or encrypted filesystem and mount it only for Housebook sessions. The
   app sees the mounted plaintext view; unmounting removes ordinary file
   access. Full-disk/LUKS, fscrypt, or an audited encrypted-filesystem
   layer can satisfy this boundary depending on the host threat model.
3. **Keys:** no key, passphrase, recovery material, or decrypted rclone
   configuration may live inside the workspace, database, synced
   journal, repository, logs, or backups. Maintain an offline recovery
   copy and test restoration before deleting plaintext remote data.

Use a new encrypted remote path and a non-destructive canary migration:
push, list through the crypt remote, pull into an isolated workspace,
open/verify the database and sample documents, compare counts/hashes,
and run `rclone cryptcheck`. Deleting the existing plaintext Drive tree
requires a separate explicit user approval after successful recovery
testing.

Do **not** add SQLCipher yet. It encrypts only the database, not source
documents, and the current sync conflict detector deliberately reads
SQLite's plaintext file-header change counter at byte 24. SQLCipher
would invalidate that protocol plus database backup/recovery assumptions.
If SQLCipher is later desired as defense in depth, first replace header-
counter sync with an encryption-independent, authenticated revision
protocol and define key delivery for every CLI and background process.

Primary storage guidance:

- [rclone crypt](https://rclone.org/crypt/)
- [SQLCipher project documentation](https://github.com/sqlcipher/sqlcipher)

“Decrypt on demand” therefore means unlocking/mounting the workspace for
an application session and unmounting it afterward. While mounted and
while the authenticated app is running, the process can read the data;
encryption at rest cannot protect against a compromised authorized
process or unlocked host.

## Release gates

LAN access is not supported until all of these are true:

- authenticated TLS proxy covers every route;
- Uvicorn is unreachable from the LAN;
- Host and Origin allowlists are tested;
- authentication failures and audit identity are tested;
- secrets are outside the workspace and logs;
- backup/restore works through the encrypted storage path;
- there is no plaintext cloud fallback used by routine sync.

Multi-year “filed comparison” is not supported for a year until:

- federal and state parameter sets are explicit and sourced;
- source forms and filed-return evidence are separate;
- known-unknown amounts block completeness;
- estimate inputs/results are snapshotted;
- reconciliation differences remain visible and explainable.
