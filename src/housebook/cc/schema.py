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
  - all transactions within [start - 14d, end + 14d] (grace for
    posting lag; an installment parcel also gets one billing cycle
    per earlier parcel, since every parcel keeps the purchase date)
  - tx_count_db, tx_total_db consistent with the transactions array
  - account.last4 is exactly 4 digits or null
  - issuer is non-empty
  - sums of transaction amounts roughly match tx_total_db
  - a foreign-currency statement names its rate source and gives
    every transaction a positive fx_rate
  - every excluded line has a reason, and imported + excluded lines
    sum to closing - opening balance
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

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

CENT = Decimal("0.01")


class CcSchemaError(ValueError):
    """Raised when a CC sidecar's data block fails validation."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
    errors.extend(_row_errors(txs, "transactions", start, end))

    # Rows the statement prints but the import deliberately leaves out.
    excluded = data.get("excluded_transactions", [])
    if not isinstance(excluded, list):
        errors.append("excluded_transactions must be a list")
        excluded = []
    errors.extend(
        _row_errors(excluded, "excluded_transactions", start, end)
    )
    errors.extend(_excluded_errors(data, txs, excluded))

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


def _row_errors(rows: list, field: str, start, end) -> list[str]:
    """Check each row's shape, installment marker and date window.

    `transactions` and `excluded_transactions` share these checks: an
    excluded row is still a statement line, and a mis-dated one is
    the same extraction bug wherever it is filed.
    """
    errors: list[str] = []
    for i, t in enumerate(rows):
        where = f"{field}[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{where} must be an object")
            continue
        if "amount" not in t or not isinstance(t["amount"], (int, float)):
            errors.append(f"{where}.amount must be numeric")
        if not isinstance(t.get("description", ""), str):
            errors.append(f"{where}.description must be string")
        installment_errors, earlier_cycles = _installment(t, where)
        errors.extend(installment_errors)

        if not (start and end):
            continue
        try:
            d = _parse_date(t.get("date", ""), f"{where}.date")
        except CcSchemaError as e:
            errors.append(str(e))
            continue
        early = start - timedelta(
            days=TX_DATE_GRACE_DAYS + earlier_cycles * MAX_PERIOD_DAYS
        )
        late = end + timedelta(days=TX_DATE_GRACE_DAYS)
        if d < early or d > late:
            allowance = f"{TX_DATE_GRACE_DAYS}-day grace"
            if earlier_cycles:
                allowance += f" + {earlier_cycles} earlier billing cycle(s)"
            errors.append(
                f"{where}.date {d} is outside [{early}, {late}] "
                f"(period {start} to {end} with {allowance}) — "
                f"description={t.get('description')!r}"
            )
    return errors


def _installment(t: dict, where: str) -> tuple[list[str], int]:
    """Validate an installment marker. Return (errors, earlier cycles).

    Brazilian cards split a purchase into parcels, one per statement,
    and every parcel keeps the purchase date. Parcel n of m is billed
    n - 1 cycles after the purchase, so its date may sit that many
    cycles before the statement opens.

    The description must carry "n/m". Parcels of one purchase share
    its date and often its amount, so without the marker the
    cross-statement duplicate check would take parcel 2 for a repeat
    of parcel 1 and drop it.
    """
    inst = t.get("installment")
    if inst is None:
        return [], 0
    if not isinstance(inst, dict):
        return [
            f'{where}.installment must be an object like '
            f'{{"number": 2, "of": 3}}; got {inst!r}'
        ], 0
    n, m = inst.get("number"), inst.get("of")
    if not (_is_int(n) and _is_int(m) and 2 <= m and 1 <= n <= m):
        return [
            f"{where}.installment needs integers with "
            f"1 <= number <= of and of >= 2; got {inst!r}"
        ], 0

    errors: list[str] = []
    description = t.get("description", "")
    if isinstance(description, str) and f"{n}/{m}" not in description:
        errors.append(
            f"{where}.description must contain '{n}/{m}' so each "
            f"parcel stays distinct; got {description!r}"
        )
    metadata = t.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(
            f"{where}.metadata must be an object or null so the "
            "installment can be recorded in it"
        )
    return errors, n - 1


def _excluded_errors(data: dict, txs: list, excluded: list) -> list[str]:
    """Check that left-out rows are explained, and that none are missing.

    A card imported only for a trip still prints its regular charges,
    such as a subscription already tracked as a manual expense. Those
    lines go here instead of in `transactions`, each with a reason,
    and the ingestor skips them.

    Leaving lines out is safe only if the listed ones are all that was
    left out. So once any line is excluded, the balance identity is
    mandatory: imported plus excluded lines must equal closing minus
    opening balance, to the cent. It is not required of every sidecar,
    because many older DB-assisted ones do not satisfy it.
    """
    errors: list[str] = []
    for i, t in enumerate(excluded):
        if not isinstance(t, dict):
            continue
        reason = t.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(
                f"excluded_transactions[{i}].reason must say why the "
                "line is left out"
            )
    if not excluded:
        return errors

    balances = data.get("balances")
    if not isinstance(balances, dict):
        balances = {}
    opening, closing = balances.get("opening"), balances.get("closing")
    if not (_is_number(opening) and _is_number(closing)):
        errors.append(
            "excluded_transactions needs numeric balances.opening and "
            "balances.closing: the balance check is what proves only "
            "the listed lines were left out"
        )
        return errors

    lines = sum(
        (
            Decimal(str(t["amount"]))
            for t in txs + excluded
            if isinstance(t, dict) and _is_number(t.get("amount"))
        ),
        Decimal(0),
    ).quantize(CENT)
    expected = (Decimal(str(closing)) - Decimal(str(opening))).quantize(CENT)
    if lines != expected:
        errors.append(
            f"transactions + excluded_transactions sum to {lines}, but "
            f"closing - opening balance is {expected}: a line is "
            "missing or misread"
        )
    return errors
