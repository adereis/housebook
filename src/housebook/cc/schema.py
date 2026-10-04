"""CC sidecar `data` block schema + validators.

Every CC sidecar wraps its source-specific block inside the v1
envelope (see core/sidecar.py). This module owns the contents of
the `data` field and the structural checks that run on it.

The validators here are the **single most important defense** against
import-time bugs landing in the database. They were written in
direct response to a real bug the bulk-backlog import hit: BoA
statements print their period as "December 12 - January 11, 2025"
with only one explicit year, and the agent applied that year to
both ends — producing `start > end`. The check `start <= end`
catches that case immediately. Other checks in the same spirit:

  - statement_period span between 20 and 40 days (typical billing cycles)
  - all transactions within [start - 5d, end + 5d] (5d grace for
    posting lag — real CC data has tx dates 1-2 days before period
    start; tightening past 5 days produces false positives)
  - tx_count_db, tx_total_db consistent with the transactions array
  - account.last4 is exactly 4 digits or null
  - issuer is non-empty
  - sums of transaction amounts roughly match tx_total_db
  - a foreign-currency statement names its rate source and gives
    every transaction a positive fx_rate
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# The ledger has no currency column: every `transactions.amount` is
# read as US dollars. A sidecar that omits `currency` is a USD
# statement; any other currency is converted at ingest.
BASE_CURRENCY = "USD"

# Permissive bounds. Most billing cycles are 28-32 days; we allow
# 20-40 to handle short-month boundaries, mid-cycle account opens,
# and account closes without false-positive escalations.
MIN_PERIOD_DAYS = 20
MAX_PERIOD_DAYS = 40

# Posting lag: a transaction's authorization date can fall well
# outside the statement's posting-date period. Car rentals, hotels,
# and international merchants commonly have 7-14 day auth→post
# delays. 14 days handles all observed real-world lag while still
# catching year-inference bugs (which are 330+ days off).
TX_DATE_GRACE_DAYS = 14

# Sums match tolerance — DB stores rounded floats; allow $0.10 drift
# across the whole statement to absorb rounding noise.
SUM_TOLERANCE = 0.10


class CcSchemaError(ValueError):
    """Raised when a CC sidecar's data block fails validation."""


def statement_currency(data: dict) -> str:
    """Return the currency the statement's amounts are printed in."""
    return data.get("currency", BASE_CURRENCY)


def _parse_date(s: str, field: str) -> date:
    if not isinstance(s, str) or not DATE_RE.match(s):
        raise CcSchemaError(
            f"{field}: expected ISO date YYYY-MM-DD, got {s!r}"
        )
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise CcSchemaError(f"{field}: invalid date {s!r}") from e


