"""CC medical expense scanner.

Scans the transactions table for medical-category expenses and creates
stub entries in hsa_expenses so the user can collect receipts.
"""

import json
import os
import sqlite3
from typing import List

from housebook.config.settings import DB_PATH, HSA_SCANNER_JSON
from housebook.hsa.providers import ProviderResolver

MEDICAL_CATEGORIES = {
    "Health",  # primary category from rules.json template
    "Health & Medical",   # alternate name some rule sets use
    "Dental",
    "Vision",
}

MEDICAL_KEYWORDS = [
    "hospital", "medical", "clinic", "doctor", "physician",
    "health center", "urgent care", "emergency", "pediatric",
    "pharmacy", "cvs/pharmacy", "walgreens", "rite aid",
    "dental", "dentist", "orthodont",
    "optometrist", "ophthalmol", "eye care", "lenscrafters",
    "labcorp", "quest diag", "pathology",
    "physical therapy", "chiropractic", "acupuncture",
    "psychiatr", "psycholog", "counseling", "therapist",
    "planned parenthood",
]


def _load_scanner_config() -> dict:
    """Load the scanner config from config/hsa/scanner.json.

    A corrupt config must fail loudly: silently returning {} would
    disable every exclusion and let known non-HSA merchants create
    stubs again.
    """
    if not os.path.exists(HSA_SCANNER_JSON):
        return {}
    with open(HSA_SCANNER_JSON) as f:
        return json.load(f)


def _medical_keywords(config: dict) -> list[str]:
    """Built-in keywords plus the workspace's own `medical_keywords`.

    The built-in list holds only generic words and national chains.
    Regional hospital networks whose names carry no generic word
    belong in the workspace config. Lowercased because they are
    compared against LOWER(description).
    """
    extra = [str(k).lower() for k in config.get("medical_keywords", [])]
    return MEDICAL_KEYWORDS + extra


def _build_exclusion_sql(patterns: list[str]) -> tuple[str, list]:
    """Return a SQL fragment and params that exclude non-HSA merchants."""
    if not patterns:
        return "1=1", []
    conditions = " AND ".join(
        "LOWER(t.description) NOT LIKE ?" for _ in patterns
    )
    # Compared against LOWER(description) — patterns must be
    # lowercased too or an uppercase config entry never matches.
    params = [f"%{p.lower()}%" for p in patterns]
    return conditions, params


def _category_from_description(description: str) -> str:
    """Map a transaction description to an HSA category."""
    d = description.lower()
    if any(kw in d for kw in ["dental", "dentist", "orthodont"]):
        return "dental"
    if any(kw in d for kw in [
        "optometrist", "ophthalmol", "eye care",
        "lenscrafters", "vision",
    ]):
        return "vision"
    if any(kw in d for kw in [
        "pharmacy", "cvs", "walgreens", "rite aid", "rx",
    ]):
        return "pharmacy"
    if any(kw in d for kw in [
        "psychiatr", "psycholog", "therapist", "counseling",
    ]):
        return "mental_health"
    if any(kw in d for kw in [
        "labcorp", "quest diag", "pathology", "laboratory",
    ]):
        return "lab"
    return "medical"


def scan_cc_transactions(
    db_path: str = None,
    dry_run: bool = False,
    resolver: ProviderResolver = None,
) -> List[dict]:
    """Find medical transactions and create HSA stubs.

    Returns a list of dicts describing created (or would-create) stubs.
    """
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    config = _load_scanner_config()
    exclusion_patterns = [str(p) for p in config.get("exclusion_patterns", [])]
    excl_sql, excl_params = _build_exclusion_sql(exclusion_patterns)
    medical_keywords = _medical_keywords(config)

    # Find transactions in medical categories that don't already
    # have an HSA stub
    cat_placeholders = ",".join("?" for _ in MEDICAL_CATEGORIES)
    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.description, t.amount,
               t.category, t.source
        FROM transactions t
        WHERE t.category IN ({cat_placeholders})
          AND t.amount > 0
          AND t.source != 'Amazon'
          AND t.status != 'RECONCILED'
          AND t.linked_transaction_id IS NULL
          AND {excl_sql}
          AND t.id NOT IN (
              SELECT transaction_id FROM hsa_expenses
              WHERE transaction_id IS NOT NULL
          )
        ORDER BY t.date
        """,
        list(MEDICAL_CATEGORIES) + excl_params,
    ).fetchall()

    # Also find transactions matching medical keywords regardless
    # of category, but exclude ones we already found
    found_ids = {r["id"] for r in rows}
    keyword_conditions = " OR ".join(
        "LOWER(t.description) LIKE ?" for _ in medical_keywords
    )
    keyword_params = [f"%{kw}%" for kw in medical_keywords]

    keyword_rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.description, t.amount,
               t.category, t.source
        FROM transactions t
        WHERE ({keyword_conditions})
          AND t.amount > 0
          AND t.source != 'Amazon'
          AND t.status != 'RECONCILED'
          AND t.linked_transaction_id IS NULL
          AND t.category NOT IN ('CC Payment', 'Transfers & Refunds')
          AND {excl_sql}
          AND t.id NOT IN (
              SELECT transaction_id FROM hsa_expenses
              WHERE transaction_id IS NOT NULL
          )
        ORDER BY t.date
        """,
        keyword_params + excl_params,
    ).fetchall()

    # Merge, dedup
    all_rows = list(rows)
    for r in keyword_rows:
        if r["id"] not in found_ids:
            all_rows.append(r)
            found_ids.add(r["id"])

    if resolver is None:
        resolver = ProviderResolver()
    stubs = []
    for r in all_rows:
        hsa_category = _category_from_description(
            r["description"]
        )
        resolved_provider = resolver.resolve(r["description"])
        stub = {
            "transaction_id": r["id"],
            "service_date": r["date"],
            "provider": resolved_provider,
            "amount": float(r["amount"]),
            "category": hsa_category,
            "cc_category": r["category"],
            "source_card": r["source"],
        }
        stubs.append(stub)

        if not dry_run:
            conn.execute(
                "INSERT INTO hsa_expenses "
                "(service_date, provider, patient, "
                "description, patient_responsibility, "
                "category, payment_method, payment_date, "
                "transaction_id, source, status, "
                "needs_review, evidence_level, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["date"],
                    resolved_provider,
                    "self",
                    r["description"],
                    float(r["amount"]),
                    hsa_category,
                    r["source"],
                    r["date"],
                    r["id"],
                    "cc_stub",
                    "UNREIMBURSED",
                    1,
                    # Explicit: the column DEFAULT is the retired
                    # pre-migration-014 level 'unverified'.
                    "stub",
                    "Stub created from CC transaction. "
                    "Collect receipt to complete.",
                ),
            )

    if not dry_run and stubs:
        conn.commit()

    conn.close()
    return stubs
