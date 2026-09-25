import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from housebook.core.database import (
    Database,
    SchemaTooNewError,
    _rotate_backups,
    backup_database,
    checkpoint_wal,
)


class TestDatabaseMigration(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        with patch("builtins.print"):
            self.db = Database(self.db_path)
        self._init_db()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""CREATE TABLE processed_files (
                        file_path TEXT PRIMARY KEY,
                        file_hash TEXT,
                        last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        statement_start DATE,
                        statement_end DATE
                    )""")
        c.execute("""CREATE TABLE ingestion_errors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_path TEXT,
                        line_number INTEGER,
                        raw_text TEXT,
                        error TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )""")
        conn.commit()
        conn.close()

    def test_is_file_processed_migration(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # Simulate old entry with NULL hash
        c.execute(
            "INSERT INTO processed_files (file_path, file_hash) VALUES (?, ?)",
            ("old_file.pdf", None),
        )
        conn.commit()
        conn.close()

        # Should return True (processed) AND update hash
        new_hash = "new_hash_123"
        processed = self.db.is_file_processed("old_file.pdf", new_hash)
        self.assertTrue(processed)

        # Verify hash update
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT file_hash FROM processed_files WHERE file_path = ?",
            ("old_file.pdf",),
        )
        stored_hash = c.fetchone()[0]
        self.assertEqual(stored_hash, new_hash)
        conn.close()

    def test_is_file_processed_match(self):
        hash_val = "hash_456"
        self.db.mark_file_processed("file.pdf", hash_val)

        # Exact match -> True
        self.assertTrue(self.db.is_file_processed("file.pdf", hash_val))

        # Mismatch -> False
        self.assertFalse(self.db.is_file_processed("file.pdf", "different_hash"))

    def test_is_file_processed_new(self):
        self.assertFalse(self.db.is_file_processed("new_file.pdf", "hash_789"))

    def test_wal_mode_enabled(self):
        conn = self.db._get_connection()
        c = conn.cursor()
        c.execute("PRAGMA journal_mode")
        mode = c.fetchone()[0]
        conn.close()
        self.assertEqual(mode, "wal")

    def test_log_ingestion_error(self):
        self.db.log_ingestion_error(
            "test.pdf", 42, "bad line content", "Amount parse error"
        )

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT file_path, line_number, raw_text, error "
            "FROM ingestion_errors"
        )
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "test.pdf")
        self.assertEqual(row[1], 42)
        self.assertEqual(row[2], "bad line content")
        self.assertEqual(row[3], "Amount parse error")

    def test_dry_run_skips_writes(self):
        dry_db = Database(self.db_path, dry_run=True)
        dry_db.mark_file_processed("should_not_exist.pdf", "hash")
        dry_db.log_ingestion_error("x.pdf", 1, "text", "error")

        self.assertFalse(self.db.is_file_processed("should_not_exist.pdf", "hash"))

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM ingestion_errors")
        count = c.fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_transaction_commits_all_writes_together(self):
        with self.db.transaction() as connection:
            self.db.log_ingestion_error(
                "statement.csv", 7, "bad row", "bad amount",
                connection=connection,
            )
            self.db.mark_file_processed(
                "statement.csv", "hash-123",
                connection=connection,
            )

        self.assertTrue(
            self.db.is_file_processed("statement.csv", "hash-123")
        )
        conn = sqlite3.connect(self.db_path)
        error_count = conn.execute(
            "SELECT COUNT(*) FROM ingestion_errors"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(error_count, 1)

    def test_transaction_rolls_back_on_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.db.transaction() as connection:
                self.db.log_ingestion_error(
                    "statement.csv", 7, "bad row", "bad amount",
                    connection=connection,
                )
                self.db.mark_file_processed(
                    "statement.csv", "hash-123",
                    connection=connection,
                )
                raise KeyboardInterrupt

        self.assertFalse(
            self.db.is_file_processed("statement.csv", "hash-123")
        )
        conn = sqlite3.connect(self.db_path)
        error_count = conn.execute(
            "SELECT COUNT(*) FROM ingestion_errors"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(error_count, 0)


class TestBackupDatabase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "finance.db")
        self.backup_dir = os.path.join(self.tmpdir, "backups")
        # Create a real DB with some data
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_backup_creates_file(self):
        path = backup_database(self.db_path, self.backup_dir)
        self.assertTrue(os.path.exists(path))
        self.assertIn("finance.db.", os.path.basename(path))

    def test_backup_is_valid_sqlite(self):
        path = backup_database(self.db_path, self.backup_dir)
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT id FROM t").fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    def test_backup_nonexistent_db_returns_empty(self):
        result = backup_database("/no/such/file.db", self.backup_dir)
        self.assertEqual(result, "")

    def test_backup_hard_cap(self):
        """Total backups never exceed max_backups."""
        for i in range(15):
            # Mutate DB each iteration so hash-dedup doesn't skip
            conn = sqlite3.connect(self.db_path)
            conn.execute(f"INSERT INTO t VALUES ({100 + i})")
            conn.commit()
            conn.close()
            backup_database(self.db_path, self.backup_dir,
                            max_backups=5)
        backups = os.listdir(self.backup_dir)
        self.assertLessEqual(len(backups), 5)

    def test_tiered_keeps_all_within_last_hour(self):
        """Backups from the last hour are all retained."""
        os.makedirs(self.backup_dir, exist_ok=True)
        now = datetime(2026, 4, 14, 18, 0, 0)
        # Create 5 backups within the last hour
        for i in range(5):
            ts = now - timedelta(minutes=10 * i)
            name = f"finance.db.{ts.strftime('%Y%m%d_%H%M%S')}"
            path = os.path.join(self.backup_dir, name)
            with open(path, "w") as f:
                f.write(f"backup-{i}")
        _rotate_backups(self.backup_dir, "finance.db", now=now)
        self.assertEqual(len(os.listdir(self.backup_dir)), 5)

    def test_tiered_collapses_same_day(self):
        """Multiple backups on the same day (>1h ago) collapse to 1."""
        os.makedirs(self.backup_dir, exist_ok=True)
        now = datetime(2026, 4, 14, 18, 0, 0)
        # 3 backups from yesterday morning (all >1h old, same day)
        for hour in [9, 10, 11]:
            ts = datetime(2026, 4, 13, hour, 0, 0)
            name = f"finance.db.{ts.strftime('%Y%m%d_%H%M%S')}"
            with open(os.path.join(self.backup_dir, name), "w") as f:
                f.write(f"backup-{hour}")
        _rotate_backups(self.backup_dir, "finance.db", now=now)
        remaining = sorted(os.listdir(self.backup_dir))
        # Only the latest from that day survives
        self.assertEqual(len(remaining), 1)
        self.assertIn("110000", remaining[0])

    def test_tiered_collapses_same_week(self):
        """Multiple backups from the same week (>7d ago) collapse to 1."""
        os.makedirs(self.backup_dir, exist_ok=True)
        now = datetime(2026, 4, 14, 18, 0, 0)
        # 3 backups from 10 days ago (same ISO week, >7d old)
        for hour in [9, 12, 15]:
            ts = datetime(2026, 4, 3, hour, 0, 0)
            name = f"finance.db.{ts.strftime('%Y%m%d_%H%M%S')}"
            with open(os.path.join(self.backup_dir, name), "w") as f:
                f.write(f"backup-{hour}")
        _rotate_backups(self.backup_dir, "finance.db", now=now)
        remaining = sorted(os.listdir(self.backup_dir))
        self.assertEqual(len(remaining), 1)
        self.assertIn("150000", remaining[0])

    def test_tiered_mixed_ages(self):
        """Full scenario: recent + daily + weekly backups."""
        os.makedirs(self.backup_dir, exist_ok=True)
        now = datetime(2026, 4, 14, 18, 0, 0)
        timestamps = [
            # Last hour — all kept (2)
            now - timedelta(minutes=5),
            now - timedelta(minutes=30),
            # Yesterday — 3 backups, collapse to 1
            datetime(2026, 4, 13, 9, 0, 0),
            datetime(2026, 4, 13, 14, 0, 0),
            datetime(2026, 4, 13, 17, 0, 0),
            # 3 days ago — 1 backup, kept
            datetime(2026, 4, 11, 10, 0, 0),
            # 10 days ago — 2 backups same week, collapse to 1
            datetime(2026, 4, 4, 8, 0, 0),
            datetime(2026, 4, 3, 8, 0, 0),
        ]
        for ts in timestamps:
            name = f"finance.db.{ts.strftime('%Y%m%d_%H%M%S')}"
            with open(os.path.join(self.backup_dir, name), "w") as f:
                f.write(ts.isoformat())
        _rotate_backups(self.backup_dir, "finance.db", now=now)
        remaining = sorted(os.listdir(self.backup_dir))
        # 2 (hour) + 1 (yesterday) + 1 (3d ago) + 1 (week) = 5
        self.assertEqual(len(remaining), 5)

    def test_checkpoint_wal(self):
        # After checkpoint, WAL file should be empty or gone
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO t VALUES (2)")
        conn.commit()
        conn.close()
        checkpoint_wal(self.db_path)
        wal_path = self.db_path + "-wal"
        if os.path.exists(wal_path):
            self.assertEqual(os.path.getsize(wal_path), 0)


class TestSchemaVersionGuard(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_guard_passes_when_current(self):
        from housebook.migrations.runner import run_migrations

        run_migrations(self.db_path, verbose=False)
        db = Database(self.db_path)
        # Should not raise
        db.verify_schema_version()

    def test_guard_rejects_newer_db(self):
        from housebook.migrations.runner import run_migrations

        run_migrations(self.db_path, verbose=False)
        # Simulate a future migration applied by a newer tool
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO schema_version (version, name) "
            "VALUES (999, 'future')"
        )
        conn.commit()
        conn.close()

        db = Database(self.db_path)
        with self.assertRaises(SchemaTooNewError):
            db.verify_schema_version()


class TestBulkUpdateDryRun(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        from housebook.migrations.runner import run_migrations

        run_migrations(self.db_path, verbose=False)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_bulk_update_respects_dry_run(self):
        # Insert a transaction
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, needs_review, profile) "
            "VALUES ('2026-01-01', 'Test', '-10.00', "
            "'Miscellaneous', 'test', 'UNVERIFIED', 1, 'test_profile')"
        )
        conn.commit()
        tx_id = conn.execute(
            "SELECT id FROM transactions"
        ).fetchone()[0]
        conn.close()

        dry_db = Database(self.db_path, dry_run=True)
        dry_db.bulk_update_transactions([
            {"id": tx_id, "category": "Food",
             "trip_id": None, "needs_review": 0},
        ])

        # Category should remain unchanged
        conn = sqlite3.connect(self.db_path)
        cat = conn.execute(
            "SELECT category FROM transactions WHERE id = ?",
            (tx_id,),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(cat, "Miscellaneous")


if __name__ == "__main__":
    unittest.main()
