"""Tests for the HSA Shoebox module.

Covers: migration schema, ingestor, scanner, CLI, and app endpoints.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from housebook.core.database import Database
from housebook.core.models import HsaDocument, HsaExpense
from housebook.hsa.ingestor import HsaIngestor
from housebook.hsa.scanner import scan_cc_transactions


def _wrap_in_envelope(meta: dict, pdf_path: str) -> dict:
    """Wrap a test fixture's ``data`` block in the v1 sidecar envelope.

    Tests historically wrote the HSA-specific fields (date, entity,
    doc_type, ...) directly at the top of the sidecar. The ingestor
    now requires the unified envelope, so all tests funnel through
    this helper to add the wrapper without rewriting every fixture.
    """
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
        size = os.path.getsize(pdf_path)
    else:
        sha256 = "0" * 64
        size = 0
    return {
        "schema_version": "1",
        "source": "hsa",
        "source_file": {
            "path": os.path.basename(pdf_path),
            "sha256": sha256,
            "size_bytes": size,
            "mime_type": "application/pdf",
        },
        "classified_at": "2026-01-01T00:00:00Z",
        "classified_by": "test",
        "classifier_notes": None,
        "data": meta,
    }


def _create_hsa_schema(db_path):
    """Create the minimal schema needed for HSA tests."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS processed_files (
        file_path TEXT PRIMARY KEY,
        file_hash TEXT,
        last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        statement_start DATE,
        statement_end DATE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, description TEXT, amount REAL,
        category TEXT, source TEXT, status TEXT,
        original_file TEXT, needs_review INTEGER DEFAULT 1,
        trip_id INTEGER, profile TEXT, metadata TEXT,
        linked_transaction_id INTEGER,
        source_file_path TEXT,
        source_file_sha256 TEXT,
        source_page INTEGER,
        sidecar_path TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ingestion_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT, line_number INTEGER,
        raw_text TEXT, error TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hsa_expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service_date DATE, provider TEXT, patient TEXT,
        description TEXT, amount_billed REAL,
        insurance_paid REAL DEFAULT 0,
        patient_responsibility REAL, category TEXT,
        payment_method TEXT, payment_date DATE,
        transaction_id INTEGER REFERENCES transactions(id),
        source TEXT DEFAULT 'manual',
        status TEXT DEFAULT 'UNREIMBURSED',
        needs_review BOOLEAN DEFAULT 1,
        -- Matches migration 013: this DEFAULT is the retired
        -- pre-014 level. Declaring 'stub' here (as this fixture used
        -- to) masked a real bug — the CC-stub scanner omitted the
        -- column and every production stub got 'unverified'. Writers
        -- must set evidence_level explicitly; tests must see the same
        -- default production has.
        evidence_level TEXT DEFAULT 'unverified',
        notes TEXT,
        exclusion_reason TEXT DEFAULT NULL,
        payment_plan_id INTEGER REFERENCES hsa_payment_plans(id),
        plan_role TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hsa_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_id INTEGER REFERENCES hsa_expenses(id),
        document_type TEXT, file_path TEXT,
        file_hash TEXT NOT NULL, original_filename TEXT,
        raw_data TEXT,
        source_page INTEGER,
        sidecar_path TEXT,
        ingested_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hsa_providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name TEXT UNIQUE NOT NULL,
        category TEXT, aliases TEXT DEFAULT '[]'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hsa_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name TEXT NOT NULL, record_id INTEGER NOT NULL,
        field_name TEXT NOT NULL, old_value TEXT,
        new_value TEXT, changed_by TEXT DEFAULT 'system',
        changed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        reason TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hsa_payment_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        total_liability REAL NOT NULL,
        notes TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hsa_reimbursements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reimbursement_date DATE, total_amount REAL,
        method TEXT, packet_file TEXT,
        status TEXT DEFAULT 'PLANNED', notes TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hsa_reimbursement_items (
        reimbursement_id INTEGER REFERENCES hsa_reimbursements(id),
        expense_id INTEGER REFERENCES hsa_expenses(id),
        PRIMARY KEY (reimbursement_id, expense_id)
    )""")
    conn.commit()
    conn.close()


class TestHsaMigration(unittest.TestCase):
    """Verify migration 012 creates all HSA tables."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_migration_creates_hsa_tables(self):
        from housebook.migrations.runner import (
            run_migrations,
        )

        run_migrations(self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()

        expected = {
            "hsa_expenses",
            "hsa_documents",
            "hsa_providers",
            "hsa_audit_log",
            "hsa_reimbursements",
            "hsa_reimbursement_items",
        }
        self.assertTrue(
            expected.issubset(tables),
            f"Missing tables: {expected - tables}",
        )

    def test_migration_creates_indexes(self):
        from housebook.migrations.runner import (
            run_migrations,
        )

        run_migrations(self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'idx_hsa%'"
            ).fetchall()
        }
        conn.close()

        expected = {
            "idx_hsa_expenses_status",
            "idx_hsa_expenses_service_date",
            "idx_hsa_expenses_transaction_id",
            "idx_hsa_documents_expense_id",
            "idx_hsa_audit_log_record",
        }
        self.assertTrue(
            expected.issubset(indexes),
            f"Missing indexes: {expected - indexes}",
        )


class TestHsaIngestor(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self.db = Database(self.db_path)

        self.providers_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        )
        json.dump(
            {
                "providers": [
                    {
                        "canonical_name": "Maple Health Center",
                        "category": "medical",
                        "aliases": ["MHC", "MAPLE HEALTH CTR"],
                    },
                ],
            },
            self.providers_file,
        )
        self.providers_file.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        os.unlink(self.providers_file.name)

    def _make_ingestor(self):
        with patch(
            "housebook.hsa.providers.HSA_PROVIDERS_JSON",
            self.providers_file.name,
        ):
            return HsaIngestor(self.db, None)

    def test_provider_resolution_exact(self):
        ing = self._make_ingestor()
        self.assertEqual(
            ing.resolve_provider("MHC"),
            "Maple Health Center",
        )

    def test_provider_resolution_partial(self):
        ing = self._make_ingestor()
        self.assertEqual(
            ing.resolve_provider("MAPLE HEALTH CTR"),
            "Maple Health Center",
        )

    def test_provider_config_is_loaded_once_and_reused(self):
        from housebook.hsa.providers import ProviderResolver

        resolver = ProviderResolver(self.providers_file.name)
        with open(self.providers_file.name, "w") as f:
            json.dump({"providers": []}, f)

        self.assertEqual(
            resolver.resolve("MAPLE HEALTH CTR"),
            "Maple Health Center",
        )
        self.assertEqual(
            resolver.get_config("Maple Health Center")["category"],
            "medical",
        )

    def test_provider_resolution_unknown(self):
        ing = self._make_ingestor()
        self.assertEqual(
            ing.resolve_provider("Some Unknown Clinic"),
            "Some Unknown Clinic",
        )

    def test_detect_category(self):
        ing = self._make_ingestor()
        self.assertEqual(
            ing._detect_category("DENTAL CLINIC visit", ""),
            "dental",
        )
        self.assertEqual(
            ing._detect_category("general checkup", ""),
            "medical",
        )

    def test_validate_directory_finds_orphans(self):
        ing = self._make_ingestor()
        tmpdir = tempfile.mkdtemp()
        year_dir = os.path.join(tmpdir, "2025")
        os.makedirs(year_dir)

        # PDF with sidecar — OK
        with open(os.path.join(year_dir, "a.pdf"), "w") as f:
            f.write("pdf")
        with open(os.path.join(year_dir, "a.json"), "w") as f:
            f.write("{}")

        # PDF without sidecar — orphan
        with open(os.path.join(year_dir, "b.pdf"), "w") as f:
            f.write("pdf")

        errors = ing.validate_directory(tmpdir)
        self.assertEqual(len(errors), 1)
        self.assertIn("b.pdf", errors[0])

        import shutil

        shutil.rmtree(tmpdir)

    def test_save_to_db(self):
        ing = self._make_ingestor()

        expense = HsaExpense(
            service_date="2025-03-15",
            provider="Test Clinic",
            patient="self",
            description="Office visit",
            patient_responsibility=Decimal("50.00"),
            category="medical",
            source="receipt",
        )
        doc = HsaDocument(
            expense_id=None,
            document_type="receipt",
            file_path="hsa/2025/test.pdf",
            file_hash="abc123",
            original_filename="test.pdf",
        )

        ing._save_to_db(expense, doc)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("SELECT * FROM hsa_expenses").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "Test Clinic")
        self.assertEqual(
            rows[0]["patient_responsibility"],
            50.00,
        )
        self.assertEqual(rows[0]["status"], "UNREIMBURSED")

        docs = conn.execute("SELECT * FROM hsa_documents").fetchall()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["file_hash"], "abc123")
        self.assertEqual(docs[0]["expense_id"], rows[0]["id"])
        conn.close()

    def test_dry_run_does_not_write(self):
        self.db.dry_run = True
        ing = self._make_ingestor()

        expense = HsaExpense(
            service_date="2025-03-15",
            provider="Test",
            patient="self",
            description="Test",
            patient_responsibility=Decimal("10.00"),
            category="medical",
            source="receipt",
        )
        doc = HsaDocument(
            expense_id=None,
            document_type="receipt",
            file_path="test.pdf",
            file_hash="xyz",
            original_filename="test.pdf",
        )

        ing._save_to_db(expense, doc)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


