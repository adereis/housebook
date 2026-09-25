import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class TestAppDataEndpoint(unittest.TestCase):
    """Tests for GET /api/data: date filtering, trip splits,
    and response shape."""

    def setUp(self):
        self.db_fd = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False
        )
        self.db_path = self.db_fd.name
        self.db_fd.close()

        self._init_db()
        self._seed_data()

        self.patcher = patch(
            "housebook.app.DB_PATH", self.db_path
        )
        self.patcher.start()

        from housebook.app import app
        self.client = TestClient(app)

    def tearDown(self):
        self.patcher.stop()
        import os
        os.unlink(self.db_path)

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, description TEXT, amount REAL,
            category TEXT, source TEXT, status TEXT,
            original_file TEXT, needs_review INTEGER DEFAULT 1,
            trip_id INTEGER, profile TEXT, metadata TEXT,
            source_file_path TEXT, source_file_sha256 TEXT,
            source_page INTEGER, sidecar_path TEXT,
            linked_transaction_id INTEGER REFERENCES transactions(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tax_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tax_year INTEGER, document_type TEXT,
            issuer TEXT, category TEXT, amount REAL,
            currency TEXT, original_file TEXT,
            status TEXT, needs_review INTEGER DEFAULT 1,
            raw_data TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, start_date TEXT, end_date TEXT,
            status TEXT NOT NULL DEFAULT 'confirmed',
            type TEXT NOT NULL DEFAULT 'unknown',
            location TEXT,
            created_by TEXT NOT NULL DEFAULT 'manual'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS categorization_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT, keyword TEXT UNIQUE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ingestion_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT, line_number INTEGER,
            raw_line TEXT, error_message TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS manual_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL, amount REAL NOT NULL,
            category TEXT NOT NULL, start_date DATE NOT NULL,
            end_date DATE, frequency TEXT NOT NULL DEFAULT 'one-time'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        conn.close()

    def _seed_data(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # Two trips: one personal, one work
        c.execute(
            "INSERT INTO trips (name, start_date, end_date, "
            "status, type, location) VALUES "
            "('Hawaii Vacation', '2026-01-10', '2026-01-17', "
            "'confirmed', 'personal', 'Maui, HI')"
        )
        personal_trip_id = c.lastrowid

        c.execute(
            "INSERT INTO trips (name, start_date, end_date, "
            "status, type, location) VALUES "
            "('NYC Conference', '2026-02-05', '2026-02-07', "
            "'confirmed', 'work', 'New York, NY')"
        )
        work_trip_id = c.lastrowid

        # Personal trip transactions
        for desc, amt, date in [
            ("Marriott Maui", -350.00, "2026-01-11"),
            ("Uber Airport", -45.00, "2026-01-10"),
            ("Snorkeling Tour", -120.00, "2026-01-13"),
        ]:
            c.execute(
                "INSERT INTO transactions "
                "(date, description, amount, category, source, "
                "status, needs_review, trip_id) "
                "VALUES (?, ?, ?, 'Travel', 'test', "
                "'VERIFIED', 0, ?)",
                (date, desc, amt, personal_trip_id),
            )

        # Work trip transactions (Work (Reimbursable) category)
        for desc, amt, date in [
            ("Hilton NYC", -280.00, "2026-02-05"),
            ("Conference Dinner", -95.00, "2026-02-06"),
        ]:
            c.execute(
                "INSERT INTO transactions "
                "(date, description, amount, category, source, "
                "status, needs_review, trip_id) "
                "VALUES (?, ?, ?, 'Work (Reimbursable)', "
                "'test', 'VERIFIED', 0, ?)",
                (date, desc, amt, work_trip_id),
            )

        # Untripped transactions across different months
        for desc, amt, date in [
            ("Grocery Store", -85.00, "2025-06-15"),
            ("Electric Bill", -120.00, "2025-11-01"),
            ("Coffee Shop", -5.50, "2026-01-05"),
            ("Gas Station", -42.00, "2026-03-10"),
        ]:
            c.execute(
                "INSERT INTO transactions "
                "(date, description, amount, category, source, "
                "status, needs_review) "
                "VALUES (?, ?, ?, 'Miscellaneous', 'test', "
                "'UNVERIFIED', 1)",
                (date, desc, amt),
            )

        conn.commit()
        conn.close()

    # --- Response shape ---

    def test_response_contains_all_keys(self):
        resp = self.client.get("/api/data")
        data = resp.json()
        for key in [
            "transactions", "categories", "trips",
            "trip_summaries", "work_trip_summaries",
            "tax_docs",
        ]:
            self.assertIn(key, data, f"Missing key: {key}")

    def test_legacy_and_spending_routes_share_projection(self):
        query = "?start_date=2026-01-01&end_date=2026-02-28"
        legacy = self.client.get("/api/data" + query).json()
        spending = self.client.get("/api/spending/data" + query).json()
        self.assertEqual(
            {key: legacy[key] for key in spending},
            spending,
        )

    # --- Date filtering ---

    def test_date_filter_returns_subset(self):
        resp = self.client.get(
            "/api/data?start_date=2026-01-01&end_date=2026-01-31"
        )
        data = resp.json()
        dates = [tx["date"] for tx in data["transactions"]]
        self.assertTrue(all(
            "2026-01-01" <= d <= "2026-01-31" for d in dates
        ))
        # Should include Jan transactions (trip + coffee)
        # but exclude Nov, Jun, Feb, Mar
        self.assertGreater(len(dates), 0)
        self.assertTrue(all(
            d.startswith("2026-01") for d in dates
        ))

    def test_date_filter_excludes_outside_range(self):
        resp = self.client.get(
            "/api/data?start_date=2025-06-01&end_date=2025-06-30"
        )
        data = resp.json()
        descs = [tx["description"] for tx in data["transactions"]]
        self.assertIn("Grocery Store", descs)
        self.assertNotIn("Coffee Shop", descs)
        self.assertNotIn("Gas Station", descs)

    def test_no_date_params_returns_all(self):
        resp = self.client.get("/api/data")
        data = resp.json()
        # All 9 seeded transactions
        self.assertEqual(len(data["transactions"]), 9)

    def test_date_range_requires_both_bounds_in_order(self):
        missing_end = self.client.get(
            "/api/spending/data?start_date=2026-01-01"
        )
        reversed_range = self.client.get(
            "/api/spending/data?start_date=2026-02-01"
            "&end_date=2026-01-01"
        )
        invalid_date = self.client.get(
            "/api/spending/data?start_date=not-a-date"
            "&end_date=2026-01-01"
        )
        self.assertEqual(missing_end.status_code, 422)
        self.assertEqual(reversed_range.status_code, 422)
        self.assertEqual(invalid_date.status_code, 422)

    # --- Trip summary split ---

    def test_personal_trip_in_trip_summaries(self):
        resp = self.client.get("/api/data")
        data = resp.json()
        names = [t["name"] for t in data["trip_summaries"]]
        self.assertIn("Hawaii Vacation", names)
        self.assertNotIn("NYC Conference", names)

    def test_work_trip_in_work_summaries(self):
        resp = self.client.get("/api/data")
        data = resp.json()
        names = [t["name"] for t in data["work_trip_summaries"]]
        self.assertIn("NYC Conference", names)
        self.assertNotIn("Hawaii Vacation", names)

    def test_trip_summary_totals(self):
        resp = self.client.get("/api/data")
        data = resp.json()

        hawaii = next(
            t for t in data["trip_summaries"]
            if t["name"] == "Hawaii Vacation"
        )
        # -350 + -45 + -120 = -515
        self.assertAlmostEqual(hawaii["total"], -515.0)

        nyc = next(
            t for t in data["work_trip_summaries"]
            if t["name"] == "NYC Conference"
        )
        # -280 + -95 = -375
        self.assertAlmostEqual(nyc["total"], -375.0)

    def test_trip_filter_excludes_unrelated_manual_expenses(self):
        conn = sqlite3.connect(self.db_path)
        trip_id = conn.execute(
            "SELECT id FROM trips WHERE name = 'Hawaii Vacation'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO manual_expenses "
            "(description, amount, category, start_date, frequency) "
            "VALUES ('Ledger Housekeeping', 75.00, "
            "'Bills & Utilities', '2026-01-12', 'one-time')"
        )
        conn.commit()
        conn.close()

        data = self.client.get(
            f"/api/spending/data?trip_id={trip_id}"
        ).json()
        self.assertTrue(data["transactions"])
        self.assertTrue(all(
            tx["trip_id"] == trip_id for tx in data["transactions"]
        ))
        self.assertNotIn(
            "Ledger Housekeeping",
            [tx["description"] for tx in data["transactions"]],
        )

    # --- Transactions shape ---

    def test_transaction_has_trip_name(self):
        resp = self.client.get("/api/data")
        data = resp.json()
        maui_tx = next(
            tx for tx in data["transactions"]
            if tx["description"] == "Marriott Maui"
        )
        self.assertEqual(maui_tx["trip_name"], "Hawaii Vacation")

    def test_untripped_transaction_has_null_trip(self):
        resp = self.client.get("/api/data")
        data = resp.json()
        grocery = next(
            tx for tx in data["transactions"]
            if tx["description"] == "Grocery Store"
        )
        self.assertIsNone(grocery["trip_name"])
        self.assertIsNone(grocery["trip_id"])

    def test_missing_transaction_targets_return_404(self):
        detail = self.client.get("/api/transactions/9999/detail")
        trip = self.client.post(
            "/api/transactions/9999/trip", json={"trip_id": None},
        )
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(trip.status_code, 404)

    # --- Linked transactions ---

    def test_linked_transactions_excluded(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, needs_review) "
            "VALUES ('2026-03-01', 'Hotel Charge', -400.00, "
            "'Lodging', 'test', 'VERIFIED', 0)"
        )
        purchase_id = c.lastrowid
        c.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, needs_review) "
            "VALUES ('2026-03-15', 'Hotel Refund', 400.00, "
            "'Transfers & Refunds', 'test', 'VERIFIED', 0)"
        )
        refund_id = c.lastrowid
        c.execute(
            "UPDATE transactions SET linked_transaction_id=? "
            "WHERE id=?", (refund_id, purchase_id))
        c.execute(
            "UPDATE transactions SET linked_transaction_id=? "
            "WHERE id=?", (purchase_id, refund_id))
        conn.commit()
        conn.close()

        resp = self.client.get("/api/data")
        data = resp.json()
        descs = [tx["description"] for tx in data["transactions"]]
        self.assertNotIn("Hotel Charge", descs)
        self.assertNotIn("Hotel Refund", descs)


if __name__ == "__main__":
    unittest.main()
