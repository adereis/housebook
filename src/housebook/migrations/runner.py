"""Simple schema migration runner.

Tracks applied migrations in a `schema_version` table.
Migration files are numbered SQL or Python scripts
(001_name.sql, 002_name.py, etc.) in this directory.
Python migrations must define a ``migrate(conn)`` function.
"""

import importlib.util
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent


def _ensure_version_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _get_current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()
    return row[0] or 0


def _discover_migrations() -> list:
    """Return sorted list of (version, name, path) tuples."""
    seen: dict[int, tuple] = {}
    for pattern in ("*.sql", "*.py"):
        for f in MIGRATIONS_DIR.glob(pattern):
            if f.name == "__init__.py" or f.name == "runner.py":
                continue
            parts = f.stem.split("_", 1)
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            version = int(parts[0])
            name = parts[1]
            seen[version] = (version, name, f)
    return sorted(seen.values())


# Derived from the migration files so it can never drift from them
# (a stale hand-maintained constant made every tool refuse freshly
# migrated DBs with SchemaTooNewError).
_discovered = _discover_migrations()
EXPECTED_SCHEMA_VERSION = _discovered[-1][0] if _discovered else 0


def run_migrations(db_path: str, verbose: bool = True):
    """Apply all pending migrations to the database."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_version_table(conn)

    current = _get_current_version(conn)
    migrations = _discover_migrations()
    applied = 0

    for version, name, path in migrations:
        if version <= current:
            continue

        if verbose:
            print(f"  Applying migration {version:03d}_{name}...")

        if path.suffix == ".py":
            spec = importlib.util.spec_from_file_location(
                f"migration_{version:03d}", path,
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.migrate(conn)
        else:
            conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_version (version, name) "
            "VALUES (?, ?)",
            (version, name),
        )
        conn.commit()
        applied += 1

    if verbose:
        if applied:
            print(f"  Applied {applied} migration(s). "
                  f"Schema at version {_get_current_version(conn)}.")
        else:
            print(f"  Schema up to date (version {current}).")

    conn.close()
    return applied


def get_schema_version(db_path: str) -> int:
    """Return current schema version, or 0 if uninitialized."""
    conn = sqlite3.connect(db_path)
    try:
        _ensure_version_table(conn)
        return _get_current_version(conn)
    finally:
        conn.close()