class TestHsaScanner(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self._seed_transactions()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _seed_transactions(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-03-15",
                "RIVERSIDE HOSPITAL",
                150.00,
                "Health & Medical",
                "Amex",
                "AGENT_VERIFIED",
                "stmt.pdf",
                0,
            ),
        )
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-04-01",
                "CVS/PHARMACY #1234",
                27.00,
                "Pharmacy",
                "BoA",
                "AGENT_VERIFIED",
                "stmt2.pdf",
                0,
            ),
        )
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-04-05",
                "STARBUCKS",
                5.00,
                "Dining & Takeout",
                "Amex",
                "AGENT_VERIFIED",
                "stmt.pdf",
                0,
            ),
        )
        # Negative amount should be excluded
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-04-10",
                "HOSPITAL REFUND",
                -50.00,
                "Health & Medical",
                "Amex",
                "AGENT_VERIFIED",
                "stmt.pdf",
                0,
            ),
        )
        conn.commit()
        conn.close()

    def test_scan_finds_medical_transactions(self):
        stubs = scan_cc_transactions(
            db_path=self.db_path,
            dry_run=True,
        )
        self.assertEqual(len(stubs), 2)
        providers = {s["provider"] for s in stubs}
        self.assertIn("RIVERSIDE HOSPITAL", providers)
        self.assertIn("CVS/PHARMACY #1234", providers)

    def test_scan_creates_stubs(self):
        stubs = scan_cc_transactions(
            db_path=self.db_path,
            dry_run=False,
        )
        self.assertEqual(len(stubs), 2)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_scan_deduplicates(self):
        scan_cc_transactions(
            db_path=self.db_path,
            dry_run=False,
        )
        stubs2 = scan_cc_transactions(
            db_path=self.db_path,
            dry_run=False,
        )
        self.assertEqual(len(stubs2), 0)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_scan_excludes_negative_amounts(self):
        stubs = scan_cc_transactions(
            db_path=self.db_path,
            dry_run=True,
        )
        amounts = [s["amount"] for s in stubs]
        self.assertTrue(all(a > 0 for a in amounts))

    def test_scan_assigns_categories(self):
        stubs = scan_cc_transactions(
            db_path=self.db_path,
            dry_run=True,
        )
        by_provider = {s["provider"]: s for s in stubs}
        self.assertEqual(
            by_provider["CVS/PHARMACY #1234"]["category"],
            "pharmacy",
        )
        self.assertEqual(
            by_provider["RIVERSIDE HOSPITAL"]["category"],
            "medical",
        )

    def test_scan_keyword_detection(self):
        """Transactions with medical keywords but non-medical
        category should still be detected."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-05-01",
                "DENTAL ASSOCIATES",
                200.00,
                "Miscellaneous",
                "Amex",
                "AGENT_VERIFIED",
                "stmt.pdf",
                0,
            ),
        )
        conn.commit()
        conn.close()

        stubs = scan_cc_transactions(
            db_path=self.db_path,
            dry_run=True,
        )
        providers = {s["provider"] for s in stubs}
        self.assertIn("DENTAL ASSOCIATES", providers)


    def test_scan_excludes_amazon_transactions(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-06-01",
                "First Aid Kit",
                30.00,
                "Health & Medical",
                "Amazon",
                "AGENT_VERIFIED",
                "orders.csv",
                0,
            ),
        )
        conn.commit()
        conn.close()

        stubs = scan_cc_transactions(
            db_path=self.db_path,
            dry_run=True,
        )
        providers = {s["provider"] for s in stubs}
        self.assertNotIn("First Aid Kit", providers)

    def test_scan_workspace_medical_keywords(self):
        """A regional network with no generic word in its descriptor
        is found only through the workspace's `medical_keywords`."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-07-01",
                "LAKESIDE HEALTHCARE",
                90.00,
                "Miscellaneous",
                "Amex",
                "AGENT_VERIFIED",
                "stmt.pdf",
                0,
            ),
        )
        conn.commit()
        conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "scanner.json")
            with patch(
                "housebook.hsa.scanner.HSA_SCANNER_JSON", config_path,
            ):
                stubs = scan_cc_transactions(
                    db_path=self.db_path, dry_run=True,
                )
                self.assertNotIn(
                    "LAKESIDE HEALTHCARE", {s["provider"] for s in stubs},
                )

                with open(config_path, "w") as f:
                    json.dump({"medical_keywords": ["Lakeside Health"]}, f)
                stubs = scan_cc_transactions(
                    db_path=self.db_path, dry_run=True,
                )
                self.assertIn(
                    "LAKESIDE HEALTHCARE", {s["provider"] for s in stubs},
                )


