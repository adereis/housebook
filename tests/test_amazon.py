"""Tests for the Amazon CSV ingestor.

Focused on the intra-file duplicate regression: an Order History CSV
can legitimately contain the same product, ordered the same day, for
the same amount (two separate orders). The old ingestor passed the
default max_duplicates=1 to transaction_exists, so the second such
row matched the one just inserted and was silently dropped.
"""

import csv
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal


def _create_schema(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS processed_files (
        file_path TEXT PRIMARY KEY,
        file_hash TEXT,
        last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        statement_start DATE, statement_end DATE
    )""")
    # Column names must match migrations/001_baseline.sql — this
    # fixture previously declared (raw_line, error_message), so any
    # test reaching Database.log_ingestion_error would have failed
    # with "no such column", leaving every error path unexercised.
    c.execute("""CREATE TABLE IF NOT EXISTS ingestion_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT, line_number INTEGER,
        raw_text TEXT, error TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, description TEXT, amount REAL,
        category TEXT, source TEXT, status TEXT,
        original_file TEXT, profile TEXT,
        needs_review BOOLEAN DEFAULT 1,
        trip_id INTEGER, metadata TEXT,
        linked_transaction_id INTEGER,
        source_file_path TEXT, source_file_sha256 TEXT,
        source_page INTEGER, sidecar_path TEXT
    )""")
    conn.commit()
    conn.close()


class TestAmazonIngestor(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        _create_schema(self.db_path)
        self.profile_dir = tempfile.mkdtemp()
        self.orders_dir = os.path.join(
            self.profile_dir, "Your Amazon Orders",
        )
        os.makedirs(self.orders_dir)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil
        shutil.rmtree(self.profile_dir)

    def _make_ingestor(self):
        from housebook.amazon.ingestor import AmazonIngestor
        from housebook.core.database import Database
        return AmazonIngestor(Database(self.db_path), None)

    def _write_orders(self, rows):
        path = os.path.join(self.orders_dir, "Order History.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "Order ID", "Order Date", "Currency",
                "Total Amount", "Product Name",
            ])
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    def test_orders_metadata_persists_order_id(self):
        """Each ingested order row carries its Amazon Order ID in
        `transactions.metadata` as JSON. Consumed by the
        aggregate-by-Order-ID reconciler to match multi-line physical
        orders against single bank charges (see runbook §6a step 3)."""
        self._write_orders([{
            "Order ID": "112-1234567-7654321",
            "Order Date": "2025-06-10T00:00:00Z",
            "Currency": "USD",
            "Total Amount": "$24.99",
            "Product Name": "HDMI Cable",
        }])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 1)
        meta = json.loads(txs[0].metadata)
        self.assertEqual(meta["amazon_order_id"], "112-1234567-7654321")
        self.assertEqual(meta["csv"], "orders")

        conn = sqlite3.connect(self.db_path)
        row_meta = conn.execute(
            "SELECT metadata FROM transactions WHERE source='Amazon'",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(
            json.loads(row_meta)["amazon_order_id"],
            "112-1234567-7654321",
        )

    def test_ingest_sets_unverified_and_needs_review(self):
        """The status-lifecycle contract: scripts write UNVERIFIED only.

        AGENTS.md calls this binding — a promoted status would silently
        skip the mandatory agent review.
        """
        self._write_orders([{
            "Order ID": "112-1234567-7654321",
            "Order Date": "2025-06-10T00:00:00Z",
            "Currency": "USD",
            "Total Amount": "$24.99",
            "Product Name": "HDMI Cable",
        }])
        ing = self._make_ingestor()
        ing.ingest_profile(self.profile_dir, profile="test")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        tx = conn.execute(
            "SELECT * FROM transactions WHERE source='Amazon'"
        ).fetchone()
        conn.close()
        self.assertEqual(tx["status"], "UNVERIFIED")
        self.assertEqual(tx["needs_review"], 1)

    def test_intra_file_duplicate_orders_all_ingested(self):
        """Two separate orders of the same item, same day, same price
        must both be ingested — not collapsed into one row."""
        order = {
            "Order Date": "2024-03-01T00:00:00Z",
            "Currency": "USD",
            "Total Amount": "$12.99",
            "Product Name": "USB-C Cable 6ft",
        }
        self._write_orders([
            {"Order ID": "111-0000001-0000001", **order},
            {"Order ID": "111-0000002-0000002", **order},
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 2)

        conn = sqlite3.connect(self.db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE description = 'Amazon: USB-C Cable 6ft'",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)

    def test_mid_file_failure_rolls_back_and_retries_cleanly(self):
        self._write_orders([
            {
                "Order ID": "111-0000001-0000001",
                "Order Date": "2026-01-10T00:00:00Z",
                "Currency": "USD",
                "Total Amount": "$10.00",
                "Product Name": "Maple Cable",
            },
            {
                "Order ID": "111-0000002-0000002",
                "Order Date": "2026-01-11T00:00:00Z",
                "Currency": "USD",
                "Total Amount": "$20.00",
                "Product Name": "Trigger Failure",
            },
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TRIGGER fail_amazon_insert "
            "BEFORE INSERT ON transactions "
            "WHEN NEW.description = 'Amazon: Trigger Failure' "
            "BEGIN SELECT RAISE(ABORT, 'forced test failure'); END"
        )
        conn.commit()
        conn.close()

        ing = self._make_ingestor()
        with self.assertRaises(sqlite3.IntegrityError):
            ing.ingest_profile(self.profile_dir, profile="test")

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0],
            0,
        )
        conn.execute("DROP TRIGGER fail_amazon_insert")
        conn.commit()
        conn.close()

        rows = ing.ingest_profile(self.profile_dir, profile="test")
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

    def test_same_item_across_profiles_not_collapsed(self):
        """Two people (different Amazon profiles) buying the same item,
        same day, same price are distinct transactions. The dedup key
        includes profile, so neither suppresses the other.

        Regression: the key was (date, description, amount, source);
        with source='Amazon' for everyone, the second profile's row
        matched the first profile's and was silently dropped."""
        order = {
            "Order Date": "2024-03-01T00:00:00Z",
            "Currency": "USD",
            "Total Amount": "$12.99",
            "Product Name": "USB-C Cable 6ft",
        }
        # Profile 1 (the default dir from setUp); distinct Order IDs so
        # the two CSVs differ (otherwise is_file_processed would skip).
        self._write_orders([{"Order ID": "111-AAAAAAA-0000001", **order}])

        # Profile 2 in its own directory tree.
        p2 = tempfile.mkdtemp()
        p2_orders = os.path.join(p2, "Your Amazon Orders")
        os.makedirs(p2_orders)
        with open(
            os.path.join(p2_orders, "Order History.csv"),
            "w", newline="", encoding="utf-8",
        ) as f:
            w = csv.DictWriter(f, fieldnames=[
                "Order ID", "Order Date", "Currency",
                "Total Amount", "Product Name",
            ])
            w.writeheader()
            w.writerow({"Order ID": "222-BBBBBBB-0000002", **order})
        self.addCleanup(__import__("shutil").rmtree, p2)

        ing = self._make_ingestor()
        ing.ingest_profile(self.profile_dir, profile="sterling")
        ing.ingest_profile(p2, profile="penny")

        conn = sqlite3.connect(self.db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE description = 'Amazon: USB-C Cable 6ft'",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)

    def test_distinct_orders_still_ingested(self):
        """Sanity: genuinely different items both land."""
        self._write_orders([
            {
                "Order ID": "111-0000001-0000001",
                "Order Date": "2024-03-01T00:00:00Z",
                "Currency": "USD", "Total Amount": "$12.99",
                "Product Name": "USB-C Cable 6ft",
            },
            {
                "Order ID": "111-0000002-0000002",
                "Order Date": "2024-03-01T00:00:00Z",
                "Currency": "USD", "Total Amount": "$8.50",
                "Product Name": "AA Batteries",
            },
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 2)


class TestAmazonDigitalContent(unittest.TestCase):
    """Tests for the Digital Content Orders.csv ingest.

    Each Amazon Order ID in this CSV maps to multiple rows
    (Price Amount + Tax, optionally with Promotion/Coupon rows for
    discounts). The ingestor aggregates Transaction Amount per
    Order ID and writes one transaction per order.
    """

    DIGITAL_FIELDS = [
        "Order ID", "Order Date", "Product Name",
        "Price Currency Code", "Component Type",
        "Offer Type Code", "Transaction Amount",
    ]

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        _create_schema(self.db_path)
        self.profile_dir = tempfile.mkdtemp()
        self.orders_dir = os.path.join(
            self.profile_dir, "Your Amazon Orders",
        )
        os.makedirs(self.orders_dir)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil
        shutil.rmtree(self.profile_dir)

    def _make_ingestor(self):
        from housebook.amazon.ingestor import AmazonIngestor
        from housebook.core.database import Database
        return AmazonIngestor(Database(self.db_path), None)

    def _write_digital(self, rows):
        path = os.path.join(
            self.orders_dir, "Digital Content Orders.csv",
        )
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.DIGITAL_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    def _digital_row(self, **overrides):
        """Default-row factory; override only what each test cares
        about. Keeps test bodies focused."""
        base = {
            "Order ID": "D01-0000000-0000001",
            "Order Date": "2025-01-15T12:00:00Z",
            "Product Name": "Prime Membership Fee",
            "Price Currency Code": "USD",
            "Component Type": "Price Amount",
            "Offer Type Code": "Not Applicable",
            "Transaction Amount": "0.0",
        }
        base.update(overrides)
        return base

    def test_digital_metadata_persists_order_id(self):
        """Digital Content orders also carry their Order ID in
        `transactions.metadata` (csv='digital'). The D01- prefix on
        the ID already distinguishes physical from digital, but the
        csv tag closes the disambiguation for refund rows whose ID
        inherits the original order's prefix."""
        self._write_digital([
            self._digital_row(**{
                "Order ID": "D01-FOO0000-BAR0001",
                "Product Name": "Prime Membership",
                "Component Type": "Price Amount",
                "Transaction Amount": "14.99",
            }),
            self._digital_row(**{
                "Order ID": "D01-FOO0000-BAR0001",
                "Product Name": "Prime Membership",
                "Component Type": "Tax",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 1)
        meta = json.loads(txs[0].metadata)
        self.assertEqual(meta["amazon_order_id"], "D01-FOO0000-BAR0001")
        self.assertEqual(meta["csv"], "digital")

    def test_aggregates_price_and_tax_into_single_transaction(self):
        """Two rows (Price Amount + Tax) for one Order ID → one tx
        at the net Transaction Amount.

        The real-world shape: Price row carries the customer-paid
        amount; Tax row contributes 0 on most US orders but is still
        part of the same logical purchase.
        """
        self._write_digital([
            self._digital_row(**{
                "Component Type": "Price Amount",
                "Transaction Amount": "9.99",
            }),
            self._digital_row(**{
                "Component Type": "Tax",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0].amount, Decimal("9.99"))
        self.assertEqual(
            txs[0].description, "Amazon Digital: Prime Membership Fee",
        )
        self.assertEqual(txs[0].status, "UNVERIFIED")
        self.assertTrue(txs[0].needs_review)

    def test_coupon_offsets_price_yielding_zero_net_skipped(self):
        """LastPass-style free-with-coupon: Price $12 + Coupon -$12
        nets to $0 — must be skipped (no real charge happened)."""
        oid = "D01-FREE0000-0000002"
        self._write_digital([
            self._digital_row(**{
                "Order ID": oid, "Product Name": "LastPass Premium",
                "Component Type": "Price Amount",
                "Offer Type Code": "Not Applicable",
                "Transaction Amount": "12.00",
            }),
            self._digital_row(**{
                "Order ID": oid, "Product Name": "LastPass Premium",
                "Component Type": "Price Amount",
                "Offer Type Code": "Coupon",
                "Transaction Amount": "-12.00",
            }),
            self._digital_row(**{
                "Order ID": oid, "Product Name": "LastPass Premium",
                "Component Type": "Tax",
                "Offer Type Code": "Not Applicable",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(txs, [])

    def test_promotion_and_coupon_partial_discount_net_correct(self):
        """Mixed-discount order: Price $20 + Promotion +$0 +
        Coupon -$5 + Tax $0 → net $15."""
        oid = "D01-DISC0000-0000003"
        self._write_digital([
            self._digital_row(**{
                "Order ID": oid, "Product Name": "Some Book",
                "Component Type": "Price Amount",
                "Offer Type Code": "Not Applicable",
                "Transaction Amount": "20.00",
            }),
            self._digital_row(**{
                "Order ID": oid, "Product Name": "Some Book",
                "Component Type": "Price Amount",
                "Offer Type Code": "Coupon",
                "Transaction Amount": "-5.00",
            }),
            self._digital_row(**{
                "Order ID": oid, "Product Name": "Some Book",
                "Component Type": "Tax",
                "Offer Type Code": "Not Applicable",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0].amount, Decimal("15.00"))

    def test_non_usd_order_skipped(self):
        """BRL-denominated orders (e.g. a secondary profile) must not
        be summed as USD. The whole order is excluded if any row's
        Price Currency Code is non-USD."""
        oid = "D01-BR000000-0000004"
        self._write_digital([
            self._digital_row(**{
                "Order ID": oid, "Product Name": "Livro Brasileiro",
                "Price Currency Code": "BRL",
                "Component Type": "Price Amount",
                "Transaction Amount": "28.49",
            }),
            self._digital_row(**{
                "Order ID": oid, "Product Name": "Livro Brasileiro",
                "Price Currency Code": "BRL",
                "Component Type": "Tax",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(txs, [])

    def test_two_distinct_orders_both_ingested(self):
        """Sanity: separate Order IDs produce separate transactions."""
        self._write_digital([
            self._digital_row(**{
                "Order ID": "D01-AAAAAAA-0000005",
                "Product Name": "Kindle Unlimited",
                "Component Type": "Price Amount",
                "Transaction Amount": "9.99",
            }),
            self._digital_row(**{
                "Order ID": "D01-AAAAAAA-0000005",
                "Product Name": "Kindle Unlimited",
                "Component Type": "Tax", "Transaction Amount": "0.0",
            }),
            self._digital_row(**{
                "Order ID": "D01-BBBBBBB-0000006",
                "Product Name": "Audible Audiobook",
                "Component Type": "Price Amount",
                "Transaction Amount": "27.99",
            }),
            self._digital_row(**{
                "Order ID": "D01-BBBBBBB-0000006",
                "Product Name": "Audible Audiobook",
                "Component Type": "Tax", "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 2)
        amounts = sorted(str(t.amount) for t in txs)
        self.assertEqual(amounts, ["27.99", "9.99"])

    def test_reingest_same_file_is_idempotent(self):
        """File-level idempotency: re-ingesting the same CSV must
        not insert a second copy. Uses processed_files via the
        file-hash gate in ingest_profile."""
        self._write_digital([
            self._digital_row(**{
                "Product Name": "Kindle Unlimited",
                "Transaction Amount": "9.99",
            }),
        ])
        ing = self._make_ingestor()
        ing.ingest_profile(self.profile_dir, profile="test")
        ing.ingest_profile(self.profile_dir, profile="test")
        conn = sqlite3.connect(self.db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE description = 'Amazon Digital: Kindle Unlimited'",
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_two_identical_orders_in_one_file_both_land(self):
        """Multiplicity safety: two separate Order IDs with the same
        date+product+net should both be ingested. Same regression
        shape as the Order History intra-file dup test."""
        rows = []
        for oid in ("D01-IDENTIC-0000007", "D01-IDENTIC-0000008"):
            rows.extend([
                self._digital_row(**{
                    "Order ID": oid,
                    "Product Name": "Kindle Unlimited",
                    "Component Type": "Price Amount",
                    "Transaction Amount": "9.99",
                }),
                self._digital_row(**{
                    "Order ID": oid,
                    "Product Name": "Kindle Unlimited",
                    "Component Type": "Tax",
                    "Transaction Amount": "0.0",
                }),
            ])
        self._write_digital(rows)
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 2)

    def test_not_applicable_transaction_amount_is_ignored(self):
        """Some rows have Transaction Amount='Not Applicable'.
        Those contribute zero to the order's net; an order whose
        sole real charge is on one row must still ingest correctly."""
        oid = "D01-NOTAPPL-0000009"
        self._write_digital([
            self._digital_row(**{
                "Order ID": oid,
                "Product Name": "Ad free for Prime Video",
                "Component Type": "Price Amount",
                "Transaction Amount": "2.99",
            }),
            self._digital_row(**{
                "Order ID": oid,
                "Product Name": "Ad free for Prime Video",
                "Component Type": "Tax",
                "Transaction Amount": "Not Applicable",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0].amount, Decimal("2.99"))


class TestAmazonRefunds(unittest.TestCase):
    """Tests for Refund Details.csv ingest.

    The refund row's description carries the original product name (a
    lookup via orders_map keyed by Order ID), so an Order History.csv
    must be present alongside Refund Details.csv. The refund row's
    Order ID inherits the original physical/digital prefix — that is
    why metadata.csv = "refunds" is needed to distinguish it from
    the original purchase row at matcher time.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        _create_schema(self.db_path)
        self.profile_dir = tempfile.mkdtemp()
        self.orders_dir = os.path.join(
            self.profile_dir, "Your Amazon Orders",
        )
        self.refunds_dir = os.path.join(
            self.profile_dir, "Your Returns & Refunds",
        )
        os.makedirs(self.orders_dir)
        os.makedirs(self.refunds_dir)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil
        shutil.rmtree(self.profile_dir)

    def _make_ingestor(self):
        from housebook.amazon.ingestor import AmazonIngestor
        from housebook.core.database import Database
        return AmazonIngestor(Database(self.db_path), None)

    def _write_orders(self, rows):
        path = os.path.join(self.orders_dir, "Order History.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "Order ID", "Order Date", "Currency",
                "Total Amount", "Product Name",
            ])
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def _write_refunds(self, rows):
        path = os.path.join(self.refunds_dir, "Refund Details.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "Order ID", "Refund Date", "Currency", "Refund Amount",
            ])
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_refund_metadata_persists_order_id(self):
        """Each refund row tags its source Order ID and csv='refunds'.
        Order ID lets the future matcher pair the refund with its
        original purchase without fuzzy amount matching."""
        oid = "112-9999999-9999999"
        self._write_orders([{
            "Order ID": oid,
            "Order Date": "2025-05-01T00:00:00Z",
            "Currency": "USD",
            "Total Amount": "$42.00",
            "Product Name": "USB Hub",
        }])
        self._write_refunds([{
            "Order ID": oid,
            "Refund Date": "2025-05-20T00:00:00Z",
            "Currency": "USD",
            "Refund Amount": "$42.00",
        }])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        refunds = [t for t in txs if t.description.startswith(
            "Amazon REFUND:",
        )]
        self.assertEqual(len(refunds), 1)
        meta = json.loads(refunds[0].metadata)
        self.assertEqual(meta["amazon_order_id"], oid)
        self.assertEqual(meta["csv"], "refunds")
        # Sanity: refund amount is stored as negative
        self.assertEqual(refunds[0].amount, Decimal("-42.00"))


class TestAmazonDigitalReturns(unittest.TestCase):
    """Tests for Digital Returns.csv ingest.

    Same multi-row-per-order shape as Digital Content Orders (Price
    Amount + Tax + optional Coupon/Promotion). The per-row Transaction
    Amount values sum to the gross refund credit (positive in the
    CSV); the DB stores the negation so spending views net correctly.
    """

    DIGITAL_RETURNS_FIELDS = [
        "Order ID", "Return Date", "Product Name", "Base Currency",
        "Monetary Component Type", "Offer Type Code",
        "Transaction Amount",
    ]

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        _create_schema(self.db_path)
        self.profile_dir = tempfile.mkdtemp()
        self.orders_dir = os.path.join(
            self.profile_dir, "Your Amazon Orders",
        )
        os.makedirs(self.orders_dir)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        import shutil
        shutil.rmtree(self.profile_dir)

    def _make_ingestor(self):
        from housebook.amazon.ingestor import AmazonIngestor
        from housebook.core.database import Database
        return AmazonIngestor(Database(self.db_path), None)

    def _write_digital_returns(self, rows):
        path = os.path.join(self.orders_dir, "Digital Returns.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=self.DIGITAL_RETURNS_FIELDS,
            )
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    def _row(self, **overrides):
        base = {
            "Order ID": "D01-0000000-0000001",
            "Return Date": "2025-03-10T12:00:00Z",
            "Product Name": "Some E-Book",
            "Base Currency": "USD",
            "Monetary Component Type": "Price Amount",
            "Offer Type Code": "Not Applicable",
            "Transaction Amount": "0.0",
        }
        base.update(overrides)
        return base

    def test_aggregates_into_negative_refund_transaction(self):
        """Price $9.96 + Tax $0 → one tx at -$9.96 (refund credit
        stored as negative spending)."""
        self._write_digital_returns([
            self._row(**{
                "Monetary Component Type": "Price Amount",
                "Transaction Amount": "9.96",
            }),
            self._row(**{
                "Monetary Component Type": "Tax",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0].amount, Decimal("-9.96"))
        self.assertEqual(
            txs[0].description,
            "Amazon Digital Refund: Some E-Book",
        )
        self.assertEqual(txs[0].source, "Amazon")
        self.assertEqual(txs[0].status, "UNVERIFIED")
        self.assertTrue(txs[0].needs_review)

    def test_metadata_persists_order_id_and_csv_tag(self):
        """metadata.csv = 'digital_refunds' is what tells the matcher
        this row is the *refund side* of a D01- order, since the
        Order ID itself inherits the original purchase's D01- prefix."""
        oid = "D01-2222222-2222222"
        self._write_digital_returns([
            self._row(**{
                "Order ID": oid,
                "Monetary Component Type": "Price Amount",
                "Transaction Amount": "8.96",
            }),
            self._row(**{
                "Order ID": oid,
                "Monetary Component Type": "Tax",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(txs), 1)
        meta = json.loads(txs[0].metadata)
        self.assertEqual(meta["amazon_order_id"], oid)
        self.assertEqual(meta["csv"], "digital_refunds")

    def test_coupon_offset_yielding_zero_net_skipped(self):
        """Promotional return where every line cancels (e.g. a fully-
        discounted book returned) → net 0 → no transaction emitted."""
        oid = "D01-FREE0000-0000002"
        self._write_digital_returns([
            self._row(**{
                "Order ID": oid,
                "Monetary Component Type": "Price Amount",
                "Offer Type Code": "Not Applicable",
                "Transaction Amount": "5.00",
            }),
            self._row(**{
                "Order ID": oid,
                "Monetary Component Type": "Price Amount",
                "Offer Type Code": "Coupon",
                "Transaction Amount": "-5.00",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(txs, [])

    def test_non_usd_order_skipped(self):
        """Mirrors the digital_content non-USD guard. Currency field
        is `Base Currency` here, not `Price Currency Code`."""
        oid = "D01-BR000000-0000003"
        self._write_digital_returns([
            self._row(**{
                "Order ID": oid, "Base Currency": "BRL",
                "Monetary Component Type": "Price Amount",
                "Transaction Amount": "12.50",
            }),
            self._row(**{
                "Order ID": oid, "Base Currency": "BRL",
                "Monetary Component Type": "Tax",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        txs = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(txs, [])

    def test_reingest_same_file_is_idempotent(self):
        """File-level idempotency via processed_files: re-running with
        the same file produces no new rows."""
        self._write_digital_returns([
            self._row(**{
                "Monetary Component Type": "Price Amount",
                "Transaction Amount": "2.99",
            }),
            self._row(**{
                "Monetary Component Type": "Tax",
                "Transaction Amount": "0.0",
            }),
        ])
        ing = self._make_ingestor()
        first = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(len(first), 1)
        second = ing.ingest_profile(self.profile_dir, profile="test")
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
