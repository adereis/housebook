import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from housebook.demo_seed import (
    seed_db,
    setup_workspace,
)


class TestDemoSeed(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory to act as the root for the test
        self.test_root = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.test_root)

        # Override constants in the module for testing
        self.demo_workspace = "test-demo-workspace"
        self.db_name = "test-finance-demo.db"

        # Patching the constants in the module
        self.patcher1 = patch(
            "housebook.demo_seed.DEMO_WORKSPACE", self.demo_workspace
        )
        self.patcher2 = patch("housebook.demo_seed.DB_NAME", self.db_name)
        self.patcher1.start()
        self.patcher2.start()

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.test_root)
        self.patcher1.stop()
        self.patcher2.stop()

    def test_isolation_no_leakage(self):
        """Verify that running seed doesn't create files outside the demo workspace."""
        setup_workspace()
        seed_db()

        # Check that only the demo workspace exists in the test root
        items = os.listdir(".")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0], self.demo_workspace)

        # Verify no 'finance.db' was created in the root
        self.assertFalse(os.path.exists("finance.db"))

    def test_seed_is_idempotent(self):
        """Re-running seed_db skips seeding if the DB already exists."""
        setup_workspace()
        seed_db()

        db_path = os.path.join(self.demo_workspace, "data", self.db_name)
        mtime_after_first = os.path.getmtime(db_path)

        seed_db()  # second call — must not modify the DB

        mtime_after_second = os.path.getmtime(db_path)
        self.assertEqual(mtime_after_first, mtime_after_second)

    def test_workspace_structure(self):
        """Verify the demo workspace has the correct directory structure."""
        setup_workspace()

        base = self.demo_workspace
        self.assertTrue(os.path.isdir(os.path.join(base, "data")))
        self.assertTrue(os.path.isdir(os.path.join(base, "config")))
        self.assertTrue(os.path.isdir(os.path.join(base, "input")))

        profile_path = os.path.join(base, "config", "user_profile.json")
        self.assertTrue(os.path.exists(profile_path))

        with open(profile_path, "r") as f:
            profile = json.load(f)
            self.assertEqual(profile["user_name"], "Sterling Ledger")
            self.assertIn("Penny", profile["household_members"])

    def test_database_content(self):
        """Verify the seeded database contains the expected Ledger family data."""
        setup_workspace()
        seed_db()

        db_path = os.path.join(self.demo_workspace, "data", self.db_name)
        self.assertTrue(os.path.exists(db_path))

        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # 1. Check Transactions
        c.execute("SELECT COUNT(*) FROM transactions")
        count = c.fetchone()[0]
        self.assertGreater(count, 2000)  # Should be around 2100+

        # 2. Check Trips (The Pun Trips)
        c.execute("SELECT name FROM trips WHERE name LIKE '%Bull Market%'")
        self.assertIsNotNone(c.fetchone())

        c.execute("SELECT name FROM trips WHERE name LIKE '%Dividend Discovery%'")
        self.assertIsNotNone(c.fetchone())

        # 3. Check Recent Transactions Status
        today = date(2026, 4, 3)
        recent_cutoff = (today - timedelta(days=30)).isoformat()

        c.execute(
            "SELECT status, needs_review FROM transactions WHERE date > ?",
            (recent_cutoff,),
        )
        recent_txs = c.fetchall()
        for status, needs_review in recent_txs:
            self.assertEqual(status, "UNVERIFIED")
            self.assertEqual(needs_review, 1)

        # 4. Check Older Transactions Status
        c.execute(
            "SELECT status, needs_review FROM transactions"
            " WHERE date < '2026-01-01' LIMIT 100"
        )
        old_txs = c.fetchall()
        for status, needs_review in old_txs:
            # Most should be verified, except maybe some edge cases if we added any
            self.assertIn(status, ["AGENT_VERIFIED", "RECONCILED"])
            self.assertEqual(needs_review, 0)

        # 5. Check Manual Expenses
        c.execute(
            "SELECT description FROM manual_expenses"
            " WHERE description = 'House Cleaning'"
        )
        self.assertIsNotNone(c.fetchone())

        # 6. Check Tax Documents
        c.execute("SELECT COUNT(*) FROM tax_documents WHERE tax_year = 2024")
        self.assertEqual(c.fetchone()[0], 2)  # W2 and 1098

        conn.close()

    def test_inflation_scaling(self):
        """Verify that transaction amounts increase over time (inflation)."""
        setup_workspace()
        seed_db()

        db_path = os.path.join(self.demo_workspace, "data", self.db_name)
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # Compare average Grocery spending in 2021 vs 2025
        c.execute(
            "SELECT AVG(amount) FROM transactions "
            "WHERE category = 'Groceries' AND date LIKE '2021%'"
        )
        avg_2021 = c.fetchone()[0]

        c.execute(
            "SELECT AVG(amount) FROM transactions "
            "WHERE category = 'Groceries' AND date LIKE '2025%'"
        )
        avg_2025 = c.fetchone()[0]

        conn.close()

        # 2025 should be significantly higher than 2021 (approx 23% based on our scales)
        self.assertGreater(avg_2025, avg_2021 * 1.15)
        self.assertLess(avg_2025, avg_2021 * 1.35)


if __name__ == "__main__":
    unittest.main()
