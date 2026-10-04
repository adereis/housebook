import os
import shutil

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

    # 2. Give a new workspace its live rules.json. It is the only rule
    # store (ingest and apply-rules read it, the dashboard lists its
    # categories), so start it from the template rather than leaving
    # the workspace without one. An existing file is never touched.
    if not os.path.exists(RULES_JSON):
        shutil.copyfile(EXAMPLE_RULES_JSON, RULES_JSON)
        print(f"Created {RULES_JSON} from the template")

    print("Database initialized successfully.")


def main():
    init_db()


if __name__ == "__main__":
    main()
