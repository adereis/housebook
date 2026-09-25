import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from housebook.core import sidecar
from housebook.core.trip_detector import extract_location_hints
from housebook.demo_seed import (
    ITALY_TRIP,
    MERCHANTS,
    TRIPS,
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


class TestDemoTrips(unittest.TestCase):
    """The demo trips must follow the project's own assignment rules."""

    @classmethod
    def setUpClass(cls):
        cls.test_root = tempfile.mkdtemp()
        cls.old_cwd = os.getcwd()
        os.chdir(cls.test_root)
        setup_workspace()
        seed_db()
        cls.conn = sqlite3.connect(os.path.join(
            "demo-workspace", "data", "finance.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.test_root)

    def _trip(self, name):
        return self.conn.execute(
            "SELECT id, start_date, end_date FROM trips WHERE name = ?",
            (name,),
        ).fetchone()

    def test_home_merchants_are_never_linked_to_a_trip(self):
        """A trip is linked by location, not date overlap alone."""
        home = [m for merchants in MERCHANTS.values() for m, _, _ in merchants]
        linked = self.conn.execute(
            "SELECT description FROM transactions WHERE trip_id IS NOT NULL"
            f" AND description IN ({','.join('?' * len(home))})",
            home,
        ).fetchall()
        self.assertEqual(linked, [])

    def test_no_home_discretionary_spend_while_family_is_away(self):
        _, start, end = self._trip(ITALY_TRIP)
        rows = self.conn.execute(
            "SELECT description FROM transactions"
            " WHERE date BETWEEN ? AND ? AND trip_id IS NULL"
            " AND category IN ('Groceries', 'Dining & Takeout')"
            " AND source != 'Amazon CSV'",
            (start, end),
        ).fetchall()
        self.assertEqual(rows, [])

    def test_vacation_includes_advance_bookings(self):
        trip_id, start, _ = self._trip(ITALY_TRIP)
        early = self.conn.execute(
            "SELECT category FROM transactions"
            " WHERE trip_id = ? AND date < ?",
            (trip_id, start),
        ).fetchall()
        self.assertIn(("Flights",), early)
        self.assertIn(("Lodging",), early)

    def test_foreign_charges_record_original_currency(self):
        trip_id, _, _ = self._trip(ITALY_TRIP)
        rows = self.conn.execute(
            "SELECT amount, metadata FROM transactions"
            " WHERE trip_id = ? AND description LIKE '% IT'",
            (trip_id,),
        ).fetchall()
        self.assertTrue(rows)
        for amount, metadata in rows:
            meta = json.loads(metadata)
            self.assertEqual(meta["foreign_currency"], "EUR")
            self.assertAlmostEqual(
                amount, meta["foreign_amount"] * meta["exchange_rate"],
                places=2)

    def test_trip_descriptions_carry_a_location_hint(self):
        """Charges made abroad end in the country, as detect-trips reads."""
        trip_id, start, end = self._trip(ITALY_TRIP)
        rows = self.conn.execute(
            "SELECT description FROM transactions WHERE trip_id = ?"
            " AND date BETWEEN ? AND ? AND metadata IS NOT NULL",
            (trip_id, start, end),
        ).fetchall()
        self.assertGreater(len(rows), 20)
        hint = extract_location_hints([{"description": d} for d, in rows])
        self.assertEqual(hint, "IT")

    def test_work_trip_charges_are_reimbursable(self):
        for trip in TRIPS:
            if trip["type"] != "Work":
                continue
            cats = self.conn.execute(
                "SELECT DISTINCT category FROM transactions"
                " WHERE trip_id = (SELECT id FROM trips WHERE name = ?)",
                (trip["name"],),
            ).fetchall()
            self.assertEqual(cats, [("Work (Reimbursable)",)], trip["name"])

    def test_visits_to_family_have_no_hotel(self):
        for trip in TRIPS:
            if trip.get("lodging") is not False:
                continue
            n = self.conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE category = 'Lodging'"
                " AND trip_id = (SELECT id FROM trips WHERE name = ?)",
                (trip["name"],),
            ).fetchone()[0]
            self.assertEqual(n, 0, trip["name"])


class TestDemoHsa(unittest.TestCase):
    """The demo shoebox must look like a real import + reconcile."""

    @classmethod
    def setUpClass(cls):
        cls.test_root = tempfile.mkdtemp()
        cls.old_cwd = os.getcwd()
        os.chdir(cls.test_root)
        setup_workspace()
        seed_db()
        cls.workspace = os.path.abspath("demo-workspace")
        cls.conn = sqlite3.connect(os.path.join(
            cls.workspace, "data", "finance.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.test_root)

    def test_every_evidence_level_and_status_is_shown(self):
        levels = {r for r, in self.conn.execute(
            "SELECT DISTINCT evidence_level FROM hsa_expenses")}
        self.assertEqual(levels, {"stub", "weak", "ready", "strong"})
        statuses = {r for r, in self.conn.execute(
            "SELECT DISTINCT status FROM hsa_expenses")}
        self.assertEqual(statuses, {"UNREIMBURSED", "PENDING", "REIMBURSED"})
        excluded = self.conn.execute(
            "SELECT COUNT(*) FROM hsa_expenses"
            " WHERE exclusion_reason IS NOT NULL").fetchone()[0]
        self.assertEqual(excluded, 1)

    def test_documents_match_their_files_and_sidecars(self):
        rows = self.conn.execute(
            "SELECT file_path, file_hash, sidecar_path FROM hsa_documents"
        ).fetchall()
        self.assertGreater(len(rows), 10)
        for file_path, file_hash, sidecar_path in rows:
            pdf = os.path.join(self.workspace, file_path)
            self.assertEqual(sidecar.sha256_file(pdf), file_hash)
            with open(pdf, "rb") as f:
                self.assertTrue(f.read().startswith(b"%PDF-"))
            sc = sidecar.load(os.path.join(self.workspace, sidecar_path))
            self.assertEqual(sc.source, "hsa")
            self.assertEqual(sc.source_file.path, file_path)
            processed = self.conn.execute(
                "SELECT COUNT(*) FROM processed_files WHERE file_path = ?",
                (sidecar_path,)).fetchone()[0]
            self.assertEqual(processed, 1, sidecar_path)

    def test_card_charges_prove_the_linked_expenses(self):
        """The consolidated-payment math proof holds for every charge."""
        rows = self.conn.execute(
            "SELECT t.amount, SUM(e.patient_responsibility), COUNT(*)"
            " FROM hsa_expenses e JOIN transactions t"
            " ON t.id = e.transaction_id GROUP BY t.id").fetchall()
        self.assertTrue(any(n > 1 for _, _, n in rows))
        for charge, total, _ in rows:
            self.assertAlmostEqual(charge, total, places=2)

    def test_only_uncorroborated_rows_need_review(self):
        rows = self.conn.execute(
            "SELECT evidence_level, needs_review, exclusion_reason"
            " FROM hsa_expenses").fetchall()
        for level, needs_review, excluded in rows:
            expected = 0 if excluded or level in ("ready", "strong") else 1
            self.assertEqual(needs_review, expected, level)

    def test_reimbursed_rows_belong_to_a_completed_batch(self):
        rows = self.conn.execute(
            "SELECT e.status, r.status FROM hsa_expenses e"
            " JOIN hsa_reimbursement_items i ON i.expense_id = e.id"
            " JOIN hsa_reimbursements r ON r.id = i.reimbursement_id"
        ).fetchall()
        self.assertIn(("REIMBURSED", "COMPLETED"), rows)
        self.assertIn(("PENDING", "PLANNED"), rows)
        unbatched = self.conn.execute(
            "SELECT COUNT(*) FROM hsa_expenses WHERE status != 'UNREIMBURSED'"
            " AND id NOT IN (SELECT expense_id FROM hsa_reimbursement_items)"
        ).fetchone()[0]
        self.assertEqual(unbatched, 0)


if __name__ == "__main__":
    unittest.main()
