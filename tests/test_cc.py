"""Tests for the CC module: schema validation, ingestor, and CLI.

Test data uses the v1 envelope format as produced by the
bulk-backlog classify pass. Fixture sidecars exercise:
- Valid sidecar → ingest succeeds, provenance recorded
- Period start > end (BoA year-inference bug) → rejected
- Transaction outside grace window → rejected
- Idempotent re-ingest → skipped
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

from housebook.cc.schema import (
    CcSchemaError,
    validate_data_block,
)

# ── Schema validation tests ────────────────────────────────────


class TestCcSchemaValidation(unittest.TestCase):
    """Tests for validate_data_block()."""

    def _make_valid_data(self):
        return {
            "issuer": "Amex",
            "account": {"last4": "1111", "name": "Test User"},
            "statement_period": {
                "start": "2025-03-05",
                "end": "2025-04-03",
            },
            "balances": {"opening": 100.0, "closing": 200.0},
            "payment": {"due_date": "2025-04-28", "minimum_due": 25.0},
            "transactions": [
                {
                    "date": "2025-03-09",
                    "description": "GROCERY STORE",
                    "amount": 42.50,
                    "category": "Groceries",
                    "metadata": None,
                    "page": None,
                },
            ],
            "tx_count_db": 1,
            "tx_total_db": 42.50,
        }

    def test_valid_data_passes(self):
        errs = validate_data_block(self._make_valid_data())
        self.assertEqual(errs, [])

    def test_start_after_end_rejected(self):
        """The BoA single-year bug: Dec 2025 → Jan 2025 = start > end."""
        data = self._make_valid_data()
        data["statement_period"]["start"] = "2025-12-12"
        data["statement_period"]["end"] = "2025-01-11"
        errs = validate_data_block(data)
        self.assertTrue(any("start" in e and "end" in e for e in errs))

    def test_period_span_too_long_rejected(self):
        data = self._make_valid_data()
        data["statement_period"]["end"] = "2025-06-01"
        errs = validate_data_block(data)
        self.assertTrue(any("span" in e for e in errs))

    def test_transaction_outside_grace_rejected(self):
        data = self._make_valid_data()
        data["transactions"][0]["date"] = "2024-01-01"
        errs = validate_data_block(data)
        self.assertTrue(any("outside" in e for e in errs))

    def test_transaction_within_grace_passes(self):
        data = self._make_valid_data()
        data["transactions"][0]["date"] = "2025-02-26"
        errs = validate_data_block(data)
        self.assertEqual(errs, [])

    def test_tx_count_mismatch_rejected(self):
        data = self._make_valid_data()
        data["tx_count_db"] = 99
        errs = validate_data_block(data)
        self.assertTrue(any("tx_count_db" in e for e in errs))

    def test_tx_total_mismatch_rejected(self):
        data = self._make_valid_data()
        data["tx_total_db"] = 999.99
        errs = validate_data_block(data)
        self.assertTrue(any("tx_total_db" in e for e in errs))

    def test_missing_issuer_rejected(self):
        data = self._make_valid_data()
        data["issuer"] = ""
        errs = validate_data_block(data)
        self.assertTrue(any("issuer" in e for e in errs))

    def test_last4_xxxx_accepted(self):
        data = self._make_valid_data()
        data["account"]["last4"] = "XXXX"
        errs = validate_data_block(data)
        self.assertEqual(errs, [])

    def test_last4_null_accepted(self):
        data = self._make_valid_data()
        data["account"]["last4"] = None
        errs = validate_data_block(data)
        self.assertEqual(errs, [])

    def test_issuer_alias_flagged_with_resolver(self):
        """An issuer written as a known alias is flagged when a
        resolver is supplied — this is the check that catches the
        'Home Goods'/'Home-Goods' source split at its source."""
        data = self._make_valid_data()
        data["issuer"] = "Home Goods"
        resolver = _make_resolver()
        errs = validate_data_block(data, issuer_resolver=resolver)
        self.assertTrue(
            any("alias" in e and "Home-Goods" in e for e in errs)
        )

    def test_issuer_canonical_passes_with_resolver(self):
        data = self._make_valid_data()
        data["issuer"] = "Home-Goods"
        errs = validate_data_block(data, issuer_resolver=_make_resolver())
        self.assertEqual(errs, [])

    def test_unknown_issuer_not_flagged(self):
        """A genuinely new card may be added before issuers.json is
        updated — an unknown issuer must not block validation."""
        data = self._make_valid_data()
        data["issuer"] = "Brand New Bank"
        errs = validate_data_block(data, issuer_resolver=_make_resolver())
        self.assertEqual(errs, [])

    def test_no_resolver_skips_issuer_canonicalization(self):
        """Without a resolver, an alias is accepted as-is (back-compat
        for callers that don't inject one)."""
        data = self._make_valid_data()
        data["issuer"] = "Home Goods"
        errs = validate_data_block(data)
        self.assertEqual(errs, [])


# ── Issuer resolver tests ──────────────────────────────────────


def _make_resolver():
    """Build an IssuerResolver over a temp issuers.json fixture."""
    from housebook.cc.issuers import IssuerResolver

    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "issuers": [
                    {
                        "name": "Home-Goods",
                        "aliases": ["HomeGoods", "Home Goods", "TJX"],
                    },
                    {"name": "Chase", "aliases": []},
                    {
                        "name": "Chase-Amazon",
                        "aliases": ["Chase Amazon Visa"],
                    },
                ]
            },
            f,
        )
    return IssuerResolver(issuers_json=path)