def validate_data_block(data: dict, issuer_resolver=None) -> list[str]:
    """Validate a CC sidecar's `data` block.

    Returns a list of error strings; empty list means valid.
    Errors are accumulated rather than raised on first failure so
    `housebook-cc validate` can report all problems with one file in
    one pass.

    `issuer_resolver` is optional. When supplied (an
    `IssuerResolver`), the issuer is checked against
    `config/cc/issuers.json` and a *known alias* written instead of
    the canonical name is flagged — this is the check that would have
    caught a real alias-spelling source split at its source.
    The ingestor canonicalizes regardless, so this is a clean-up nudge
    surfaced by `housebook-cc validate`/`check`, not an ingest gate.
    Unknown issuers are *not* flagged: a genuinely new card may be
    added before `issuers.json` is updated.
    """
    errors: list[str] = []

    # ── issuer / account ───────────────────────────────────────
    issuer = data.get("issuer")
    if not isinstance(issuer, str) or not issuer.strip():
        errors.append("issuer must be a non-empty string")
    elif issuer_resolver is not None:
        canonical = issuer_resolver.resolve(issuer)
        if canonical != issuer.strip():
            errors.append(
                f"issuer {issuer!r} is a known alias of canonical "
                f"{canonical!r}; use the canonical name (see "
                "config/cc/issuers.json) so the DB `source` stays "
                "consistent across statements"
            )

    account = data.get("account")
    if not isinstance(account, dict):
        errors.append("account must be an object")
        account = {}
    last4 = account.get("last4")
    if last4 is not None and not (
        isinstance(last4, str) and re.fullmatch(r"\d{4}|XXXX", last4)
    ):
        errors.append(
            f"account.last4 must be 4 digits, 'XXXX', or null; got {last4!r}"
        )

    # ── statement_period ────────────────────────────────────────
    sp = data.get("statement_period")
    if not isinstance(sp, dict):
        errors.append("statement_period must be an object")
        sp = {}

    try:
        start = _parse_date(sp.get("start", ""), "statement_period.start")
        end = _parse_date(sp.get("end", ""), "statement_period.end")
    except CcSchemaError as e:
        errors.append(str(e))
        start = end = None

    if start and end:
        if start > end:
            errors.append(
                f"statement_period.start ({start}) must be <= end ({end}) — "
                "this caught the BoA single-year-format bug; check the agent's "
                "year inference"
            )
        else:
            span = (end - start).days
            if span < MIN_PERIOD_DAYS or span > MAX_PERIOD_DAYS:
                errors.append(
                    f"statement_period span {span} days is outside "
                    f"[{MIN_PERIOD_DAYS}, {MAX_PERIOD_DAYS}] — "
                    "expected a single billing cycle"
                )

    # ── transactions ────────────────────────────────────────────
    txs = data.get("transactions")
    if not isinstance(txs, list):
        errors.append("transactions must be a list")
        txs = []

    if start and end and txs:
        early = start - timedelta(days=TX_DATE_GRACE_DAYS)
        late = end + timedelta(days=TX_DATE_GRACE_DAYS)
        for i, t in enumerate(txs):
            if not isinstance(t, dict):
                # Reported by the type-check loop below; a non-dict here
                # must not abort the whole batch with an AttributeError.
                continue
            try:
                d = _parse_date(t.get("date", ""), f"transactions[{i}].date")
            except CcSchemaError as e:
                errors.append(str(e))
                continue
            if d < early or d > late:
                errors.append(
                    f"transactions[{i}].date {d} is outside "
                    f"[{early}, {late}] (period {start} to {end} "
                    f"with {TX_DATE_GRACE_DAYS}-day grace) — "
                    f"description={t.get('description')!r}"
                )

    for i, t in enumerate(txs):
        if not isinstance(t, dict):
            errors.append(f"transactions[{i}] must be an object")
            continue
        if "amount" not in t or not isinstance(
            t["amount"], (int, float)
        ):
            errors.append(
                f"transactions[{i}].amount must be numeric"
            )
        if not isinstance(t.get("description", ""), str):
            errors.append(f"transactions[{i}].description must be string")

    errors.extend(_currency_errors(data, txs))

    # ── reconciliation: tx_count_db and tx_total_db ──────────────
    tx_count_db = data.get("tx_count_db")
    if tx_count_db is not None:
        if not isinstance(tx_count_db, int):
            errors.append("tx_count_db must be int or null")
        elif tx_count_db != len(txs):
            errors.append(
                f"tx_count_db ({tx_count_db}) != len(transactions) "
                f"({len(txs)})"
            )

    tx_total_db = data.get("tx_total_db")
    if tx_total_db is not None:
        if not isinstance(tx_total_db, (int, float)):
            errors.append("tx_total_db must be numeric or null")
        else:
            actual = sum(
                float(t.get("amount", 0))
                for t in txs
                if isinstance(t, dict) and isinstance(
                    t.get("amount"), (int, float)
                )
            )
            if abs(actual - float(tx_total_db)) > SUM_TOLERANCE:
                errors.append(
                    f"tx_total_db ({tx_total_db:.2f}) does not match "
                    f"sum(transactions.amount) ({actual:.2f}) — "
                    f"difference exceeds ${SUM_TOLERANCE:.2f} tolerance"
                )

    return errors


def _currency_errors(data: dict, txs: list) -> list[str]:
    """Check that a foreign statement carries what its conversion needs.

    The statement's own amounts stay as printed, so its totals and
    balances still check in that currency. Each row carries the rate
    the ingestor divides by, and `fx_source` says where the rates came
    from, so a converted amount can always be traced and recomputed.
    """
    errors: list[str] = []
    currency = statement_currency(data)
    if not (isinstance(currency, str) and CURRENCY_RE.match(currency)):
        return [
            f"currency must be a 3-letter ISO 4217 code such as 'BRL'; "
            f"got {currency!r}"
        ]

    rows = [(i, t) for i, t in enumerate(txs) if isinstance(t, dict)]
    if currency == BASE_CURRENCY:
        return [
            f"transactions[{i}].fx_rate is set, but the statement is "
            f"in {BASE_CURRENCY} and needs no conversion"
            for i, t in rows if "fx_rate" in t
        ]

    fx_source = data.get("fx_source")
    if not isinstance(fx_source, str) or not fx_source.strip():
        errors.append(
            f"fx_source must name where the {currency} rates came from; "
            f"a {currency} statement is converted to {BASE_CURRENCY} "
            "at ingest"
        )
    for i, t in rows:
        rate = t.get("fx_rate")
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or rate <= 0):
            errors.append(
                f"transactions[{i}].fx_rate must be a positive number "
                f"of {currency} per 1 {BASE_CURRENCY}; got {rate!r}"
            )
        metadata = t.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            errors.append(
                f"transactions[{i}].metadata must be an object or null "
                "so the conversion can be recorded in it"
            )
    return errors