class TestHsaCli(unittest.TestCase):
    """Test CLI commands via direct function calls."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self._seed_expenses()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _seed_expenses(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient, description, "
            "patient_responsibility, category, source, "
            "status, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-03-15",
                "Test Clinic",
                "self",
                "Office visit",
                50.00,
                "medical",
                "receipt",
                "UNREIMBURSED",
                1,
            ),
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient, description, "
            "patient_responsibility, category, source, "
            "status, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-04-01",
                "Eye Doctor",
                "spouse",
                "Eye exam",
                120.00,
                "vision",
                "receipt",
                "UNREIMBURSED",
                0,
            ),
        )
        conn.commit()
        conn.close()

    def test_verify_clears_needs_review(self):
        from housebook.hsa.cli import cmd_verify

        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "ids": [1],
                "category": None,
                "patient": None,
                "provider": None,
                "evidence_level": None,
                "transaction_id": None,
                "notes": None,
                "payment_method": None,
                "payment_date": None,
            },
        )()

        cmd_verify(args)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT needs_review FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["needs_review"], 0)

    def test_verify_updates_category(self):
        from housebook.hsa.cli import cmd_verify

        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "ids": [1],
                "category": "dental",
                "patient": None,
                "provider": None,
                "evidence_level": None,
                "transaction_id": None,
                "notes": None,
                "payment_method": None,
                "payment_date": None,
            },
        )()

        cmd_verify(args)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT category FROM hsa_expenses WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(row["category"], "dental")

    def test_verify_writes_audit_log(self):
        from housebook.hsa.cli import cmd_verify

        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "ids": [1],
                "category": "dental",
                "patient": None,
                "provider": None,
                "evidence_level": None,
                "transaction_id": None,
                "notes": None,
                "payment_method": None,
                "payment_date": None,
            },
        )()

        cmd_verify(args)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        logs = conn.execute(
            "SELECT * FROM hsa_audit_log WHERE record_id = 1"
        ).fetchall()
        conn.close()

        fields = {log["field_name"] for log in logs}
        self.assertIn("category", fields)
        self.assertIn("needs_review", fields)

    def test_summary_json_output(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_summary

        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "year": None,
                "patient": None,
                "json_output": True,
            },
        )()

        f = io.StringIO()
        with redirect_stdout(f):
            cmd_summary(args)

        output = json.loads(f.getvalue())
        self.assertIn("by_group", output)
        self.assertIn("totals", output)
        self.assertIn("pending_review", output)

    def test_list_json_output(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_list

        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "year": None,
                "status": None,
                "patient": None,
                "needs_review": False,
                "json_output": True,
            },
        )()

        f = io.StringIO()
        with redirect_stdout(f):
            cmd_list(args)

        output = json.loads(f.getvalue())
        self.assertEqual(len(output), 2)


class TestHsaAppEndpoints(unittest.TestCase):
    """Tests for HSA web API endpoints."""

    def setUp(self):
        self.db_fd = tempfile.NamedTemporaryFile(
            suffix=".db",
            delete=False,
        )
        self.db_path = self.db_fd.name
        self.db_fd.close()

        _create_hsa_schema(self.db_path)
        self._seed_data()

        self.patcher = patch(
            "housebook.app.DB_PATH",
            self.db_path,
        )
        self.patcher.start()

        from fastapi.testclient import TestClient

        from housebook.app import app

        self.client = TestClient(app)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _seed_data(self):
        conn = sqlite3.connect(self.db_path)
        # Need supporting tables for app startup
        conn.execute("""CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, start_date TEXT, end_date TEXT,
            status TEXT DEFAULT 'confirmed',
            type TEXT DEFAULT 'unknown', location TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS
            categorization_rules (
            category TEXT, keyword TEXT,
            UNIQUE(category, keyword)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS
            manual_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT, amount REAL, category TEXT,
            start_date TEXT, end_date TEXT, frequency TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS
            schema_version (
            version INTEGER PRIMARY KEY, name TEXT,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute(
            "INSERT INTO schema_version (version, name) "
            "VALUES (13, 'hsa_evidence_level')",
        )

        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient, description, "
            "patient_responsibility, category, source, "
            "status, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-03-15",
                "Test Clinic",
                "self",
                "Office visit",
                50.00,
                "medical",
                "receipt",
                "UNREIMBURSED",
                1,
            ),
        )
        conn.commit()
        conn.close()

    def test_hsa_page_loads(self):
        resp = self.client.get("/hsa")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("HSA Shoebox", resp.text)

    def test_hsa_data_endpoint(self):
        resp = self.client.get("/api/hsa/data")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("expenses", data)
        self.assertIn("summary", data)
        self.assertEqual(len(data["expenses"]), 1)

    def test_hsa_data_filter_by_status(self):
        resp = self.client.get("/api/hsa/data?status=UNREIMBURSED")
        data = resp.json()
        self.assertEqual(len(data["expenses"]), 1)

        resp2 = self.client.get("/api/hsa/data?status=REIMBURSED")
        data2 = resp2.json()
        self.assertEqual(len(data2["expenses"]), 0)

    def test_patch_hsa_expense(self):
        resp = self.client.patch(
            "/api/hsa/1",
            json={"category": "dental", "provider": "New Doc"},
        )
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT category, provider FROM hsa_expenses WHERE id = 1"
        ).fetchone()

        logs = conn.execute(
            "SELECT * FROM hsa_audit_log WHERE record_id = 1"
        ).fetchall()
        conn.close()

        self.assertEqual(row["category"], "dental")
        self.assertEqual(row["provider"], "New Doc")
        self.assertGreaterEqual(len(logs), 2)

    def test_proxy_identity_is_recorded_in_hsa_audit_log(self):
        email = "sterling.ledger@gmail.example.test"
        proxy_secret = "fictitious-proxy-secret-0123456789abcdef"
        headers = {
            "x-housebook-proxy-secret": proxy_secret,
            "x-auth-request-email": email,
            "origin": "https://housebook.example.test",
        }
        environment = {
            "HOUSEBOOK_AUTH_MODE": "proxy",
            "HOUSEBOOK_PROXY_SECRET": proxy_secret,
            "HOUSEBOOK_ALLOWED_ORIGINS": (
                "https://housebook.example.test"
            ),
        }

        with patch.dict(os.environ, environment, clear=True):
            response = self.client.patch(
                "/api/hsa/1",
                json={"category": "dental"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        changed_by = conn.execute(
            "SELECT changed_by FROM hsa_audit_log "
            "WHERE record_id = 1 AND field_name = 'category'",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(changed_by, email)

    def test_toggle_review(self):
        resp = self.client.post(
            "/api/hsa/1/review",
            json={"needs_review": False},
        )
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT needs_review FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["needs_review"], 0)

    def test_delete_hsa_expense_is_soft_delete(self):
        resp = self.client.delete("/api/hsa/1")
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM hsa_expenses WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "DELETED")

        log = conn.execute(
            "SELECT * FROM hsa_audit_log WHERE record_id = 1 AND new_value = 'DELETED'"
        ).fetchone()
        self.assertIsNotNone(log)
        conn.close()

    def test_deleted_expense_hidden_from_api(self):
        self.client.delete("/api/hsa/1")
        resp = self.client.get("/api/hsa/data")
        data = resp.json()
        self.assertEqual(len(data["expenses"]), 0)

    def test_missing_hsa_targets_return_404_without_audit_log(self):
        patch_response = self.client.patch(
            "/api/hsa/9999", json={"category": "dental"},
        )
        review_response = self.client.post(
            "/api/hsa/9999/review", json={"needs_review": False},
        )
        delete_response = self.client.delete("/api/hsa/9999")
        detail_response = self.client.get("/api/hsa/9999/detail")

        self.assertEqual(patch_response.status_code, 404)
        self.assertEqual(review_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)
        self.assertEqual(detail_response.status_code, 404)

        conn = sqlite3.connect(self.db_path)
        ghost_logs = conn.execute(
            "SELECT COUNT(*) FROM hsa_audit_log WHERE record_id = 9999"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(ghost_logs, 0)


class TestHsaSidecarIngestion(unittest.TestCase):
    """Tests for JSON sidecar ingestion path."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self.db = Database(self.db_path)
        self.tmpdir = tempfile.mkdtemp()

        self.providers_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        )
        json.dump({"providers": []}, self.providers_file)
        self.providers_file.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        os.unlink(self.providers_file.name)
        import shutil

        shutil.rmtree(self.tmpdir)

    def _make_ingestor(self):
        with patch(
            "housebook.hsa.providers.HSA_PROVIDERS_JSON",
            self.providers_file.name,
        ):
            return HsaIngestor(self.db, None)

    def _write_sidecar(self, name, meta):
        """Write a JSON sidecar (envelope-wrapped) and dummy PDF."""
        json_path = os.path.join(self.tmpdir, name + ".json")
        pdf_path = os.path.join(self.tmpdir, name + ".pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy content")
        with open(json_path, "w") as f:
            json.dump(_wrap_in_envelope(meta, pdf_path), f)
        return json_path

    def test_ingest_receipt_sidecar(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-03-15__Maple-Dental__REC__Sterling__60.71",
            {
                "date": "2025-03-15",
                "entity": "Maple-Dental",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 60.71,
                "tags": [],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM hsa_expenses").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "Maple Dental")
        self.assertAlmostEqual(
            rows[0]["patient_responsibility"],
            60.71,
        )
        self.assertEqual(rows[0]["source"], "receipt")
        self.assertEqual(rows[0]["patient"], "sterling")
        self.assertEqual(
            rows[0]["evidence_level"],
            "stub",
        )
        self.assertEqual(rows[0]["needs_review"], 1)

        docs = conn.execute("SELECT * FROM hsa_documents").fetchall()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["document_type"], "receipt")
        # Provenance columns (migration 016) populated from envelope:
        #   sidecar_path → workspace-relative path of the .json
        #   file_hash → SHA-256 from envelope.source_file.sha256
        self.assertIsNotNone(docs[0]["sidecar_path"])
        self.assertTrue(
            docs[0]["sidecar_path"].endswith(".json"),
            f"unexpected sidecar_path: {docs[0]['sidecar_path']}",
        )
        self.assertEqual(len(docs[0]["file_hash"]), 64,
                         "file_hash should be sha256 (64 hex chars)")
        conn.close()

    def test_multi_item_failure_rolls_back_and_retries_cleanly(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-03-15__Maple-Dental__REC__Sterling__multi",
            {
                "date": "2025-03-15",
                "entity": "Maple-Dental",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 60.0,
                "tags": [],
                "items": [
                    {"description": "Visit A", "amount": 10.0},
                    {"description": "Visit B", "amount": 20.0},
                    {"description": "Visit C", "amount": "invalid"},
                ],
            },
        )

        with self.assertRaises(ValueError):
            ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM hsa_documents").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            0,
        )
        conn.close()

        with open(jp) as f:
            sidecar = json.load(f)
        sidecar["data"]["items"][2]["amount"] = 30.0
        with open(jp, "w") as f:
            json.dump(sidecar, f)

        ing.ingest_sidecar(jp)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0],
            3,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM hsa_documents").fetchone()[0],
            3,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            1,
        )
        conn.close()

    def test_ingest_directory_counts_new_vs_unchanged(self):
        ing = self._make_ingestor()
        self._write_sidecar(
            "2025-03-15__Maple-Dental__REC__Sterling__60.71",
            {"date": "2025-03-15", "entity": "Maple-Dental",
             "doc_type": "REC", "patient": "Sterling",
             "amount": 60.71, "tags": []},
        )
        self._write_sidecar(
            "2025-04-01__Shield-Lab__REC__Penny__22.00",
            {"date": "2025-04-01", "entity": "Shield-Lab",
             "doc_type": "REC", "patient": "Penny",
             "amount": 22.00, "tags": []},
        )

        # First run: both sidecars are new.
        first = ing.ingest_directory(self.tmpdir)
        self.assertEqual(first["ingested"], 2)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(first["expenses"], 2)
        self.assertEqual(first["errors"], [])

        # Second run: file-level idempotency → all unchanged, no new work.
        second = ing.ingest_directory(self.tmpdir)
        self.assertEqual(second["ingested"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(second["expenses"], 0)

    def test_ingest_eob_with_financials(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-02-19__Shield-Health__EOB__Sterling__95.00__Provider",
            {
                "date": "2025-02-19",
                "entity": "Shield-Health",
                "doc_type": "EOB",
                "patient": "Sterling",
                "amount": 95.00,
                "tags": ["Provider"],
                "claim_id": "1234567890",
                "financials": {
                    "billed": 350.00,
                    "discount": 255.00,
                    "plan_paid": 0.0,
                    "patient_responsibility": 95.00,
                },
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM hsa_expenses").fetchone()
        self.assertAlmostEqual(row["amount_billed"], 350.00)
        self.assertAlmostEqual(row["insurance_paid"], 0.0)
        self.assertAlmostEqual(
            row["patient_responsibility"],
            95.00,
        )
        self.assertEqual(row["source"], "eob")
        self.assertEqual(row["provider"], "Provider")
        self.assertIn("EOB via Shield Health", row["description"])
        conn.close()

    def test_ingest_stmt_creates_document_only(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-12-31__Vault-HSA__STMT__Sterling__0.00__Annual",
            {
                "date": "2025-12-31",
                "entity": "Vault-HSA",
                "doc_type": "STMT",
                "patient": "Sterling",
                "amount": 0.0,
                "tags": ["Annual"],
                "line_items": [],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        expenses = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        docs = conn.execute("SELECT * FROM hsa_documents").fetchall()
        conn.close()

        self.assertEqual(expenses, 0)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0][2], "statement")
        self.assertIsNone(docs[0][1])  # expense_id is NULL

    def test_ingest_tax_creates_document_only(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2024-12-31__Vault-HSA__TAX__Sterling__0.00__5498-SA",
            {
                "date": "2024-12-31",
                "entity": "Vault-HSA",
                "doc_type": "TAX",
                "patient": "Sterling",
                "amount": 0.0,
                "tags": ["5498-SA"],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        expenses = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        docs = conn.execute("SELECT COUNT(*) FROM hsa_documents").fetchone()[0]
        conn.close()

        self.assertEqual(expenses, 0)
        self.assertEqual(docs, 1)

    def test_ingest_hist_creates_document_only(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2026-12-31__Vault-HSA__HIST__Sterling__0.00",
            {
                "date": "2026-12-31",
                "entity": "Vault-HSA",
                "doc_type": "HIST",
                "patient": "Sterling",
                "amount": 0.0,
                "tags": [],
                "line_items": [
                    {
                        "date": "2026-01-29",
                        "status": "Approved",
                        "type": "Credit Card (* 2222)",
                        "amount": 40.00,
                        "reconciled_to": "some_file.pdf",
                    },
                ],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        expenses = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        docs = conn.execute("SELECT COUNT(*) FROM hsa_documents").fetchone()[0]
        conn.close()

        self.assertEqual(expenses, 0)
        self.assertEqual(docs, 1)

    def test_ingest_invoice_creates_expense(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-06-03__Sunrise-Home-Care__INV__Unknown__75.00",
            {
                "date": "2025-06-03",
                "entity": "Sunrise-Home-Care",
                "doc_type": "INV",
                "patient": "Unknown",
                "amount": 75.0,
                "tags": [],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM hsa_expenses").fetchone()
        conn.close()

        self.assertEqual(row["source"], "invoice")
        self.assertEqual(row["patient"], "unknown")

    def test_zero_amount_skips_expense(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-06-03__Sunrise-Home-Care__INV__Unknown__0.00",
            {
                "date": "2025-06-03",
                "entity": "Sunrise-Home-Care",
                "doc_type": "INV",
                "patient": "Unknown",
                "amount": 0.0,
                "tags": [],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        expenses = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        docs = conn.execute("SELECT COUNT(*) FROM hsa_documents").fetchone()[0]
        conn.close()

        self.assertEqual(expenses, 0)
        self.assertEqual(docs, 1)

    def test_dedup_sidecar(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-03-15__Test__REC__Self__10.00",
            {
                "date": "2025-03-15",
                "entity": "Test",
                "doc_type": "REC",
                "patient": "Self",
                "amount": 10.0,
                "tags": [],
            },
        )
        ing.ingest_sidecar(jp)
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_evidence_level_default(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2025-01-01__Doc__REC__Self__99.99",
            {
                "date": "2025-01-01",
                "entity": "Doc",
                "doc_type": "REC",
                "patient": "Self",
                "amount": 99.99,
                "tags": [],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT evidence_level FROM hsa_expenses").fetchone()
        conn.close()
        self.assertEqual(row["evidence_level"], "stub")


class TestHsaCrossDocumentDedup(unittest.TestCase):
    """Verify that multiple documents for the same service
    produce one expense with multiple linked documents."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self.db = Database(self.db_path)
        self.tmpdir = tempfile.mkdtemp()

        self.providers_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        )
        json.dump(
            {
                "providers": [
                    {
                        "canonical_name": "Sunrise Home Care",
                        "category": "medical",
                        "aliases": ["Sunrise-Home-Care", "Sunrise-Care-Inc"],
                    },
                ],
            },
            self.providers_file,
        )
        self.providers_file.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        os.unlink(self.providers_file.name)
        import shutil

        shutil.rmtree(self.tmpdir)

    def _make_ingestor(self):
        with patch(
            "housebook.hsa.providers.HSA_PROVIDERS_JSON",
            self.providers_file.name,
        ):
            return HsaIngestor(self.db, None)

    def _write_sidecar(self, name, meta):
        json_path = os.path.join(self.tmpdir, name + ".json")
        pdf_path = os.path.join(self.tmpdir, name + ".pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy content")
        with open(json_path, "w") as f:
            json.dump(_wrap_in_envelope(meta, pdf_path), f)
        return json_path

    def test_eob_then_invoice_deduplicates(self):
        """EOB + invoice for same service → 1 expense, 2 docs."""
        ing = self._make_ingestor()
        eob = self._write_sidecar(
            "2025-09-25__Shield-Health__EOB__Sterling__36.00__Sunrise-Care-Inc",
            {
                "date": "2025-09-25",
                "entity": "Shield-Health",
                "doc_type": "EOB",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": ["Sunrise-Care-Inc"],
                "financials": {
                    "billed": 100.0,
                    "patient_responsibility": 36.00,
                },
            },
        )
        inv = self._write_sidecar(
            "2025-10-14__Sunrise-Home-Care__INV__Sterling__36.00",
            {
                "date": "2025-10-14",
                "entity": "Sunrise-Home-Care",
                "doc_type": "INV",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        ing.ingest_sidecar(eob)
        ing.ingest_sidecar(inv)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        expenses = conn.execute("SELECT * FROM hsa_expenses").fetchall()
        docs = conn.execute("SELECT * FROM hsa_documents").fetchall()
        conn.close()

        self.assertEqual(len(expenses), 1)
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0]["expense_id"], docs[1]["expense_id"])

    def test_receipt_then_eob_deduplicates(self):
        """Receipt first, then EOB → still 1 expense, 2 docs."""
        ing = self._make_ingestor()
        rec = self._write_sidecar(
            "2025-11-06__Sunrise-Home-Care__REC__Sterling__36.00",
            {
                "date": "2025-11-06",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        eob = self._write_sidecar(
            "2025-09-25__Shield-Health__EOB__Sterling__36.00__Sunrise-Care-Inc",
            {
                "date": "2025-09-25",
                "entity": "Shield-Health",
                "doc_type": "EOB",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": ["Sunrise-Care-Inc"],
                "financials": {
                    "billed": 100.0,
                    "patient_responsibility": 36.00,
                },
            },
        )
        ing.ingest_sidecar(rec)
        ing.ingest_sidecar(eob)

        conn = sqlite3.connect(self.db_path)
        expenses = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        docs = conn.execute("SELECT COUNT(*) FROM hsa_documents").fetchone()[0]
        conn.close()

        self.assertEqual(expenses, 1)
        self.assertEqual(docs, 2)

    def test_full_triad_deduplicates(self):
        """EOB + invoice + receipt → 1 expense, 3 docs."""
        ing = self._make_ingestor()
        eob = self._write_sidecar(
            "2025-09-25__Shield-Health__EOB__Sterling__36.00__Sunrise-Care-Inc",
            {
                "date": "2025-09-25",
                "entity": "Shield-Health",
                "doc_type": "EOB",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": ["Sunrise-Care-Inc"],
                "financials": {
                    "billed": 100.0,
                    "patient_responsibility": 36.00,
                },
            },
        )
        inv = self._write_sidecar(
            "2025-10-14__Sunrise-Home-Care__INV__Sterling__36.00",
            {
                "date": "2025-10-14",
                "entity": "Sunrise-Home-Care",
                "doc_type": "INV",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        rec = self._write_sidecar(
            "2025-11-06__Sunrise-Home-Care__REC__Sterling__36.00",
            {
                "date": "2025-11-06",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        ing.ingest_sidecar(eob)
        ing.ingest_sidecar(inv)
        ing.ingest_sidecar(rec)

        conn = sqlite3.connect(self.db_path)
        expenses = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        docs = conn.execute("SELECT COUNT(*) FROM hsa_documents").fetchone()[0]
        conn.close()

        self.assertEqual(expenses, 1)
        self.assertEqual(docs, 3)

    def test_different_dates_beyond_window_not_deduped(self):
        """Same provider/amount but >45 days apart → 2 expenses."""
        ing = self._make_ingestor()
        s1 = self._write_sidecar(
            "2025-03-15__Sunrise-Home-Care__REC__Sterling__64.50",
            {
                "date": "2025-03-15",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 64.50,
                "tags": [],
            },
        )
        s2 = self._write_sidecar(
            "2025-06-08__Sunrise-Home-Care__REC__Sterling__64.50",
            {
                "date": "2025-06-08",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 64.50,
                "tags": [],
            },
        )
        ing.ingest_sidecar(s1)
        ing.ingest_sidecar(s2)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()

        self.assertEqual(count, 2)

    def test_different_amounts_not_deduped(self):
        """Same provider/patient/date but different amounts."""
        ing = self._make_ingestor()
        s1 = self._write_sidecar(
            "2025-09-25__Sunrise-Home-Care__REC__Sterling__36.00",
            {
                "date": "2025-09-25",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        s2 = self._write_sidecar(
            "2025-09-25__Sunrise-Home-Care__REC__Sterling__95.00",
            {
                "date": "2025-09-25",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 95.00,
                "tags": [],
            },
        )
        ing.ingest_sidecar(s1)
        ing.ingest_sidecar(s2)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()

        self.assertEqual(count, 2)

    def test_different_patients_not_deduped(self):
        """Same provider/amount/date but different patients."""
        ing = self._make_ingestor()
        s1 = self._write_sidecar(
            "2025-09-25__Sunrise-Home-Care__REC__Sterling__36.00",
            {
                "date": "2025-09-25",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        s2 = self._write_sidecar(
            "2025-09-25__Sunrise-Home-Care__REC__Penny__36.00",
            {
                "date": "2025-09-25",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Penny",
                "amount": 36.00,
                "tags": [],
            },
        )
        ing.ingest_sidecar(s1)
        ing.ingest_sidecar(s2)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()

        self.assertEqual(count, 2)

    def test_same_source_type_not_deduped(self):
        """Two receipts for same amount within window = separate."""
        ing = self._make_ingestor()
        s1 = self._write_sidecar(
            "2025-05-23__Sunrise-Home-Care__REC__Sterling__64.50",
            {
                "date": "2025-05-23",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 64.50,
                "tags": [],
            },
        )
        s2 = self._write_sidecar(
            "2025-06-08__Sunrise-Home-Care__REC__Sterling__64.50",
            {
                "date": "2025-06-08",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 64.50,
                "tags": [],
            },
        )
        ing.ingest_sidecar(s1)
        ing.ingest_sidecar(s2)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()

        self.assertEqual(count, 2)

    def test_deleted_expense_not_matched(self):
        """Soft-deleted expense should not block new ingestion."""
        ing = self._make_ingestor()
        s1 = self._write_sidecar(
            "2025-09-25__Sunrise-Home-Care__REC__Sterling__36.00",
            {
                "date": "2025-09-25",
                "entity": "Sunrise-Home-Care",
                "doc_type": "REC",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        ing.ingest_sidecar(s1)

        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE hsa_expenses SET status = 'DELETED'")
        conn.commit()
        conn.close()

        s2 = self._write_sidecar(
            "2025-10-01__Sunrise-Home-Care__INV__Sterling__36.00",
            {
                "date": "2025-10-01",
                "entity": "Sunrise-Home-Care",
                "doc_type": "INV",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": [],
            },
        )
        ing.ingest_sidecar(s2)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hsa_expenses").fetchone()[0]
        conn.close()

        self.assertEqual(count, 2)

    def test_dry_run_shows_dedup_matches(self):
        """Dry-run should detect existing matches (show ~ not +)."""
        ing = self._make_ingestor()
        eob = self._write_sidecar(
            "2025-09-25__Shield-Health__EOB__Sterling__36.00__Sunrise-Care-Inc",
            {
                "date": "2025-09-25",
                "entity": "Shield-Health",
                "doc_type": "EOB",
                "patient": "Sterling",
                "amount": 36.00,
                "tags": ["Sunrise-Care-Inc"],
                "financials": {
                    "billed": 100.0,
                    "patient_responsibility": 36.00,
                },
            },
        )
        ing.ingest_sidecar(eob)

        conn = sqlite3.connect(self.db_path)
        expense_id = conn.execute(
            "SELECT id FROM hsa_expenses"
        ).fetchone()[0]
        conn.close()

        self.db.dry_run = True
        ing_dry = self._make_ingestor()

        match = ing_dry._find_matching_expense(
            "Sunrise Home Care", "sterling", 36.00,
            "2025-10-14", "invoice",
        )
        self.assertEqual(match, expense_id)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM hsa_expenses"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


class TestHsaEvidenceLevel(unittest.TestCase):
    """Tests for evidence_level in summary and API."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self._seed()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _seed(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient, description, "
            "patient_responsibility, category, source, "
            "status, needs_review, evidence_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-01-01",
                "Doc A",
                "self",
                "Visit",
                100.00,
                "medical",
                "receipt",
                "UNREIMBURSED",
                0,
                "ready",
            ),
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient, description, "
            "patient_responsibility, category, source, "
            "status, needs_review, evidence_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2025-02-01",
                "Doc B",
                "self",
                "Visit",
                200.00,
                "medical",
                "eob",
                "UNREIMBURSED",
                1,
                "stub",
            ),
        )
        conn.commit()
        conn.close()

    def test_summary_shows_reimbursable_total(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_summary

        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "year": None,
                "patient": None,
                "json_output": True,
            },
        )()

        f = io.StringIO()
        with redirect_stdout(f):
            cmd_summary(args)

        output = json.loads(f.getvalue())
        self.assertEqual(output["reimbursable_total"], 100.00)
        self.assertIn("evidence_totals", output)

    def test_merge_transfers_fields_and_docs(self):
        from housebook.hsa.cli import cmd_merge

        # 1. Create target (Receipt) and source (CC Stub)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (10, '2026-04-20', 'Target Prov', 123.45, 'receipt', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review, transaction_id) "
            "VALUES (11, '2026-04-25', 'Source Prov', 123.45, 'cc_stub', 1, 99999)"
        )
        conn.execute(
            "INSERT INTO hsa_documents "
            "(expense_id, document_type, file_path, file_hash, "
            "original_filename) "
            "VALUES (11, 'receipt', 'test.pdf', 'hash', 'test.pdf')"
        )
        conn.commit()

        # 2. Execute merge
        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "target_id": 10,
                "source_id": 11,
                "reason": "Test merge",
            },
        )()

        cmd_merge(args)

        # 3. Verify
        row_target = conn.execute("SELECT * FROM hsa_expenses WHERE id = 10").fetchone()
        row_source = conn.execute("SELECT * FROM hsa_expenses WHERE id = 11").fetchone()
        doc_row = conn.execute(
            "SELECT expense_id FROM hsa_documents WHERE id = 1"
        ).fetchone()

        conn.close()

        self.assertEqual(row_target[11], 99999)  # transaction_id
        self.assertEqual(row_target[14], 0)  # needs_review
        self.assertIn("Test merge", row_target[16])  # notes

        self.assertEqual(row_source[13], "DELETED")  # status
        self.assertEqual(row_source[14], 0)  # needs_review

        self.assertEqual(doc_row[0], 10)  # Document moved to target

    def test_verify_sets_evidence_level(self):
        from housebook.hsa.cli import cmd_verify

        args = type(
            "Args",
            (),
            {
                "db_path": self.db_path,
                "ids": [2],
                "category": None,
                "patient": None,
                "provider": None,
                "evidence_level": "ready",
                "transaction_id": None,
                "notes": None,
                "payment_method": None,
                "payment_date": None,
            },
        )()

        cmd_verify(args)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT evidence_level, needs_review FROM hsa_expenses WHERE id = 2"
        ).fetchone()
        conn.close()
        self.assertEqual(row["evidence_level"], "ready")
        self.assertEqual(row["needs_review"], 0)


    def test_merge_service_into_service(self):
        """Merge works with any source type, not just CC stubs."""
        from housebook.hsa.cli import cmd_merge

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (20, '2026-04-20', 'Prov A', 50.00, 'eob', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (21, '2026-04-25', 'Prov A', 50.00, 'invoice', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_documents "
            "(expense_id, document_type, file_path, file_hash, "
            "original_filename) "
            "VALUES (21, 'invoice', 'test.pdf', 'abc', 'test.pdf')"
        )
        conn.commit()

        import io
        from contextlib import redirect_stdout

        args = type("Args", (), {
            "db_path": self.db_path, "target_id": 20,
            "source_id": 21, "reason": None, "json_output": False,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_merge(args)
        self.assertIn("Successfully merged", f.getvalue())

        row = conn.execute(
            "SELECT status FROM hsa_expenses WHERE id = 21"
        ).fetchone()
        self.assertEqual(row[0], "DELETED")

        doc = conn.execute(
            "SELECT expense_id FROM hsa_documents WHERE file_path = 'test.pdf'"
        ).fetchone()
        self.assertEqual(doc[0], 20)
        conn.close()

    def test_merge_rejects_deleted_target(self):
        from housebook.hsa.cli import cmd_merge

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review, status) "
            "VALUES (30, '2026-04-20', 'Prov A', 50.00, 'eob', 0, "
            "'DELETED')"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (31, '2026-04-25', 'Prov B', 50.00, 'invoice', 1)"
        )
        conn.commit()

        import io
        from contextlib import redirect_stdout

        args = type("Args", (), {
            "db_path": self.db_path, "target_id": 30,
            "source_id": 31, "reason": None, "json_output": False,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_merge(args)
        self.assertIn("is deleted", f.getvalue())
        conn.close()

    def test_merge_nonexistent_ids(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_merge

        args = type("Args", (), {
            "db_path": self.db_path, "target_id": 999,
            "source_id": 998, "reason": None, "json_output": False,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_merge(args)
        self.assertIn("not found", f.getvalue())

    def test_merge_audit_log_documents(self):
        from housebook.hsa.cli import cmd_merge

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (40, '2026-04-20', 'Prov', 75.00, 'receipt', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review, transaction_id) "
            "VALUES (41, '2026-04-25', 'Prov', 75.00, 'cc_stub', 1, 88888)"
        )
        conn.execute(
            "INSERT INTO hsa_documents "
            "(expense_id, document_type, file_path, file_hash, "
            "original_filename) "
            "VALUES (41, 'receipt', 'stub.pdf', 'hash2', 'stub.pdf')"
        )
        conn.commit()

        args = type("Args", (), {
            "db_path": self.db_path, "target_id": 40,
            "source_id": 41, "reason": "test", "json_output": False,
        })()
        cmd_merge(args)

        conn.row_factory = sqlite3.Row
        log = conn.execute(
            "SELECT * FROM hsa_audit_log WHERE field_name = 'expense_id'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(log)
        self.assertEqual(log["table_name"], "hsa_documents")

    def test_merge_logs_notes_change(self):
        from housebook.hsa.cli import cmd_merge

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (50, '2026-04-20', 'Prov', 60.00, 'receipt', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (51, '2026-04-25', 'Prov', 60.00, 'cc_stub', 1)"
        )
        conn.commit()

        args = type("Args", (), {
            "db_path": self.db_path, "target_id": 50,
            "source_id": 51, "reason": "Merge reason", "json_output": False,
        })()
        cmd_merge(args)

        conn.row_factory = sqlite3.Row
        log = conn.execute(
            "SELECT * FROM hsa_audit_log "
            "WHERE record_id = 50 AND field_name = 'notes'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(log)
        self.assertIn("Merge reason", log["new_value"])


class TestHsaMergeMany(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(id, date, description, amount, category, source, status, "
            "original_file, needs_review, source_file_path) "
            "VALUES (7, '2026-04-20', 'MAPLE HEALTH PLAN', 100.00, "
            "'Health & Medical', 'Amex', 'AGENT_VERIFIED', "
            "'statement.pdf', 0, 'cc/2026/statement.pdf')"
        )
        for target_id, source in ((1, "receipt"), (2, "eob")):
            conn.execute(
                "INSERT INTO hsa_expenses "
                "(id, service_date, provider, patient_responsibility, "
                "source, status, needs_review, evidence_level) "
                "VALUES (?, '2026-04-15', 'Maple Clinic', 50.00, ?, "
                "'UNREIMBURSED', 1, 'stub')",
                (target_id, source),
            )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, "
            "source, status, needs_review, evidence_level, "
            "transaction_id, payment_method, payment_date) "
            "VALUES (3, '2026-04-20', 'Maple Clinic', 100.00, "
            "'cc_stub', 'UNREIMBURSED', 1, 'stub', 7, "
            "'card', '2026-04-20')"
        )
        conn.execute(
            "INSERT INTO hsa_documents "
            "(expense_id, document_type, file_path, file_hash, "
            "original_filename) "
            "VALUES (3, 'statement', 'statement.pdf', 'hash', "
            "'statement.pdf')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _args(self, **overrides):
        values = {
            "db_path": self.db_path,
            "source_id": 3,
            "target_ids": [1, 2],
            "reason": None,
            "dry_run": False,
            "json_output": True,
        }
        values.update(overrides)
        return type("Args", (), values)()

    def test_merge_many_uses_shared_transfer_path(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_merge_many

        output = io.StringIO()
        with redirect_stdout(output):
            cmd_merge_many(self._args())
        result = json.loads(output.getvalue())
        self.assertEqual(result["target_ids"], [1, 2])
        self.assertEqual(
            result["fields_transferred"],
            ["transaction_id", "payment_method", "payment_date"],
        )
        self.assertEqual(result["docs_target_id"], 1)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        targets = conn.execute(
            "SELECT * FROM hsa_expenses WHERE id IN (1, 2) ORDER BY id"
        ).fetchall()
        source = conn.execute(
            "SELECT status, needs_review FROM hsa_expenses WHERE id = 3"
        ).fetchone()
        document_target = conn.execute(
            "SELECT expense_id FROM hsa_documents"
        ).fetchone()[0]
        conn.close()

        for target in targets:
            self.assertEqual(target["transaction_id"], 7)
            self.assertEqual(target["payment_method"], "card")
            self.assertEqual(target["payment_date"], "2026-04-20")
            self.assertEqual(target["needs_review"], 0)
            self.assertIn("cc/2026/statement.pdf", target["notes"])
        self.assertIn("transferred to expense #1", targets[1]["notes"])
        self.assertEqual(tuple(source), ("DELETED", 0))
        self.assertEqual(document_target, 1)

    def test_merge_many_dry_run_changes_nothing(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_merge_many

        with redirect_stdout(io.StringIO()):
            cmd_merge_many(self._args(dry_run=True))

        conn = sqlite3.connect(self.db_path)
        source_status = conn.execute(
            "SELECT status FROM hsa_expenses WHERE id = 3"
        ).fetchone()[0]
        linked_targets = conn.execute(
            "SELECT COUNT(*) FROM hsa_expenses "
            "WHERE id IN (1, 2) AND transaction_id IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(source_status, "UNREIMBURSED")
        self.assertEqual(linked_targets, 0)

    def test_merge_many_rejects_source_as_target(self):
        from housebook.hsa.cli import cmd_merge_many

        with self.assertRaises(SystemExit):
            cmd_merge_many(self._args(target_ids=[1, 3]))


class TestHsaDelete(unittest.TestCase):
    """Tests for the delete command."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_delete_soft_deletes_expense(self):
        from housebook.hsa.cli import cmd_delete

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (1, '2026-04-20', 'Prov A', 50.00, 'receipt', 1)"
        )
        conn.commit()

        import io
        from contextlib import redirect_stdout

        args = type("Args", (), {
            "db_path": self.db_path, "ids": [1],
            "reason": "duplicate", "json_output": False,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_delete(args)
        self.assertIn("Deleted 1", f.getvalue())

        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, needs_review FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        self.assertEqual(row["status"], "DELETED")
        self.assertEqual(row["needs_review"], 0)

        log = conn.execute(
            "SELECT * FROM hsa_audit_log "
            "WHERE record_id = 1 AND new_value = 'DELETED'"
        ).fetchone()
        self.assertIsNotNone(log)
        conn.close()

    def test_delete_skips_already_deleted(self):
        from housebook.hsa.cli import cmd_delete

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review, status) "
            "VALUES (1, '2026-04-20', 'Prov A', 50.00, 'receipt', 0, "
            "'DELETED')"
        )
        conn.commit()
        conn.close()

        import io
        from contextlib import redirect_stdout

        args = type("Args", (), {
            "db_path": self.db_path, "ids": [1],
            "reason": None, "json_output": False,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_delete(args)
        self.assertIn("already deleted", f.getvalue())

    def test_delete_stores_reason_in_notes(self):
        from housebook.hsa.cli import cmd_delete

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, source, "
            "needs_review) "
            "VALUES (1, '2026-04-20', 'Prov A', 50.00, 'receipt', 1)"
        )
        conn.commit()

        args = type("Args", (), {
            "db_path": self.db_path, "ids": [1],
            "reason": "wrong patient", "json_output": False,
        })()

        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            cmd_delete(args)

        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT notes FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        self.assertIn("wrong patient", row["notes"])
        conn.close()


class TestHsaCandidates(unittest.TestCase):
    """Tests for the candidates matching command."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_candidates_exact_match(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_candidates

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-01', 'Clinic A', 150.00, 'receipt', "
            "'UNREIMBURSED', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-05', 'Clinic A', 150.00, 'cc_stub', "
            "'UNREIMBURSED', 1)"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_candidates(args)
        out = json.loads(f.getvalue())
        results = out["exact_matches"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["prov_match"])
        self.assertGreaterEqual(results[0]["score"], 100)

    def test_candidates_share_ingestor_alias_and_lag_resolution(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_candidates

        providers = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        )
        json.dump({
            "providers": [{
                "canonical_name": "Maple Dental",
                "category": "dental",
                "aliases": ["Maple Dental"],
                "expected_billing_lag_days": 4,
            }],
        }, providers)
        providers.close()
        self.addCleanup(os.unlink, providers.name)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-01', 'Maple Dental Center - North', "
            "150.00, 'receipt', 'UNREIMBURSED', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-05', 'MAPLE DENTAL', 150.00, "
            "'cc_stub', 'UNREIMBURSED', 1)"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        output = io.StringIO()
        with patch(
            "housebook.hsa.providers.HSA_PROVIDERS_JSON",
            providers.name,
        ), redirect_stdout(output):
            cmd_candidates(args)

        results = json.loads(output.getvalue())["exact_matches"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["prov_match"])
        self.assertEqual(results[0]["score"], 150)

    def test_candidates_skips_zero_amount(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_candidates

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-01', 'Clinic A', 0.00, 'receipt', "
            "'UNREIMBURSED', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-05', 'Clinic A', 0.00, 'cc_stub', "
            "'UNREIMBURSED', 1)"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_candidates(args)
        out = json.loads(f.getvalue())
        self.assertEqual(len(out["exact_matches"]), 0)
        self.assertEqual(len(out["installment_patterns"]), 0)

    def test_candidates_skips_bad_dates(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_candidates

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES (NULL, 'Clinic A', 100.00, 'receipt', "
            "'UNREIMBURSED', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-05', 'Clinic A', 100.00, 'cc_stub', "
            "'UNREIMBURSED', 1)"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_candidates(args)
        out = json.loads(f.getvalue())
        self.assertEqual(len(out["exact_matches"]), 0)

    def test_candidates_no_match_different_amount(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_candidates

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-01', 'Clinic A', 150.00, 'receipt', "
            "'UNREIMBURSED', 1)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(service_date, provider, patient_responsibility, source, "
            "status, needs_review) "
            "VALUES ('2026-03-05', 'Clinic A', 200.00, 'cc_stub', "
            "'UNREIMBURSED', 1)"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_candidates(args)
        out = json.loads(f.getvalue())
        self.assertEqual(len(out["exact_matches"]), 0)


class TestMathProofGuard(unittest.TestCase):
    """Tests for math proof validation in verify and check."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self._seed()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _seed(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(id, date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (1, '2026-01-15', 'CLINIC CHARGE', 500.00, "
            "'Health & Medical', 'Amex', 'AGENT_VERIFIED', "
            "'stmt.pdf', 0)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, "
            "source, status, needs_review, transaction_id, "
            "evidence_level) "
            "VALUES (1, '2026-01-10', 'Clinic', 300.00, "
            "'receipt', 'UNREIMBURSED', 1, 1, 'stub')"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, "
            "source, status, needs_review, transaction_id, "
            "evidence_level) "
            "VALUES (2, '2026-01-12', 'Clinic', 150.00, "
            "'receipt', 'UNREIMBURSED', 1, 1, 'stub')"
        )
        conn.commit()
        conn.close()

    def test_verify_blocks_ready_on_math_mismatch(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_verify

        args = type("Args", (), {
            "db_path": self.db_path, "ids": [1],
            "category": None, "patient": None,
            "provider": None, "evidence_level": "ready",
            "transaction_id": None, "notes": None,
            "payment_method": None, "payment_date": None,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_verify(args)
        self.assertIn("Blocked", f.getvalue())
        self.assertIn("math proof", f.getvalue())

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT evidence_level FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["evidence_level"], "stub")

    def test_verify_allows_ready_when_math_matches(self):
        from housebook.hsa.cli import cmd_verify

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE hsa_expenses SET patient_responsibility = 200.00 "
            "WHERE id = 2"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "ids": [1],
            "category": None, "patient": None,
            "provider": None, "evidence_level": "ready",
            "transaction_id": None, "notes": None,
            "payment_method": None, "payment_date": None,
        })()
        cmd_verify(args)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT evidence_level FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["evidence_level"], "ready")

    def test_check_detects_math_mismatch(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_check

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_check(args)
        output = json.loads(f.getvalue())
        math_issues = [
            i for i in output["issues"]
            if i["type"] == "math_proof_mismatch"
        ]
        self.assertEqual(len(math_issues), 1)
        self.assertEqual(math_issues[0]["severity"], "error")

    def test_verify_proves_link_created_in_same_call(self):
        from housebook.hsa.cli import cmd_verify

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions "
            "(id, date, description, amount, category, source, "
            "status, original_file, needs_review) "
            "VALUES (2, '2026-02-15', 'MAPLE CLINIC', 75.00, "
            "'Health & Medical', 'Amex', 'AGENT_VERIFIED', "
            "'stmt.pdf', 0)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, "
            "source, status, needs_review, transaction_id, "
            "evidence_level) "
            "VALUES (3, '2026-02-10', 'Maple Clinic', 75.00, "
            "'receipt', 'UNREIMBURSED', 1, NULL, 'stub')"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "ids": [3],
            "category": None, "patient": None,
            "provider": None, "evidence_level": "ready",
            "transaction_id": 2, "notes": None,
            "payment_method": None, "payment_date": None,
        })()
        cmd_verify(args)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT transaction_id, evidence_level "
            "FROM hsa_expenses WHERE id = 3"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (2, "ready"))

    def test_check_reports_missing_linked_transaction(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_check

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, "
            "source, status, needs_review, transaction_id, "
            "evidence_level) "
            "VALUES (3, '2026-02-10', 'Maple Clinic', 75.00, "
            "'receipt', 'UNREIMBURSED', 1, 9999, 'stub')"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
            "verify_hashes": False,
        })()
        output = io.StringIO()
        with redirect_stdout(output):
            cmd_check(args)
        issues = json.loads(output.getvalue())["issues"]
        missing = [
            issue for issue in issues
            if issue["type"] == "missing_payment_transaction"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["severity"], "error")


class TestHsaDocumentIntegrity(unittest.TestCase):
    """`check --verify-hashes` must detect altered source documents.

    A corrupted or swapped receipt is exactly the tampering an
    IRS-audit-proof ledger has to catch; existence-only checking
    passed it clean while a hash sat unused in the row.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self.tmpdir = tempfile.mkdtemp()
        self.doc = os.path.join(self.tmpdir, "receipt.pdf")
        with open(self.doc, "wb") as f:
            f.write(b"%PDF original receipt")

        from housebook.core import sidecar as sidecar_mod
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_documents "
            "(expense_id, document_type, file_path, file_hash, "
            "original_filename) VALUES (NULL, 'receipt', ?, ?, ?)",
            (self.doc, sidecar_mod.sha256_file(self.doc), "receipt.pdf"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil
        shutil.rmtree(self.tmpdir)

    def _run_check(self, verify_hashes):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_check

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
            "verify_hashes": verify_hashes,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_check(args)
        out = json.loads(f.getvalue())
        return [i for i in out["issues"] if i["type"] == "hash_mismatch"]

    def test_intact_document_passes(self):
        self.assertEqual(self._run_check(True), [])

    def test_altered_document_is_detected(self):
        with open(self.doc, "wb") as f:
            f.write(b"%PDF TAMPERED receipt")
        issues = self._run_check(True)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "error")

    def test_hash_check_is_opt_in(self):
        """Re-hashing is IO-bound, so it stays off by default."""
        with open(self.doc, "wb") as f:
            f.write(b"%PDF TAMPERED receipt")
        self.assertEqual(self._run_check(False), [])


class TestHsaPaymentPlan(unittest.TestCase):
    """Tests for payment plan creation and guards."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self._seed()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _seed(self):
        conn = sqlite3.connect(self.db_path)
        # Master candidate (EOB with large liability)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient, description, "
            "patient_responsibility, category, source, "
            "status, needs_review, evidence_level) "
            "VALUES (1, '2026-01-10', 'Clinic', 'self', 'Surgery', "
            "500.00, 'medical', 'eob', 'UNREIMBURSED', 1, 'stub')"
        )
        # Installment stubs
        for i, date in enumerate([
            "2026-02-01", "2026-03-01", "2026-04-01",
        ], start=2):
            conn.execute(
                "INSERT INTO hsa_expenses "
                "(id, service_date, provider, patient, description, "
                "patient_responsibility, category, source, "
                "status, needs_review, evidence_level) "
                "VALUES (?, ?, 'Clinic', 'self', 'Payment', "
                "100.00, 'medical', 'cc_stub', 'UNREIMBURSED', 1, 'stub')",
                (i, date),
            )
        conn.commit()
        conn.close()

    def _make_args(self, **overrides):
        defaults = {
            "db_path": self.db_path,
            "master_ids": None,
            "installment_ids": None,
            "name": None,
            "notes": None,
            "list_plans": False,
            "show_id": None,
            "json_output": False,
        }
        defaults.update(overrides)
        return type("Args", (), defaults)()

    def test_plan_creation(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_plan

        args = self._make_args(
            master_ids=[1],
            installment_ids=[2, 3, 4],
            name="Clinic Plan",
            json_output=True,
        )
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)
        out = json.loads(f.getvalue())
        self.assertEqual(out["plan_id"], 1)
        self.assertEqual(out["total_liability"], 500.00)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        master = conn.execute(
            "SELECT plan_role, evidence_level, payment_plan_id "
            "FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        self.assertEqual(master["plan_role"], "master")
        self.assertEqual(master["evidence_level"], "stub")
        self.assertEqual(master["payment_plan_id"], 1)

        inst = conn.execute(
            "SELECT plan_role, payment_plan_id "
            "FROM hsa_expenses WHERE id = 2"
        ).fetchone()
        self.assertEqual(inst["plan_role"], "installment")
        self.assertEqual(inst["payment_plan_id"], 1)

        logs = conn.execute(
            "SELECT * FROM hsa_audit_log WHERE record_id = 1"
        ).fetchall()
        fields = {row["field_name"] for row in logs}
        self.assertIn("payment_plan_id", fields)
        self.assertIn("plan_role", fields)
        conn.close()

    def test_plan_list(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_plan

        # Create a plan first
        args = self._make_args(
            master_ids=[1], installment_ids=[2, 3, 4],
            name="Test Plan",
        )
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)

        # List plans
        args = self._make_args(list_plans=True, json_output=True)
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)
        out = json.loads(f.getvalue())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Test Plan")
        self.assertEqual(out[0]["installment_count"], 3)

    def test_plan_show(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_plan

        args = self._make_args(
            master_ids=[1], installment_ids=[2, 3, 4],
            name="Show Plan",
        )
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)

        args = self._make_args(show_id=1, json_output=True)
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)
        out = json.loads(f.getvalue())
        self.assertEqual(out["plan"]["name"], "Show Plan")
        self.assertEqual(len(out["masters"]), 1)
        self.assertEqual(len(out["installments"]), 3)

    def test_plan_rejects_already_linked(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_plan

        # Create first plan
        args = self._make_args(
            master_ids=[1], installment_ids=[2, 3],
            name="Plan A",
        )
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)

        # Try to create second plan reusing installment 2
        args = self._make_args(
            master_ids=[1], installment_ids=[2, 4],
            name="Plan B",
        )
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)
        self.assertIn("already linked", f.getvalue())

        # Verify only one plan exists
        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM hsa_payment_plans"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_verify_blocks_master_promotion(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_plan, cmd_verify

        # Create plan
        args = self._make_args(
            master_ids=[1], installment_ids=[2, 3, 4],
            name="Guard Plan",
        )
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_plan(args)

        # Try to promote master to ready
        args = type("Args", (), {
            "db_path": self.db_path, "ids": [1],
            "category": None, "patient": None,
            "provider": None, "evidence_level": "ready",
            "transaction_id": None, "notes": None,
            "payment_method": None, "payment_date": None,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_verify(args)
        self.assertIn("Blocked", f.getvalue())
        self.assertIn("plan master", f.getvalue())

        # Verify evidence_level unchanged
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT evidence_level FROM hsa_expenses WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["evidence_level"], "stub")


class TestInstallmentPatternDetection(unittest.TestCase):
    """Tests for the pure installment matcher."""

    def test_detects_monthly_pattern(self):
        from housebook.hsa.matching import detect_installment_patterns

        stubs = [
            {"id": i, "provider": "Clinic", "patient_responsibility": 100.0,
             "service_date": f"2026-0{m}-01", "payment_plan_id": None}
            for i, m in enumerate([1, 2, 3], start=1)
        ]
        expenses = []
        patterns = detect_installment_patterns(expenses, stubs)
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["installment_count"], 3)
        self.assertEqual(patterns[0]["installment_amount"], 100.0)

    def test_rejects_irregular_spacing(self):
        from housebook.hsa.matching import detect_installment_patterns

        stubs = [
            {"id": 1, "provider": "Clinic", "patient_responsibility": 50.0,
             "service_date": "2026-01-01", "payment_plan_id": None},
            {"id": 2, "provider": "Clinic", "patient_responsibility": 50.0,
             "service_date": "2026-01-05", "payment_plan_id": None},
            {"id": 3, "provider": "Clinic", "patient_responsibility": 50.0,
             "service_date": "2026-03-15", "payment_plan_id": None},
        ]
        patterns = detect_installment_patterns([], stubs)
        self.assertEqual(len(patterns), 0)

    def test_excludes_already_linked_stubs(self):
        from housebook.hsa.matching import detect_installment_patterns

        stubs = [
            {"id": 1, "provider": "Clinic", "patient_responsibility": 100.0,
             "service_date": "2026-01-01", "payment_plan_id": 5},
            {"id": 2, "provider": "Clinic", "patient_responsibility": 100.0,
             "service_date": "2026-02-01", "payment_plan_id": 5},
            {"id": 3, "provider": "Clinic", "patient_responsibility": 100.0,
             "service_date": "2026-03-01", "payment_plan_id": None},
        ]
        patterns = detect_installment_patterns([], stubs)
        self.assertEqual(len(patterns), 0)

    def test_finds_master_candidates(self):
        from housebook.hsa.matching import detect_installment_patterns

        stubs = [
            {"id": i, "provider": "Clinic", "patient_responsibility": 100.0,
             "service_date": f"2026-0{m}-01", "payment_plan_id": None}
            for i, m in enumerate([1, 2, 3], start=10)
        ]
        expenses = [
            {"id": 1, "provider": "Clinic", "patient_responsibility": 500.0,
             "source": "invoice", "payment_plan_id": None},
        ]
        patterns = detect_installment_patterns(expenses, stubs)
        self.assertEqual(len(patterns), 1)
        self.assertEqual(len(patterns[0]["master_candidates"]), 1)
        self.assertEqual(patterns[0]["master_candidates"][0]["id"], 1)


class TestPlanIntegrityChecks(unittest.TestCase):
    """Tests for plan-related checks in cmd_check."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_check_detects_orphaned_plan_link(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_check

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, "
            "source, status, needs_review, payment_plan_id, plan_role) "
            "VALUES (1, '2026-01-01', 'Clinic', 100.00, "
            "'cc_stub', 'UNREIMBURSED', 0, 999, 'installment')"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_check(args)
        out = json.loads(f.getvalue())
        orphan_issues = [
            i for i in out["issues"]
            if i["type"] == "orphaned_plan_link"
        ]
        self.assertEqual(len(orphan_issues), 1)

    def test_check_detects_reimbursable_master(self):
        import io
        from contextlib import redirect_stdout

        from housebook.hsa.cli import cmd_check

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO hsa_payment_plans (id, name, total_liability) "
            "VALUES (1, 'Test', 500.00)"
        )
        conn.execute(
            "INSERT INTO hsa_expenses "
            "(id, service_date, provider, patient_responsibility, "
            "source, status, needs_review, payment_plan_id, "
            "plan_role, evidence_level) "
            "VALUES (1, '2026-01-01', 'Clinic', 500.00, "
            "'eob', 'UNREIMBURSED', 0, 1, 'master', 'ready')"
        )
        conn.commit()
        conn.close()

        args = type("Args", (), {
            "db_path": self.db_path, "json_output": True,
        })()
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_check(args)
        out = json.loads(f.getvalue())
        master_issues = [
            i for i in out["issues"]
            if i["type"] == "master_reimbursable"
        ]
        self.assertEqual(len(master_issues), 1)


class TestPlanSidecarIngestion(unittest.TestCase):
    """Test that PLAN sidecars create documents only, not expenses."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_hsa_schema(self.db_path)
        self.db = Database(self.db_path)
        self.tmpdir = tempfile.mkdtemp()

        self.providers_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        )
        json.dump({"providers": []}, self.providers_file)
        self.providers_file.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        os.unlink(self.providers_file.name)
        import shutil
        shutil.rmtree(self.tmpdir)

    def _make_ingestor(self):
        with patch(
            "housebook.hsa.providers.HSA_PROVIDERS_JSON",
            self.providers_file.name,
        ):
            return HsaIngestor(self.db, None)

    def _write_sidecar(self, name, meta):
        json_path = os.path.join(self.tmpdir, name + ".json")
        pdf_path = os.path.join(self.tmpdir, name + ".pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy content")
        with open(json_path, "w") as f:
            json.dump(_wrap_in_envelope(meta, pdf_path), f)
        return json_path

    def test_plan_sidecar_creates_document_only(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar(
            "2026-01-15__Clinic__PLAN__Self__0.00__Installment",
            {
                "date": "2026-01-15",
                "entity": "Clinic",
                "doc_type": "PLAN",
                "patient": "Self",
                "amount": 0.0,
                "tags": ["Installment"],
            },
        )
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        expenses = conn.execute(
            "SELECT COUNT(*) FROM hsa_expenses"
        ).fetchone()[0]
        docs = conn.execute(
            "SELECT COUNT(*) FROM hsa_documents"
        ).fetchone()[0]
        doc_type = conn.execute(
            "SELECT document_type FROM hsa_documents"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(expenses, 0)
        self.assertEqual(docs, 1)
        self.assertEqual(doc_type, "plan")


if __name__ == "__main__":
    unittest.main()
