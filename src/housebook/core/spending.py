"""The canonical spending-view predicate.

Root `AGENTS.md` states the exclusion contract once, but it was
previously written out by hand in seven places across `app.py` and
`audit.py` — and four of those copies had already drifted (missing
the zero-amount clause). Any change to the contract had to be
applied to every copy or the views would silently disagree.

The predicate is a literal SQL fragment with no user input, so it
composes into f-string queries safely.
"""

from .models import CATEGORY_CC_PAYMENT

# Filters, and why each exists (see root AGENTS.md):
#   linked_transaction_id  — hide paired purchase↔refund rows
#   status = RECONCILED    — hide bank-side Amazon duplicates
#   category = CC Payment  — hide card payments (balance transfers)
#   amount != 0            — drop no-op rows ($0 gift redemptions)
#
# The category and status tests are NULL-safe: a bare
# `category != 'CC Payment'` evaluates to NULL (not TRUE) for a NULL
# category, silently dropping the row from every spending view and
# total. Both columns are nullable in the schema.


def spend_filter(alias: str = "") -> str:
    """Return the spending-view WHERE fragment, optionally aliased.

    Pass the table alias when the query joins another table that also
    has `status` or `category` columns (e.g. `projects`), so the
    references are unambiguous.
    """
    p = f"{alias}." if alias else ""
    return (
        f"{p}amount != 0 "
        f"AND ({p}category IS NULL "
        f"OR {p}category != '{CATEGORY_CC_PAYMENT}') "
        f"AND ({p}status IS NULL OR {p}status != 'RECONCILED') "
        f"AND {p}linked_transaction_id IS NULL"
    )
