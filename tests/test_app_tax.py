import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class TestAppTaxEndpoints(unittest.TestCase):
    """Tests for tax document PATCH/POST/DELETE endpoints."""

    def setUp(self):
        self.db_fd = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False
        )
        self.db_path = self.db_fd.name
        self.db_fd.close()

        self._init_db()

        # Patch DB_PATH before importing app
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
            trip_id INTEGER,
            linked_transaction_id INTEGER REFERENCES transactions(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tax_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tax_year INTEGER, document_type TEXT,
            issuer TEXT, category TEXT, amount REAL,
            currency TEXT, original_file TEXT,
            status TEXT, needs_review INTEGER DEFAULT 1,
            raw_data TEXT, source_file_path TEXT, sidecar_path TEXT
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
        # Insert a test tax document
        c.execute(
            "INSERT INTO tax_documents "
            "(tax_year, document_type, issuer, category, "
            "amount, currency, original_file, status, "
            "needs_review, raw_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2025, "W2", "Acme Corp", "Income",
                85000.0, "USD", "/fake/w2.pdf",
                "UNVERIFIED", 1,
                json.dumps({"note": "test"}),
            ),
        )
        conn.commit()
        conn.close()

    def test_patch_tax_doc_update_fields(self):
        resp = self.client.patch(
            "/api/tax_docs/1",
            json={
                "amount": 90000.0,
                "category": "Income",
                "status": "USER_VERIFIED",
                "issuer": "Acme Corp LLC",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")

        # Verify persisted
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM tax_documents WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["amount"], 90000.0)
        self.assertEqual(row["status"], "USER_VERIFIED")
        self.assertEqual(row["needs_review"], 0)
        self.assertEqual(row["issuer"], "Acme Corp LLC")

    def test_patch_tax_doc_partial_update(self):
        resp = self.client.patch(
            "/api/tax_docs/1",
            json={"status": "USER_VERIFIED"},
        )
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM tax_documents WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "USER_VERIFIED")
        self.assertEqual(row["needs_review"], 0)
        # Other fields unchanged
        self.assertEqual(row["issuer"], "Acme Corp")

    def test_patch_tax_doc_edit_confirms_document(self):
        resp = self.client.patch(
            "/api/tax_docs/1", json={"category": "Other"},
        )
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT category, status, needs_review "
            "FROM tax_documents WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["category"], "Other")
        self.assertEqual(row["status"], "USER_VERIFIED")
        self.assertEqual(row["needs_review"], 0)

    def test_patch_tax_doc_rejects_off_lifecycle_status(self):
        """The UI may only assert USER_VERIFIED (AGENTS.md contract)."""
        resp = self.client.patch(
            "/api/tax_docs/1",
            json={"status": "AGENT_VERIFIED"},
        )
        self.assertEqual(resp.status_code, 422)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM tax_documents WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "UNVERIFIED")

    def test_patch_tax_doc_no_changes(self):
        resp = self.client.patch(
            "/api/tax_docs/1", json={}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "no_changes")

    def test_toggle_tax_review(self):
        # Turn off review
        resp = self.client.post(
            "/api/tax_docs/1/review",
            json={"needs_review": False},
        )
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT needs_review, status "
            "FROM tax_documents WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["needs_review"], 0)
        self.assertEqual(row["status"], "USER_VERIFIED")

    def test_tax_updates_return_404_for_missing_document(self):
        patch_resp = self.client.patch(
            "/api/tax_docs/9999", json={"category": "Other"},
        )
        review_resp = self.client.post(
            "/api/tax_docs/9999/review",
            json={"needs_review": False},
        )
        self.assertEqual(patch_resp.status_code, 404)
        self.assertEqual(review_resp.status_code, 404)

    def test_delete_tax_doc(self):
        resp = self.client.delete("/api/tax_docs/1")
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM tax_documents WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 0)

    def test_missing_tax_detail_and_delete_return_404(self):
        detail = self.client.get("/api/tax_docs/9999")
        delete = self.client.delete("/api/tax_docs/9999")
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(delete.status_code, 404)

    def test_api_data_includes_needs_review(self):
        resp = self.client.get("/api/data")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("tax_docs", data)
        if data["tax_docs"]:
            self.assertIn(
                "needs_review", data["tax_docs"][0]
            )


if __name__ == "__main__":
    unittest.main()