class TestIssuerResolver(unittest.TestCase):
    """Tests for IssuerResolver alias canonicalization."""

    def setUp(self):
        self.r = _make_resolver()

    def test_alias_resolves_to_canonical(self):
        self.assertEqual(self.r.resolve("Home Goods"), "Home-Goods")
        self.assertEqual(self.r.resolve("HomeGoods"), "Home-Goods")
        self.assertEqual(self.r.resolve("TJX"), "Home-Goods")

    def test_canonical_resolves_to_itself(self):
        self.assertEqual(self.r.resolve("Home-Goods"), "Home-Goods")

    def test_case_and_dash_insensitive(self):
        self.assertEqual(self.r.resolve("home goods"), "Home-Goods")
        self.assertEqual(self.r.resolve("  Home-Goods "), "Home-Goods")

    def test_substring_does_not_collapse_distinct_cards(self):
        """The HSA resolver's substring fallback would wrongly map
        'Chase' into 'Chase-Amazon'. Exact-match resolution must keep
        them distinct."""
        self.assertEqual(self.r.resolve("Chase"), "Chase")
        self.assertEqual(self.r.resolve("Chase-Amazon"), "Chase-Amazon")
        self.assertEqual(
            self.r.resolve("Chase Amazon Visa"), "Chase-Amazon"
        )

    def test_unknown_returned_stripped(self):
        self.assertEqual(self.r.resolve("  Brand New Bank "), "Brand New Bank")
        self.assertFalse(self.r.is_known("Brand New Bank"))

    def test_missing_config_is_graceful(self):
        from housebook.cc.issuers import IssuerResolver

        r = IssuerResolver(issuers_json="/nonexistent/issuers.json")
        self.assertEqual(r.resolve("Home Goods"), "Home Goods")
        self.assertFalse(r.is_known("Home Goods"))


# ── Ingestor tests ─────────────────────────────────────────────


