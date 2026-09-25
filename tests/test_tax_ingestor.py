import json
import os
import sqlite3
import tempfile
import unittest

from housebook.core.database import Database
from housebook.migrations.runner import run_migrations
from housebook.tax.ingestor import TaxGenericIngestor


class TestTaxIngestAtomicity(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        run_migrations(self.db_path, verbose=False)
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil

        shutil.rmtree(self.tmpdir)

    def _write_multi_document_sidecar(self):
        path = os.path.join(self.tmpdir, "tax-report.json")
        envelope = {
            "schema_version": "1",
            "source": "tax",
            "source_file": {
                "path": "tax/2025/ledger-report.xlsx",
                "sha256": "0" * 64,
                "size_bytes": 100,
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
            },
            "classified_at": "2026-01-01T00:00:00Z",
            "classified_by": "test",
            "data": {
                "tax_year": 2025,
                "documents": [
                    {
                        "document_type": "BR-TAX-REPORT",
                        "issuer": "Ledger Investments",
                        "category": "Income",
                        "amount": 100.0,
                        "currency": "USD",
                    },
                    {
                        "document_type": "BR-FTC",
                        "issuer": "Trigger Authority",
                        "category": "Tax Paid",
                        "amount": 10.0,
                        "currency": "USD",
                    },
                ],
            },
        }
        with open(path, "w") as f:
            json.dump(envelope, f)
        return path

    def test_mid_file_failure_rolls_back_and_retries_cleanly(self):
        sidecar = self._write_multi_document_sidecar()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TRIGGER fail_tax_insert "
            "BEFORE INSERT ON tax_documents "
            "WHEN NEW.issuer = 'Trigger Authority' "
            "BEGIN SELECT RAISE(ABORT, 'forced test failure'); END"
        )
        conn.commit()
        conn.close()

        ingestor = TaxGenericIngestor(Database(self.db_path), None)
        with self.assertRaises(sqlite3.IntegrityError):
            ingestor.ingest_sidecar(sidecar)

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM tax_documents").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            0,
        )
        conn.execute("DROP TRIGGER fail_tax_insert")
        conn.commit()
        conn.close()

        rows = ingestor.ingest_sidecar(sidecar)
        self.assertEqual(len(rows), 2)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM tax_documents").fetchone()[0],
            2,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            1,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
