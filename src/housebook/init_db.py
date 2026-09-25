import json
import os
import sqlite3

from housebook.config.settings import (
    BACKUP_DIR,
    DB_PATH,
    EXAMPLE_RULES_JSON,
    RULES_JSON,
)
from housebook.core.database import backup_database
from housebook.migrations.runner import run_migrations


def init_db():
    # Ensure data directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(RULES_JSON), exist_ok=True)

    # Backup existing DB before migration (if it exists)
    if os.path.exists(DB_PATH):
        bk = backup_database(DB_PATH, BACKUP_DIR)
        if bk:
            print(f"  Pre-migration backup: {bk}")

    # 1. Run schema migrations
    print("Running schema migrations...")
    run_migrations(DB_PATH)

    # 2. Seed categorization rules from JSON
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    seed_json = RULES_JSON
    if not os.path.exists(seed_json):
        seed_json = EXAMPLE_RULES_JSON

    if os.path.exists(seed_json):
        with open(seed_json, "r") as f:
            categories = json.load(f)

        for cat, keywords in categories.items():
            for kw in keywords:
                c.execute(
                    "INSERT OR IGNORE INTO "
                    "categorization_rules "
                    "(category, keyword) VALUES (?, ?)",
                    (cat, kw),
                )

        essential = [
            "Work (Reimbursable)",
            "Excluded",
            "Miscellaneous",
        ]
        for cat in essential:
            c.execute(
                "INSERT OR IGNORE INTO "
                "categorization_rules "
                "(category, keyword) VALUES (?, ?)",
                (cat, f"MANUAL_{cat.upper()}"),
            )

        print(f"Seeded rules from {seed_json}")

    conn.commit()
    conn.close()
    print("Database initialized successfully.")


def main():
    init_db()


if __name__ == "__main__":
    main()
