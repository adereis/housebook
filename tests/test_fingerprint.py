import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from housebook.core.database import Database
from housebook.core.models import Transaction


class TestTransactionFingerprint(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.db = Database(self.db_path)
        self._init_schema()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _init_schema(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE, description TEXT,
            amount REAL, category TEXT,
            source TEXT, status TEXT,
            original_file TEXT,
            trip_id INTEGER, needs_review BOOLEAN,
            profile TEXT, metadata TEXT
        )""")
        c.execute("""CREATE TABLE processed_files (
            file_path TEXT PRIMARY KEY,
            file_hash TEXT,
            last_processed TIMESTAMP,
            statement_start DATE,
            statement_end DATE
        )""")
        c.execute("""CREATE TABLE ingestion_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT, line_number INTEGER,
            raw_text TEXT, error TEXT,
            timestamp DATETIME
        )""")
        conn.commit()
        conn.close()

    def _add(self, desc, date, amount, source):
        self.db.add_transaction(Transaction(
            date=date, description=desc,
            amount=Decimal(amount), category="Test",
            source=source, status="UNVERIFIED",
            original_file="test.csv",
        ))

    def test_same_source_detected_as_duplicate(self):
        self._add("STARBUCKS", "2025-01-01", "5.75", "Amex")
        self.assertTrue(self.db.transaction_exists(
            "STARBUCKS", "2025-01-01",
            Decimal("5.75"), "Amex",
        ))

    def test_different_source_not_duplicate(self):
        """Two coffees at Starbucks on same day from
        different banks should both be accepted."""
        self._add("STARBUCKS", "2025-01-01", "5.75", "Amex")
        self.assertFalse(self.db.transaction_exists(
            "STARBUCKS", "2025-01-01",
            Decimal("5.75"), "Chase",
        ))

    def test_max_duplicates_allows_repeats(self):
        """With max_duplicates=2, the first two are OK,
        third is rejected."""
        self._add("STARBUCKS", "2025-01-01", "5.75", "Amex")

        # First copy exists, but max_duplicates=2 means
        # we need 2 before rejecting
        self.assertFalse(self.db.transaction_exists(
            "STARBUCKS", "2025-01-01",
            Decimal("5.75"), "Amex",
            max_duplicates=2,
        ))

        # Add second copy
        self._add("STARBUCKS", "2025-01-01", "5.75", "Amex")

        # Now at 2, should reject
        self.assertTrue(self.db.transaction_exists(
            "STARBUCKS", "2025-01-01",
            Decimal("5.75"), "Amex",
            max_duplicates=2,
        ))

    def test_default_rejects_second_occurrence(self):
        """Default max_duplicates=1 rejects duplicates."""
        self._add("GAS STATION", "2025-01-01", "42.00", "BoA")
        self.assertTrue(self.db.transaction_exists(
            "GAS STATION", "2025-01-01",
            Decimal("42.00"), "BoA",
        ))

    def test_profile_distinguishes_duplicates(self):
        """Same item/day/amount under different profiles are distinct
        when profile is supplied; omitting profile keeps legacy
        (profile-agnostic) behavior."""
        self.db.add_transaction(Transaction(
            date="2025-01-01", description="USB CABLE",
            amount=Decimal("12.99"), category="Test",
            source="Amazon", status="UNVERIFIED",
            original_file="a.csv", profile="sterling",
        ))
        # Same profile -> duplicate
        self.assertTrue(self.db.transaction_exists(
            "USB CABLE", "2025-01-01", Decimal("12.99"),
            "Amazon", profile="sterling",
        ))
        # Different profile -> not a duplicate
        self.assertFalse(self.db.transaction_exists(
            "USB CABLE", "2025-01-01", Decimal("12.99"),
            "Amazon", profile="penny",
        ))
        # No profile arg -> legacy behavior, still a duplicate
        self.assertTrue(self.db.transaction_exists(
            "USB CABLE", "2025-01-01", Decimal("12.99"), "Amazon",
        ))

    def test_different_amount_not_duplicate(self):
        self._add("STARBUCKS", "2025-01-01", "5.75", "Amex")
        self.assertFalse(self.db.transaction_exists(
            "STARBUCKS", "2025-01-01",
            Decimal("4.50"), "Amex",
        ))

    def test_different_date_not_duplicate(self):
        self._add("STARBUCKS", "2025-01-01", "5.75", "Amex")
        self.assertFalse(self.db.transaction_exists(
            "STARBUCKS", "2025-01-02",
            Decimal("5.75"), "Amex",
        ))


if __name__ == "__main__":
    unittest.main()
