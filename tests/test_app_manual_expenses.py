import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class TestManualExpenseEndpoints(unittest.TestCase):
    """Tests for manual expense CRUD: GET, POST, PUT, DELETE."""

    def setUp(self):
        self.db_fd = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False
        )
        self.db_path = self.db_fd.name
        self.db_fd.close()

        self._init_db()

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
        c.execute("""CREATE TABLE IF NOT EXISTS manual_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL, amount REAL NOT NULL,
            category TEXT NOT NULL, start_date DATE NOT NULL,
            end_date DATE, frequency TEXT NOT NULL DEFAULT 'one-time'
        )""")
        conn.commit()
        conn.close()

    def _seed_expense(self, **overrides):
        defaults = {
            "description": "Verizon Internet",
            "amount": 89.99,
            "category": "Utilities",
            "start_date": "2025-01-23",
            "end_date": None,
            "frequency": "monthly",
        }
        defaults.update(overrides)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO manual_expenses "
            "(description, amount, category, start_date, "
            "end_date, frequency) VALUES (?, ?, ?, ?, ?, ?)",
            (
                defaults["description"],
                defaults["amount"],
                defaults["category"],
                defaults["start_date"],
                defaults["end_date"],
                defaults["frequency"],
            ),
        )
        conn.commit()
        row_id = c.lastrowid
        conn.close()
        return row_id

    # -- GET --

    def test_list_empty(self):
        resp = self.client.get("/api/manual_expenses")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_returns_seeded(self):
        self._seed_expense()
        resp = self.client.get("/api/manual_expenses")
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["description"], "Verizon Internet")
        self.assertEqual(data[0]["frequency"], "monthly")

    # -- POST --

    def test_create(self):
        payload = {
            "description": "Gym Membership",
            "amount": 45.00,
            "category": "Health",
            "start_date": "2025-06-01",
            "frequency": "monthly",
        }
        resp = self.client.post(
            "/api/manual_expenses", json=payload
        )
        self.assertEqual(resp.status_code, 200)

        expenses = self.client.get("/api/manual_expenses").json()
        self.assertEqual(len(expenses), 1)
        self.assertEqual(expenses[0]["description"], "Gym Membership")
        self.assertAlmostEqual(expenses[0]["amount"], 45.00)

    # -- PUT --

    def test_update_all_fields(self):
        exp_id = self._seed_expense()
        payload = {
            "description": "Verizon 5G Home",
            "amount": 99.99,
            "category": "Internet",
            "start_date": "2025-02-01",
            "end_date": "2026-12-31",
            "frequency": "monthly",
        }
        resp = self.client.put(
            f"/api/manual_expenses/{exp_id}", json=payload
        )
        self.assertEqual(resp.status_code, 200)

        expenses = self.client.get("/api/manual_expenses").json()
        self.assertEqual(len(expenses), 1)
        exp = expenses[0]
        self.assertEqual(exp["description"], "Verizon 5G Home")
        self.assertAlmostEqual(exp["amount"], 99.99)
        self.assertEqual(exp["category"], "Internet")
        self.assertEqual(exp["start_date"], "2025-02-01")
        self.assertEqual(exp["end_date"], "2026-12-31")

    def test_update_frequency(self):
        exp_id = self._seed_expense(frequency="monthly")
        payload = {
            "description": "Verizon Internet",
            "amount": 89.99,
            "category": "Utilities",
            "start_date": "2025-01-23",
            "frequency": "yearly",
        }
        resp = self.client.put(
            f"/api/manual_expenses/{exp_id}", json=payload
        )
        self.assertEqual(resp.status_code, 200)

        exp = self.client.get("/api/manual_expenses").json()[0]
        self.assertEqual(exp["frequency"], "yearly")

    def test_update_nonexistent_returns_404(self):
        payload = {
            "description": "Ghost",
            "amount": 1.00,
            "category": "None",
            "start_date": "2025-01-01",
            "frequency": "one-time",
        }
        resp = self.client.put(
            "/api/manual_expenses/9999", json=payload
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(
            self.client.get("/api/manual_expenses").json(), []
        )

    # -- DELETE --

    def test_delete_removes_row(self):
        exp_id = self._seed_expense()
        resp = self.client.delete(
            f"/api/manual_expenses/{exp_id}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.client.get("/api/manual_expenses").json(), []
        )

    def test_delete_only_target(self):
        id1 = self._seed_expense(description="Expense A")
        self._seed_expense(description="Expense B")
        self.client.delete(f"/api/manual_expenses/{id1}")

        expenses = self.client.get("/api/manual_expenses").json()
        self.assertEqual(len(expenses), 1)
        self.assertEqual(expenses[0]["description"], "Expense B")

    def test_delete_nonexistent_returns_404(self):
        response = self.client.delete("/api/manual_expenses/9999")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