def _create_cc_schema(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS processed_files (
        file_path TEXT PRIMARY KEY,
        file_hash TEXT,
        last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        statement_start DATE, statement_end DATE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ingestion_errors (
        file_path TEXT, line_number INTEGER,
        raw_line TEXT, error_message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, description TEXT, amount REAL,
        category TEXT, source TEXT, status TEXT,
        original_file TEXT, profile TEXT,
        needs_review BOOLEAN DEFAULT 1,
        trip_id INTEGER,
        metadata TEXT,
        linked_transaction_id INTEGER,
        source_file_path TEXT,
        source_file_sha256 TEXT,
        source_page INTEGER,
        sidecar_path TEXT
    )""")
    conn.commit()
    conn.close()


def _wrap_cc_envelope(data, pdf_path):
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
        size = os.path.getsize(pdf_path)
    else:
        sha256 = "0" * 64
        size = 0
    return {
        "schema_version": "1",
        "source": "cc",
        "source_file": {
            "path": os.path.basename(pdf_path),
            "sha256": sha256,
            "size_bytes": size,
            "mime_type": "application/pdf",
        },
        "classified_at": "2026-01-01T00:00:00Z",
        "classified_by": "test",
        "classifier_notes": None,
        "data": data,
    }


class TestCcIngestor(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        _create_cc_schema(self.db_path)
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil
        shutil.rmtree(self.tmpdir)

    def _make_ingestor(self):
        from housebook.cc.ingestor import CcIngestor
        from housebook.core.database import Database
        db = Database(self.db_path)
        return CcIngestor(db, None)

    def _write_sidecar(self, name, data):
        json_path = os.path.join(self.tmpdir, name + ".json")
        pdf_path = os.path.join(self.tmpdir, name + ".pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 dummy content")
        with open(json_path, "w") as f:
            json.dump(_wrap_cc_envelope(data, pdf_path), f)
        return json_path

    def _valid_data(self):
        return {
            "issuer": "Amex",
            "account": {"last4": "1111", "name": "Test"},
            "statement_period": {
                "start": "2025-03-05", "end": "2025-04-03",
            },
            "balances": {"opening": 0, "closing": 100},
            "payment": {"due_date": None, "minimum_due": None},
            "transactions": [
                {
                    "date": "2025-03-14",
                    "description": "TEST MERCHANT",
                    "amount": 55.00,
                    "category": "Shopping & Retail",
                    "metadata": None,
                    "page": None,
                },
            ],
            "tx_count_db": 1,
            "tx_total_db": 55.00,
        }

    def test_ingest_writes_transaction_with_provenance(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar("2025-04__Amex__1111", self._valid_data())
        rows = ing.ingest_sidecar(jp)
        self.assertEqual(len(rows), 1)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        txs = conn.execute("SELECT * FROM transactions").fetchall()
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0]["source"], "Amex")
        self.assertAlmostEqual(txs[0]["amount"], 55.00)
        self.assertIsNotNone(txs[0]["sidecar_path"])
        self.assertEqual(len(txs[0]["source_file_sha256"]), 64)
        conn.close()

    def test_ingest_sets_unverified_and_needs_review(self):
        """The status-lifecycle contract: scripts write UNVERIFIED only.

        AGENTS.md calls this binding — a promoted status would silently
        skip the mandatory agent review. It had no test, surviving on
        discipline alone.
        """
        ing = self._make_ingestor()
        jp = self._write_sidecar("2025-04__Amex__1111", self._valid_data())
        ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        tx = conn.execute("SELECT * FROM transactions").fetchone()
        conn.close()
        self.assertEqual(tx["status"], "UNVERIFIED")
        self.assertEqual(tx["needs_review"], 1)

    def test_empty_sidecar_is_reported_not_counted_as_unchanged(self):
        """A NEW sidecar that writes 0 rows must be flagged, not
        silently bucketed with already-processed files.

        Both cases make ingest_sidecar return [], but they mean
        opposite things: the empty one is a mis-authored sidecar that
        is now marked processed and will never be retried.
        """
        ing = self._make_ingestor()
        empty = self._valid_data()
        empty["transactions"] = []
        empty["tx_count_db"] = 0
        empty["tx_total_db"] = 0
        self._write_sidecar("2025-05__Amex__1111", empty)

        result = ing.ingest_directory(self.tmpdir)
        self.assertEqual(result["rows_written"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(result["empty"]), 1)

        # Second pass: now genuinely already-processed, so it counts
        # as skipped rather than empty.
        result2 = ing.ingest_directory(self.tmpdir)
        self.assertEqual(result2["skipped"], 1)
        self.assertEqual(result2["empty"], [])

    def test_idempotent_reingest_skips(self):
        ing = self._make_ingestor()
        jp = self._write_sidecar("stmt", self._valid_data())
        rows1 = ing.ingest_sidecar(jp)
        rows2 = ing.ingest_sidecar(jp)
        self.assertEqual(len(rows1), 1)
        self.assertEqual(len(rows2), 0)

    def test_intra_statement_duplicates_all_ingested(self):
        """Regression: a statement that legitimately lists the same
        charge twice must ingest BOTH rows, not collapse to one.

        The old ingestor passed the default max_duplicates=1 to
        transaction_exists, so the second identical line matched the
        row it had just inserted from the same statement and was
        silently dropped (4 such rows lost in a historical backlog:
        a resort stay, EV charging, park fees and vending)."""
        ing = self._make_ingestor()
        data = self._valid_data()
        dup = {
            "date": "2025-03-14",
            "description": "EV CHARGING STATION",
            "amount": 22.26,
            "category": None,
            "metadata": None,
            "page": None,
        }
        data["transactions"] = [dict(dup), dict(dup)]
        data["tx_count_db"] = 2
        data["tx_total_db"] = 44.52
        jp = self._write_sidecar("2025-04__Amex__1111__dup", data)
        rows = ing.ingest_sidecar(jp)
        self.assertEqual(len(rows), 2)

        conn = sqlite3.connect(self.db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE description = 'EV CHARGING STATION'",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)

    def test_reingest_backfills_only_missing_duplicate(self):
        """Re-ingesting a corrected statement is a non-destructive
        repair: it backfills only the copies the DB is missing and
        leaves the existing row untouched. This is what makes a
        whole-workspace re-ingest safe after the dedup fix."""
        ing = self._make_ingestor()

        # Post-bug state: one copy already in the DB (from a file
        # whose hash differs from the repair sidecar below).
        single = self._valid_data()
        single["transactions"][0]["description"] = "REPAIR ME"
        ing.ingest_sidecar(self._write_sidecar("single", single))

        # The corrected sidecar lists the same charge twice.
        dup = self._valid_data()
        base = dict(dup["transactions"][0], description="REPAIR ME")
        dup["transactions"] = [dict(base), dict(base)]
        dup["tx_count_db"] = 2
        dup["tx_total_db"] = round(base["amount"] * 2, 2)
        rows = ing.ingest_sidecar(self._write_sidecar("dup", dup))

        # DB held 1; statement wants 2 → exactly 1 row backfilled.
        self.assertEqual(len(rows), 1)
        conn = sqlite3.connect(self.db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE description = 'REPAIR ME'",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)

    def test_duplicate_rows_skipped_is_reported(self):
        """A row suppressed by the cross-file duplicate check is
        surfaced in the result tally — so a silent drop is never
        truly silent (the failure mode that hid the original bug)."""
        self._write_sidecar("a_stmt", self._valid_data())
        self._write_sidecar("b_stmt", self._valid_data())
        ing = self._make_ingestor()
        result = ing.ingest_directory(self.tmpdir)
        self.assertEqual(result["rows_written"], 1)
        self.assertEqual(result["duplicate_rows_skipped"], 1)

    def test_invalid_sidecar_raises(self):
        ing = self._make_ingestor()
        data = self._valid_data()
        data["statement_period"]["start"] = "2025-12-12"
        data["statement_period"]["end"] = "2025-01-11"
        jp = self._write_sidecar("bad", data)
        with self.assertRaises(CcSchemaError):
            ing.ingest_sidecar(jp)

    def test_mid_file_failure_rolls_back_and_retries_cleanly(self):
        ing = self._make_ingestor()
        data = self._valid_data()
        first = dict(
            data["transactions"][0],
            description="MAPLE MARKET",
            amount=10.0,
        )
        second = dict(
            data["transactions"][0],
            description="TRIGGER FAILURE",
            amount=20.0,
        )
        data["transactions"] = [first, second]
        data["tx_count_db"] = 2
        data["tx_total_db"] = 30.0
        jp = self._write_sidecar("atomic", data)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TRIGGER fail_cc_insert BEFORE INSERT ON transactions "
            "WHEN NEW.description = 'TRIGGER FAILURE' "
            "BEGIN SELECT RAISE(ABORT, 'forced test failure'); END"
        )
        conn.commit()
        conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            ing.ingest_sidecar(jp)

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            0,
        )
        conn.execute("DROP TRIGGER fail_cc_insert")
        conn.commit()
        conn.close()

        rows = ing.ingest_sidecar(jp)
        self.assertEqual(len(rows), 2)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            2,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            1,
        )
        conn.close()

    def test_ingest_directory_reports_errors(self):
        ing = self._make_ingestor()
        self._write_sidecar("good", self._valid_data())
        bad_data = self._valid_data()
        bad_data["statement_period"]["start"] = "2025-12-01"
        bad_data["statement_period"]["end"] = "2025-01-01"
        self._write_sidecar("bad", bad_data)
        result = ing.ingest_directory(self.tmpdir)
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(len(result["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
