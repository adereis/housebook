"""Tax estimate computation engine.

Computes federal + state tax liability from ingested tax_documents
and user_profile.json configuration. Used by both the CLI
(housebook-tax estimate) and the API (/api/tax_estimate).
"""

import json
import sqlite3
from decimal import Decimal

from housebook.config.settings import DB_PATH, USER_PROFILE_JSON
from housebook.tax.parameters import (
    TaxParameterSet,
    get_parameter_set,
    supported_parameter_keys,
)

DEFAULT_FILING_STATUS = "MFJ"
DEFAULT_FILING_STATE = "MA"


def _check_supported(year, filing_status, filing_state):
    """Return an error string if the inputs fall outside the tables.

    Missing registry entries fail closed. Silently selecting a nearby
    year or different status/state would produce confidently labeled
    but wrong dollar figures.
    """
    if get_parameter_set(year, filing_status, filing_state):
        return None
    supported = ", ".join(
        f"{key_year}/{key_status}/{key_state}"
        for key_year, key_status, key_state in supported_parameter_keys()
    )
    return (
        f"Tax parameter set {year}/{filing_status}/{filing_state} is "
        f"not supported. Explicitly implemented sets: {supported}. "
        "Add sourced parameters and tests; do not reuse another year."
    )


def _preferential_tax(
    taxable_ordinary: Decimal,
    taxable_pref: Decimal,
    parameters: TaxParameterSet,
) -> tuple:
    """Tax the preferential slice stacked above ordinary income.

    Returns (tax, [{"rate", "taxed", "tax"}, ...]). Walking the
    brackets — rather than assuming a flat 15% — is what makes a
    modest-income filer's long-term gains correctly free of tax
    instead of being billed 15% from the first dollar.
    """
    tax = Decimal("0")
    detail = []
    # Where the preferential slice starts and ends in total taxable
    # income: it sits on top of the ordinary slice.
    lo = taxable_ordinary
    hi = taxable_ordinary + taxable_pref

    for upper, rate in parameters.federal.preferential_brackets:
        if lo >= hi:
            break
        band_top = hi if upper is None else min(upper, hi)
        if band_top <= lo:
            continue
        taxed = band_top - lo
        band_tax = (taxed * rate).quantize(Decimal("0.01"))
        tax += band_tax
        detail.append({
            "rate": float(rate),
            "taxed": float(taxed),
            "tax": float(band_tax),
        })
        lo = band_top

    return tax, detail


