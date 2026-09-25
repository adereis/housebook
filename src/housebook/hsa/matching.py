"""Pure HSA expense/stub candidate and installment matching."""

from __future__ import annotations

from datetime import datetime

from housebook.hsa.providers import ProviderResolver

AMOUNT_TOLERANCE = 0.001
DATE_WINDOW_BEFORE = 5
DATE_WINDOW_AFTER = 45
INSTALLMENT_MIN_DAYS = 20
INSTALLMENT_MAX_DAYS = 40
INSTALLMENT_MIN_COUNT = 2


def find_candidate_matches(
    expenses,
    stubs,
    *,
    resolver: ProviderResolver,
    cc_provenance: dict[int, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return scored one-to-one matches and installment patterns."""
    expense_rows = [dict(expense) for expense in expenses]
    stub_rows = [dict(stub) for stub in stubs]
    provenance = cc_provenance or {}
    matches = []

    for expense in expense_rows:
        if _invalid_amount(expense):
            continue
        expense_date = _parse_date(expense.get("service_date"))
        if expense_date is None:
            continue
        expense_provider = resolver.resolve(expense.get("provider"))

        for stub in stub_rows:
            if _invalid_amount(stub):
                continue
            stub_date = _parse_date(stub.get("service_date"))
            if stub_date is None:
                continue
            amount_difference = abs(
                expense["patient_responsibility"]
                - stub["patient_responsibility"]
            )
            if amount_difference > AMOUNT_TOLERANCE:
                continue

            delta = (stub_date - expense_date).days
            if delta < -DATE_WINDOW_BEFORE or delta > DATE_WINDOW_AFTER:
                continue

            stub_provider = resolver.resolve(stub.get("provider"))
            provider_match = expense_provider == stub_provider
            score = 100 if provider_match else 0
            expected_lag = resolver.get_config(expense_provider).get(
                "expected_billing_lag_days"
            )
            if (
                expected_lag is not None
                and abs(delta - expected_lag) <= 2
            ):
                score += 50

            match = {
                "expense": expense,
                "stub": stub,
                "delta_days": delta,
                "score": score,
                "prov_match": provider_match,
            }
            transaction_id = stub.get("transaction_id")
            if transaction_id and transaction_id in provenance:
                match["cc_statement"] = provenance[transaction_id]
            matches.append(match)

    matches.sort(
        key=lambda match: (-match["score"], abs(match["delta_days"]))
    )
    return matches, detect_installment_patterns(
        expense_rows, stub_rows, resolver=resolver,
    )


def detect_installment_patterns(
    expenses,
    stubs,
    *,
    resolver: ProviderResolver | None = None,
) -> list[dict]:
    """Detect recurring same-amount stubs that suggest a payment plan."""
    resolver = resolver or ProviderResolver()
    groups: dict[tuple, list] = {}
    for stub in stubs:
        if stub.get("payment_plan_id") or _invalid_amount(stub):
            continue
        provider = resolver.resolve(stub.get("provider"))
        key = (provider, round(stub["patient_responsibility"], 2))
        groups.setdefault(key, []).append(dict(stub))

    patterns = []
    for (provider, amount), group_stubs in groups.items():
        if len(group_stubs) < INSTALLMENT_MIN_COUNT:
            continue
        dated = [
            (parsed, stub)
            for stub in group_stubs
            if (parsed := _parse_date(stub.get("service_date"))) is not None
        ]
        dated.sort(key=lambda item: item[0])
        if len(dated) < INSTALLMENT_MIN_COUNT:
            continue

        spacings = [
            (dated[index + 1][0] - dated[index][0]).days
            for index in range(len(dated) - 1)
        ]
        if not all(
            INSTALLMENT_MIN_DAYS <= days <= INSTALLMENT_MAX_DAYS
            for days in spacings
        ):
            continue

        masters = [
            dict(expense) for expense in expenses
            if resolver.resolve(expense.get("provider")) == provider
            and expense.get("source") in ("invoice", "eob")
            and (expense.get("patient_responsibility") or 0) > amount
            and not expense.get("payment_plan_id")
        ]
        patterns.append({
            "provider": provider,
            "installment_amount": amount,
            "installment_count": len(dated),
            "stub_ids": [stub["id"] for _, stub in dated],
            "stubs": [stub for _, stub in dated],
            "master_candidates": masters,
        })
    return patterns


def _invalid_amount(row: dict) -> bool:
    amount = row.get("patient_responsibility")
    return amount is None or amount <= 0


def _parse_date(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
