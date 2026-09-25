"""Immutable, versioned tax parameter registry.

Only parameter sets represented here may be used by the estimator. Adding
another year means adding a complete federal/state set with sources and
tests; callers must never select the nearest available year.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FederalTaxParameters:
    ordinary_brackets: tuple[tuple[Decimal, Decimal], ...]
    preferential_brackets: tuple[
        tuple[Decimal | None, Decimal], ...
    ]
    standard_deduction: Decimal
    salt_cap: Decimal
    niit_threshold: Decimal
    niit_rate: Decimal
    qbi_rate: Decimal
    capital_loss_ordinary_limit: Decimal
    child_tax_credit: Decimal
    child_tax_credit_age_limit: int
    child_tax_credit_phaseout: Decimal
    child_tax_credit_phaseout_step: Decimal
    child_tax_credit_reduction_per_step: Decimal


@dataclass(frozen=True)
class StateTaxParameters:
    jurisdiction: str
    ordinary_rate: Decimal
    personal_exemption: Decimal
    dependent_exemption: Decimal
    plan_529_deduction_limit: Decimal


@dataclass(frozen=True)
class TaxParameterSet:
    identifier: str
    tax_year: int
    filing_status: str
    filing_state: str
    federal: FederalTaxParameters
    state: StateTaxParameters
    sources: tuple[str, ...]
    filing_comparable: bool
    known_gaps: tuple[str, ...]


PARAMETER_SETS: dict[tuple[int, str, str], TaxParameterSet] = {
    (2025, "MFJ", "MA"): TaxParameterSet(
        identifier="us-2025-mfj-ma-v1",
        tax_year=2025,
        filing_status="MFJ",
        filing_state="MA",
        federal=FederalTaxParameters(
            # Bracket widths and marginal rates.
            ordinary_brackets=(
                (Decimal("23850"), Decimal("0.10")),
                (Decimal("73100"), Decimal("0.12")),
                (Decimal("109750"), Decimal("0.22")),
                (Decimal("187900"), Decimal("0.24")),
                (Decimal("106450"), Decimal("0.32")),
                (Decimal("250550"), Decimal("0.35")),
                (Decimal("999999999"), Decimal("0.37")),
            ),
            preferential_brackets=(
                (Decimal("96700"), Decimal("0.00")),
                (Decimal("600050"), Decimal("0.15")),
                (None, Decimal("0.20")),
            ),
            standard_deduction=Decimal("30000"),
            salt_cap=Decimal("10000"),
            niit_threshold=Decimal("250000"),
            niit_rate=Decimal("0.038"),
            qbi_rate=Decimal("0.20"),
            capital_loss_ordinary_limit=Decimal("3000"),
            child_tax_credit=Decimal("2000"),
            child_tax_credit_age_limit=17,
            child_tax_credit_phaseout=Decimal("400000"),
            child_tax_credit_phaseout_step=Decimal("1000"),
            child_tax_credit_reduction_per_step=Decimal("50"),
        ),
        state=StateTaxParameters(
            jurisdiction="MA",
            ordinary_rate=Decimal("0.05"),
            # Preserves the current estimator pending the filed-return
            # comparison; official MA guidance lists $8,800 for MFJ.
            personal_exemption=Decimal("4400"),
            dependent_exemption=Decimal("1000"),
            plan_529_deduction_limit=Decimal("2000"),
        ),
        sources=(
            "https://www.irs.gov/irb/2024-45_IRB",
            "https://www.irs.gov/filing/federal-income-tax-rates-and-brackets",
            "https://www.mass.gov/guides/personal-income-tax-for-residents",
            "https://www.mass.gov/info-details/massachusetts-personal-income-tax-exemptions",
            "https://www.mass.gov/info-details/massachusetts-tax-rates",
        ),
        filing_comparable=False,
        known_gaps=(
            "capital-loss carryover does not fully net short-term gains",
            "NIIT uses gross pre-carryover investment figures",
            "1098 real-estate taxes are not included in SALT",
            "Massachusetts MFJ personal exemption is still modeled as $4,400",
            "Massachusetts surtax and separate capital-gain rates are not modeled",
        ),
    ),
}


def get_parameter_set(
    tax_year: int,
    filing_status: str,
    filing_state: str,
) -> TaxParameterSet | None:
    return PARAMETER_SETS.get((tax_year, filing_status, filing_state))


def supported_parameter_keys() -> tuple[tuple[int, str, str], ...]:
    return tuple(sorted(PARAMETER_SETS))
