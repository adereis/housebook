"""Tax sidecar `data` block schema + validators.

Tax sidecars are structurally simpler than CC: each sidecar maps
to one (or occasionally two, for the Brazilian XLSX case) tax
documents rather than a list of transactions. The `data` block has:

  - `tax_year` (required)
  - `document_type` (required — W2, 1099, 1099-R, 1098, etc.)
  - `issuer`, `recipient`, `category`, `amount`, `currency`
  - `form_data` — per-form-type fields (dividends, withholding, etc.)
  - Optionally `documents[]` array for multi-document files (Brazilian
    XLSX that produces both BR-TAX-REPORT and BR-FTC)

When `documents[]` is present, the ingestor iterates over it and
creates one `tax_documents` row per entry. When absent, the
top-level fields define a single document.
"""

from __future__ import annotations

VALID_DOC_TYPES = {
    "W2", "1099", "1099-R", "1099-HC", "1095-C",
    "1098", "BR-TAX-REPORT", "BR-FTC",
}

VALID_CATEGORIES = {
    "Income", "Interest", "Income/Interest", "Retirement",
    "Deduction", "Health", "Other", "Tax Paid",
}


class TaxSchemaError(ValueError):
    """Raised when a tax sidecar's data block fails validation."""


def _validate_single_doc(doc: dict, prefix: str) -> list[str]:
    """Validate one document entry (top-level or inside documents[])."""
    errors: list[str] = []

    dt = doc.get("document_type")
    if not isinstance(dt, str) or dt not in VALID_DOC_TYPES:
        errors.append(
            f"{prefix}document_type {dt!r} not in {VALID_DOC_TYPES}"
        )

    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or not issuer.strip():
        errors.append(f"{prefix}issuer must be a non-empty string")

    amount = doc.get("amount")
    if amount is not None and not isinstance(amount, (int, float)):
        errors.append(f"{prefix}amount must be numeric or null")

    currency = doc.get("currency", "USD")
    if not isinstance(currency, str):
        errors.append(f"{prefix}currency must be a string")

    category = doc.get("category")
    if category and category not in VALID_CATEGORIES:
        errors.append(
            f"{prefix}category {category!r} not in {VALID_CATEGORIES}"
        )

    return errors


def validate_data_block(data: dict) -> list[str]:
    """Validate a tax sidecar's `data` block.

    Returns a list of error strings; empty = valid.
    """
    errors: list[str] = []

    ty = data.get("tax_year")
    if not isinstance(ty, int) or ty < 2000 or ty > 2099:
        errors.append(
            f"tax_year must be an integer 2000-2099, got {ty!r}"
        )

    if "documents" in data:
        docs = data["documents"]
        if not isinstance(docs, list) or not docs:
            errors.append("documents must be a non-empty list")
        else:
            for i, doc in enumerate(docs):
                errors.extend(
                    _validate_single_doc(doc, f"documents[{i}].")
                )
    else:
        errors.extend(_validate_single_doc(data, ""))

    return errors
