import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from housebook.migrations.runner import run_migrations


class TestAppSecurityBoundaries(unittest.TestCase):
    """Host validation and workspace document-serving boundaries."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.db_path = self.workspace / "data" / "finance.db"
        self.db_path.parent.mkdir(parents=True)
        run_migrations(str(self.db_path), verbose=False)

        self.statement_path = self.workspace / "cc" / "2026" / "Demo.pdf"
        self.statement_path.parent.mkdir(parents=True)
        self.statement_path.write_bytes(b"%PDF-1.4\n% fictitious demo\n")

        config_dir = self.workspace / "config"
        config_dir.mkdir(parents=True)
        self.config_path = config_dir / "pii-denylist.txt"
        self.config_path.write_text("Sterling Ledger\n", encoding="utf-8")

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, status, "
            "original_file, needs_review, source_file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-01-15",
                "Maple Market",
                42.50,
                "Groceries",
                "Demo Card",
                "UNVERIFIED",
                "cc/2026/Demo.pdf",
                1,
                "cc/2026/Demo.pdf",
            ),
        )
        conn.commit()
        conn.close()

        self.db_patch = patch(
            "housebook.app.DB_PATH", str(self.db_path),
        )
        self.workspace_patch = patch(
            "housebook.app.WORKSPACE_DIR", self.workspace,
        )
        self.db_patch.start()
        self.workspace_patch.start()

        from housebook.app import app

        self.client = TestClient(app)

    def tearDown(self):
        self.workspace_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_rejects_untrusted_host(self):
        response = self.client.get(
            "/api/health", headers={"host": "attacker.example.test"},
        )
        self.assertEqual(response.status_code, 400)

    def test_serves_registered_statement_pdf(self):
        response = self.client.get(
            "/api/workspace/file", params={"path": "cc/2026/Demo.pdf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-1.4"))

    def test_rejects_non_pdf_workspace_file(self):
        response = self.client.get(
            "/api/workspace/file",
            params={"path": "config/pii-denylist.txt"},
        )
        self.assertEqual(response.status_code, 403)

    def test_rejects_unregistered_pdf(self):
        unregistered = self.workspace / "config" / "private.pdf"
        unregistered.write_bytes(b"%PDF-1.4\n% fictitious private file\n")
        response = self.client.get(
            "/api/workspace/file", params={"path": "config/private.pdf"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
