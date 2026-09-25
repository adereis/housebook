import re
import sqlite3

from housebook.config.settings import BACKUP_DIR, DB_PATH, WORKSPACE_DIR
from housebook.core.database import (
    Database,
    backup_database,
)


def optimize_rules():
    db = Database(DB_PATH, workspace_dir=str(WORKSPACE_DIR))
    db.verify_schema_version()
    bk = backup_database(DB_PATH, BACKUP_DIR)
    if bk:
        print(f"  Pre-optimize backup: {bk}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT category, keyword FROM categorization_rules")
    rules = c.fetchall()

    new_rules = []
    for cat, kw in rules:
        # Strip Amazon prefix from keywords
        kw = re.sub(r"^Amazon: ", "", kw)

        # Simplify Bank strings: "MARKET BASKET #92 ANYTOWN MA" -> "MARKET BASKET"
        # We look for common patterns like # numbers, cities, states
        kw = re.sub(r"#\d+.*$", "", kw)  # Remove anything after a #
        kw = re.sub(r"\d{4,}.*$", "", kw)  # Remove long strings of numbers
        kw = kw.strip()

        if len(kw) > 3:  # Avoid tiny rules
            new_rules.append((cat, kw))

    # Re-insert cleaned rules
    for cat, kw in new_rules:
        c.execute(
            "INSERT OR IGNORE INTO categorization_rules "
            "(category, keyword) VALUES (?, ?)",
            (cat, kw),
        )

    conn.commit()
    conn.close()
    print("AI Knowledge Base optimized.")


def main():
    optimize_rules()


if __name__ == "__main__":
    main()
