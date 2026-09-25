"""Backfill profile for legacy Amazon transactions.

Derives the profile name from the workspace directory structure
(AMAZON_DIR/<profile>/) instead of hardcoding a value.
"""

import os


def migrate(conn):
    from housebook.config.settings import AMAZON_DIR

    if not os.path.isdir(AMAZON_DIR):
        return

    # Skip if there are no Amazon transactions to backfill
    row = conn.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE source = 'Amazon' AND profile IS NULL"
    ).fetchone()
    if row[0] == 0:
        return

    profiles = [
        d for d in os.listdir(AMAZON_DIR)
        if os.path.isdir(os.path.join(AMAZON_DIR, d))
        and d not in ("__pycache__", ".DS_Store")
    ]

    if len(profiles) == 1:
        conn.execute(
            "UPDATE transactions SET profile = ? "
            "WHERE source = 'Amazon' AND profile IS NULL",
            (profiles[0],),
        )
    elif len(profiles) > 1:
        print(
            f"  WARNING: Multiple Amazon profiles found "
            f"({', '.join(sorted(profiles))}). "
            "Skipping automatic backfill — run manually:\n"
            "    UPDATE transactions SET profile = '<name>' "
            "WHERE source = 'Amazon' AND profile IS NULL;"
        )
