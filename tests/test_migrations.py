import os
import sqlite3
import tempfile
import unittest

from housebook.migrations.runner import (
    get_schema_version,
    run_migrations,
)


class TestMigrationRunner(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_fresh_db_applies_baseline(self):
        applied = run_migrations(self.db_path, verbose=False)
        self.assertGreaterEqual(applied, 1)

        version = get_schema_version(self.db_path)
        self.assertGreaterEqual(version, 1)

    def test_idempotent_rerun(self):
        run_migrations(self.db_path, verbose=False)
        applied = run_migrations(self.db_path, verbose=False)
        self.assertEqual(applied, 0)

    def test_baseline_creates_tables(self):
        run_migrations(self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in c.fetchall()}
        conn.close()

        expected = {
            "transactions",
            "processed_files",
            "categorization_rules",
            "trips",
            "tax_documents",
            "ingestion_errors",
            "schema_version",
        }
        self.assertTrue(
            expected.issubset(tables),
            f"Missing tables: {expected - tables}",
        )

    def test_version_tracks_correctly(self):
        self.assertEqual(
            get_schema_version(self.db_path), 0,
        )
        run_migrations(self.db_path, verbose=False)
        self.assertGreater(
            get_schema_version(self.db_path), 0,
        )


if __name__ == "__main__":
    unittest.main()