def _rd_dec(raw_data: dict, key: str) -> Decimal:
    """Extract a Decimal from raw_data, defaulting to 0."""
    val = raw_data.get(key)
    if val is None:
        return Decimal("0")
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def compute_tax_estimate(db_path=None, year=2025):
    """Compute federal + state tax estimate from tax_documents.

    Returns a dict suitable for JSON serialization or CLI display.
    Reads filing status from user_profile.json config.
    """
    import os

    # Load filing config first: an unsupported year/status/state must
    # be reported as such, not masked by "no documents for <year>".
    filing_status = DEFAULT_FILING_STATUS
    filing_state = DEFAULT_FILING_STATE
    dependents = 0
    dependent_ages = []
    loss_carryover = Decimal("0")
    plan_529_ma = False
    plan_529_contribution = Decimal("0")
    if os.path.exists(USER_PROFILE_JSON):
        with open(USER_PROFILE_JSON) as f:
            profile = json.load(f)
        tf = profile.get("tax_filing", {})
        filing_status = tf.get("status", "MFJ")
        filing_state = tf.get("state", "MA")
        dependents = tf.get("dependents", 0)
        dependent_ages = tf.get("dependent_ages", [])
        loss_carryover = Decimal(str(tf.get(
            f"loss_carryover_{year - 1}", 0,
        )))
        plan_529_ma = tf.get("plan_529_ma", False)
        plan_529_contribution = Decimal(str(
            tf.get("plan_529_contribution", 0),
        ))

    unsupported = _check_supported(year, filing_status, filing_state)
    if unsupported:
        return {"error": unsupported}
    parameters = get_parameter_set(year, filing_status, filing_state)
    assert parameters is not None  # established by _check_supported
    federal = parameters.federal
    state = parameters.state

    conn = _connect(db_path)
    cur = conn.cursor()
    rows = cur.execute(
        """SELECT document_type, issuer, amount, raw_data
           FROM tax_documents WHERE tax_year = ?""",
        (year,),
    ).fetchall()
    conn.close()

    if not rows:
        return {"error": f"No tax documents for {year}"}

    # Aggregate values from documents
    wages = Decimal("0")
    fed_withheld = Decimal("0")
    state_withheld = Decimal("0")
    total_divs = Decimal("0")
    qual_divs = Decimal("0")
    st_gains = Decimal("0")
    lt_gains = Decimal("0")
    cg_dist = Decimal("0")
    br_income = Decimal("0")
    ftc = Decimal("0")
    mortgage = Decimal("0")
    s199a = Decimal("0")
    rollover = Decimal("0")

    # Documents whose amount is NULL (unknown — extraction failed or
    # the form carries none). Contributing 0 would let an unextracted
    # W2 quietly understate wages, so they are collected and surfaced
    # alongside the estimate instead.
    unknown_amounts = []

    for row in rows:
        rd = json.loads(row["raw_data"]) if row["raw_data"] else {}
        doc_type = row["document_type"]
        if row["amount"] is None:
            unknown_amounts.append({
                "document_type": doc_type,
                "issuer": row["issuer"],
            })
            amount = Decimal("0")
        else:
            amount = Decimal(str(row["amount"]))

        if doc_type == "W2":
            wages += amount
            fed_withheld += _rd_dec(rd, "federal_tax_withheld")
            state_withheld += _rd_dec(rd, "state_tax_withheld")
        elif doc_type == "1099":
            total_divs += _rd_dec(rd, "dividends")
            qual_divs += _rd_dec(rd, "qualified_dividends")
            cg_dist += _rd_dec(rd, "capital_gains")
            st_gains += _rd_dec(rd, "short_term_gain_loss")
            lt_gains += _rd_dec(rd, "long_term_gain_loss")
            s199a += _rd_dec(rd, "section_199a_dividends")
        elif doc_type == "1098":
            mortgage += amount
        elif doc_type == "BR-TAX-REPORT":
            br_income += amount
        elif doc_type == "BR-FTC":
            ftc += amount
        elif doc_type == "1099-R":
            if rd.get("is_rollover"):
                rollover += amount

    ordinary_divs = total_divs - qual_divs

    # Net capital gains: ST losses offset LT gains, then carryover
    net_st = st_gains
    net_lt = lt_gains + cg_dist
    if net_st < 0:
        net_lt = net_lt + net_st
        net_st = Decimal("0")
        if net_lt < 0:
            net_st = max(net_lt, Decimal("-3000"))
            net_lt = Decimal("0")

    # Apply loss carryover from prior years
    carryover_used = Decimal("0")
    if loss_carryover > 0 and net_lt > 0:
        used = min(loss_carryover, net_lt)
        net_lt -= used
        carryover_used += used
        loss_carryover -= used
    if loss_carryover > 0:
        ordinary_offset = min(
            loss_carryover, federal.capital_loss_ordinary_limit,
        )
        net_st -= ordinary_offset
        carryover_used += ordinary_offset

    # AGI
    ordinary_income = wages + ordinary_divs + net_st + br_income
    pref_income = qual_divs + net_lt
    agi = ordinary_income + pref_income

    # Deductions
    salt = min(state_withheld, federal.salt_cap)
    itemized = mortgage + salt
    standard = federal.standard_deduction
    use_itemized = itemized > standard
    deduction = itemized if use_itemized else standard
    qbi = s199a * federal.qbi_rate

    # The deduction reduces TOTAL taxable income, not just the ordinary
    # slice: any part not absorbed by ordinary income spills over and
    # shelters preferential income too. Subtracting it from ordinary
    # only (and flooring at zero) discarded that remainder, taxing
    # gains that the deduction had already covered.
    taxable_total = max(agi - deduction - qbi, Decimal("0"))
    taxable_pref = min(max(pref_income, Decimal("0")), taxable_total)
    taxable_ordinary = taxable_total - taxable_pref

    # Federal tax on ordinary income
    fed_ordinary_tax = Decimal("0")
    remaining = taxable_ordinary
    bracket_detail = []
    for bracket_size, rate in federal.ordinary_brackets:
        if remaining <= 0:
            break
        taxed = min(remaining, bracket_size)
        tax = (taxed * rate).quantize(Decimal("0.01"))
        fed_ordinary_tax += tax
        bracket_detail.append({
            "rate": float(rate),
            "taxed": float(taxed),
            "tax": float(tax),
        })
        remaining -= taxed

    pref_tax, pref_bracket_detail = _preferential_tax(
        taxable_ordinary, taxable_pref, parameters,
    )

    # NIIT (3.8% on investment income above threshold)
    niit = Decimal("0")
    if agi > federal.niit_threshold:
        net_inv = (total_divs + st_gains + lt_gains
                   + cg_dist + br_income)
        niit_base = min(net_inv, agi - federal.niit_threshold)
        niit = (max(niit_base, Decimal("0")) * federal.niit_rate
                ).quantize(Decimal("0.01"))

    total_fed_tax = fed_ordinary_tax + pref_tax + niit

    # Child Tax Credit (CTC): $2,000/child under 17
    # Phase-out: reduced by $50 per $1,000 AGI above $400K MFJ
    ctc = Decimal("0")
    ctc_phaseout = Decimal("0")
    children_under_17 = sum(
        1 for age in dependent_ages
        if age < federal.child_tax_credit_age_limit
    )
    if children_under_17 > 0:
        ctc_base = federal.child_tax_credit * children_under_17
        if agi > federal.child_tax_credit_phaseout:
            ctc_phaseout = (
                (
                    (agi - federal.child_tax_credit_phaseout)
                    / federal.child_tax_credit_phaseout_step
                ).quantize(Decimal("1"))
                * federal.child_tax_credit_reduction_per_step
            )
        ctc = max(ctc_base - ctc_phaseout, Decimal("0"))

    fed_after_credits = total_fed_tax - ftc - ctc
    fed_balance = fed_after_credits - fed_withheld

    # State tax (MA: flat 5%)
    ma_dep_exemption = state.personal_exemption + (
        state.dependent_exemption * dependents
    )
    ma_529_deduction = (
        min(plan_529_contribution, state.plan_529_deduction_limit)
        if plan_529_ma else Decimal("0")
    )
    ma_taxable = (wages + ordinary_divs + st_gains + br_income
                  + qual_divs + net_lt
                  - ma_dep_exemption - ma_529_deduction)
    ma_tax = (max(ma_taxable, Decimal("0")) * state.ordinary_rate
              ).quantize(Decimal("0.01"))
    ma_balance = ma_tax - state_withheld

    return {
        "year": year,
        "filing_status": filing_status,
        "filing_state": filing_state,
        "parameter_set": {
            "id": parameters.identifier,
            "filing_comparable": parameters.filing_comparable,
            "sources": list(parameters.sources),
            "known_gaps": list(parameters.known_gaps),
        },
        "income": {
            "wages": float(wages),
            "ordinary_dividends": float(ordinary_divs),
            "qualified_dividends": float(qual_divs),
            "short_term_gains": float(st_gains),
            "long_term_gains": float(lt_gains),
            "capital_gain_distributions": float(cg_dist),
            "loss_carryover_applied": float(carryover_used),
            "brazilian_income": float(br_income),
            "rollover_nontaxable": float(rollover),
            "section_199a": float(s199a),
        },
        "agi": float(agi),
        "deductions": {
            "type": "itemized" if use_itemized else "standard",
            "amount": float(deduction),
            "mortgage_interest": float(mortgage),
            "salt_capped": float(salt),
            "standard_available": float(standard),
            "itemized_available": float(itemized),
            "qbi_deduction": float(qbi),
        },
        "taxable_income": {
            "ordinary": float(taxable_ordinary),
            "preferential": float(taxable_pref),
            "total": float(taxable_total),
        },
        "federal": {
            "ordinary_tax": float(fed_ordinary_tax),
            "preferential_tax": float(pref_tax),
            "niit": float(niit),
            "total_tax": float(total_fed_tax),
            "foreign_tax_credit": float(ftc),
            "child_tax_credit": float(ctc),
            "ctc_phaseout": float(ctc_phaseout),
            "tax_after_credits": float(fed_after_credits),
            "withheld": float(fed_withheld),
            "balance": float(fed_balance),
        },
        "state": {
            "name": filing_state,
            "rate": float(state.ordinary_rate),
            "exemption": float(ma_dep_exemption),
            "plan_529_deduction": float(ma_529_deduction),
            "taxable": float(ma_taxable),
            "tax": float(ma_tax),
            "withheld": float(state_withheld),
            "balance": float(ma_balance),
        },
        "brackets": bracket_detail,
        "preferential_brackets": pref_bracket_detail,
        # Non-empty means the estimate is incomplete: these documents
        # contributed nothing because their amount is unknown.
        "unknown_amounts": unknown_amounts,
    }
