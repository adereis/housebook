import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from housebook.core.database import Database
from housebook.core.models import Transaction
from housebook.core.reconciler import Reconciler


def _amazon_meta(order_id, csv_tag="orders"):
    """Build the JSON metadata blob the Amazon ingestor writes."""
    return json.dumps({"amazon_order_id": order_id, "csv": csv_tag})


def _bnpl_meta(order_id, count, downpayment):
    """Build the metadata blob for a BNPL-tagged Order History row.
    Mirrors what the Order History BNPL tagging writes."""
    return json.dumps({
        "amazon_order_id": order_id, "csv": "orders",
        "is_bnpl": True, "installment_count": count,
        "downpayment": downpayment,
    })


class TestReconciler(unittest.TestCase):

    def setUp(self):
        # Use a temporary file for the database
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.db = Database(self.db_path)
        self._init_mock_db()

        # Mock configuration for Reconciler
        self.mock_config = {
            "amazon_keywords": ["AMZN", "AMAZON"],
            "date_window_days": 3,
        }

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _init_mock_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE, description TEXT,
            amount REAL, category TEXT,
            source TEXT, status TEXT,
            original_file TEXT,
            trip_id INTEGER, needs_review BOOLEAN,
            profile TEXT, metadata TEXT
        )""")

        c.execute("""CREATE TABLE processed_files (
                        file_path TEXT PRIMARY KEY,
                        file_hash TEXT,
                        last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        statement_start DATE,
                        statement_end DATE
                    )""")
        c.execute("""CREATE TABLE ingestion_errors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_path TEXT,
                        line_number INTEGER,
                        raw_text TEXT,
                        error TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )""")
        conn.commit()
        conn.close()

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_reconcile_amazon_success(self, mock_load):
        mock_load.return_value = self.mock_config

        # 1. Add an Amazon CSV transaction — the source of truth.
        tx_amazon = Transaction(
            date="2025-01-01",
            description="Amazon: Echo Dot",
            amount=Decimal("49.99"),
            category="Shopping",
            source="Amazon",
            status="AGENT_VERIFIED",
            original_file="amazon.csv",
        )
        self.db.add_transaction(tx_amazon)

        # 2. Add a matching Bank transaction — the duplicate to hide.
        tx_bank = Transaction(
            date="2025-01-02",  # Within 3 day window
            description="AMZN MKTP US*12345",
            amount=Decimal("49.99"),  # Exact amount
            category="Miscellaneous",
            source="Amex",
            status="UNVERIFIED",
            original_file="amex.pdf",
        )
        self.db.add_transaction(tx_bank)

        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()

        self.assertEqual(matches, 1)
        self.assertEqual(orphans, 0)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Bank row is hidden + recategorized.
        c.execute(
            "SELECT category, status, needs_review "
            "FROM transactions WHERE source = 'Amex'"
        )
        row = c.fetchone()
        self.assertEqual(row["category"], "Transfers & Refunds")
        self.assertEqual(row["status"], "RECONCILED")
        self.assertEqual(row["needs_review"], 0)

        # Amazon CSV row stays as source of truth — UNTOUCHED.
        # Reconciler must never modify the Amazon side.
        c.execute(
            "SELECT status, category FROM transactions WHERE source = 'Amazon'"
        )
        row = c.fetchone()
        self.assertEqual(row["status"], "AGENT_VERIFIED")
        self.assertEqual(row["category"], "Shopping")
        conn.close()

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_reconcile_amazon_no_match_amount(self, mock_load):
        mock_load.return_value = self.mock_config

        self.db.add_transaction(Transaction(
            "2025-01-01", "Amazon: Item",
            Decimal("10.00"), "Cat", "Amazon", "Status", "file"
        ))
        self.db.add_transaction(Transaction(
            "2025-01-01", "AMZN",
            Decimal("11.00"), "Misc", "Bank", "Status", "file"
        ))

        reconciler = Reconciler(self.db)
        matches, _ = reconciler.reconcile_amazon()
        self.assertEqual(matches, 0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_reconcile_amazon_no_match_date(self, mock_load):
        mock_load.return_value = self.mock_config

        self.db.add_transaction(Transaction(
            "2025-01-01", "Amazon: Item",
            Decimal("10.00"), "Cat", "Amazon", "Status", "file"
        ))
        self.db.add_transaction(Transaction(
            "2025-01-10", "AMZN",
            Decimal("10.00"), "Misc", "Bank", "Status", "file"
        ))

        reconciler = Reconciler(self.db)
        matches, _ = reconciler.reconcile_amazon()
        self.assertEqual(matches, 0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_reconcile_amazon_dry_run_writes_nothing(self, mock_load):
        mock_load.return_value = self.mock_config

        self.db.add_transaction(Transaction(
            "2025-01-01", "Amazon: Echo Dot",
            Decimal("49.99"), "Shopping", "Amazon", "AI_VERIFIED", "amazon.csv"
        ))
        self.db.add_transaction(Transaction(
            "2025-01-02", "AMZN MKTP US*12345",
            Decimal("49.99"), "Miscellaneous", "Amex", "UNVERIFIED", "amex.pdf"
        ))

        reconciler = Reconciler(self.db)
        matches, _ = reconciler.reconcile_amazon(dry_run=True)

        # The match is reported and recorded for previewing ...
        self.assertEqual(matches, 1)
        self.assertEqual(len(reconciler.last_matches), 1)
        self.assertEqual(reconciler.last_matches[0]["amount"], 49.99)

        # ... but the database is left completely untouched.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            "SELECT category, status, needs_review "
            "FROM transactions WHERE source = 'Amex'"
        )
        row = c.fetchone()
        self.assertEqual(row["category"], "Miscellaneous")
        self.assertEqual(row["status"], "UNVERIFIED")
        c.execute("SELECT status FROM transactions WHERE source = 'Amazon'")
        self.assertEqual(c.fetchone()["status"], "AI_VERIFIED")
        conn.close()

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_reconcile_amazon_consumed_row_tracking(self, mock_load):
        """One Amazon CSV row must not be claimed by multiple bank rows.

        Setup: one $25 Amazon CSV row, two $25 bank-side Amazon rows
        within the date window. Without consumed-row tracking, the
        old reconciler would have matched both bank rows to the same
        Amazon row, over-counting matches. With tracking, only one
        bank row matches; the other is reported as an orphan.
        """
        mock_load.return_value = self.mock_config

        self.db.add_transaction(Transaction(
            "2025-01-01", "Amazon: Single Item",
            Decimal("25.00"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ))
        self.db.add_transaction(Transaction(
            "2025-01-02", "AMZN MKTP US*A",
            Decimal("25.00"), "Misc", "Amex", "UNVERIFIED", "amex.pdf",
        ))
        self.db.add_transaction(Transaction(
            "2025-01-03", "AMZN MKTP US*B",
            Decimal("25.00"), "Misc", "Amex", "UNVERIFIED", "amex.pdf",
        ))

        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()

        # Exactly one match (not two — the second bank row cannot
        # double-claim the same Amazon CSV row).
        self.assertEqual(matches, 1)
        self.assertEqual(orphans, 1)
        self.assertEqual(len(reconciler.last_matches), 1)
        self.assertEqual(len(reconciler.last_orphans), 1)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_reconcile_amazon_orphans_reported(self, mock_load):
        """Bank-Amazon rows with no CSV counterpart are flagged."""
        mock_load.return_value = self.mock_config

        # Bank-side Amazon row, no matching Amazon CSV row at all.
        self.db.add_transaction(Transaction(
            "2025-01-02", "AMZN MKTP US*ORPHAN",
            Decimal("12.34"), "Misc", "Amex", "UNVERIFIED", "amex.pdf",
        ))

        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()

        self.assertEqual(matches, 0)
        self.assertEqual(orphans, 1)
        orphan = reconciler.last_orphans[0]
        self.assertEqual(orphan["bank_source"], "Amex")
        self.assertEqual(orphan["amount"], 12.34)

        # Orphan must remain visible (not auto-hidden) for user review.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT status, category FROM transactions WHERE source='Amex'")
        row = c.fetchone()
        self.assertEqual(row["status"], "UNVERIFIED")
        self.assertEqual(row["category"], "Misc")
        conn.close()

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_exclusion_pattern_drops_office_cafe_from_orphans(
        self, mock_load,
    ):
        """A bank row matching an Amazon keyword but also matching an
        exclusion pattern is not treated as a candidate Amazon row at
        all — it's a real merchant whose name happens to contain
        'AMAZON' (the Amazon office cafeteria, in this case).

        Without the exclusion the row would be reported as an orphan,
        cluttering the reconcile output with non-issues.
        """
        mock_load.return_value = {
            "amazon_keywords": ["AMZN", "AMAZON"],
            "amazon_exclusion_patterns": ["XYZ01"],
            "date_window_days": 3,
        }
        self.db.add_transaction(Transaction(
            "2023-12-05", "AMAZON XYZ01 CAFE ANYTOWN",
            Decimal("8.25"), "Restaurants", "Chase",
            "AGENT_VERIFIED", "chase.pdf",
        ))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        self.assertEqual(matches, 0)
        self.assertEqual(orphans, 0)
        self.assertEqual(reconciler.last_orphans, [])

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_exclusion_pattern_is_case_insensitive(self, mock_load):
        """The exclusion list is matched case-insensitively against
        the bank description; configuring 'XYZ01' should match a
        description that uses any casing for the substring."""
        mock_load.return_value = {
            "amazon_keywords": ["AMAZON"],
            "amazon_exclusion_patterns": ["xyz01"],
            "date_window_days": 3,
        }
        self.db.add_transaction(Transaction(
            "2024-01-01", "AMAZON Xyz01 Cafe Anytown",
            Decimal("4.25"), "Restaurants", "Chase",
            "AGENT_VERIFIED", "chase.pdf",
        ))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        self.assertEqual(orphans, 0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_reconcile_amazon_skips_already_reconciled_bank_rows(self, mock_load):
        """Bank rows already RECONCILED in a prior run are skipped."""
        mock_load.return_value = self.mock_config

        self.db.add_transaction(Transaction(
            "2025-01-01", "Amazon: Item",
            Decimal("10.00"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ))
        self.db.add_transaction(Transaction(
            "2025-01-02", "AMZN MKTP US*PRIOR",
            Decimal("10.00"), "Transfers & Refunds", "Amex",
            "RECONCILED", "amex.pdf",
        ))

        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()

        # The bank row was already reconciled; reconciler must not
        # re-touch it. The Amazon row is therefore still available
        # but has no other bank row to pair with.
        self.assertEqual(matches, 0)
        self.assertEqual(orphans, 0)


class TestReconcilerAggregateOrderId(unittest.TestCase):
    """Tests for the pass-1 aggregate-by-Order-ID matcher.

    The fuzzy per-row matcher cannot pair a single bank charge with a
    multi-line physical order — a $26.00 bank charge has no $26.00
    CSV row, only two CSV rows summing to $26.00. Aggregating by
    `metadata.amazon_order_id` closes that gap.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.db = Database(self.db_path)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE, description TEXT,
            amount REAL, category TEXT,
            source TEXT, status TEXT,
            original_file TEXT,
            trip_id INTEGER, needs_review BOOLEAN,
            profile TEXT, metadata TEXT
        )""")
        c.execute("""CREATE TABLE processed_files (
            file_path TEXT PRIMARY KEY,
            file_hash TEXT,
            last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            statement_start DATE, statement_end DATE
        )""")
        c.execute("""CREATE TABLE ingestion_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT, line_number INTEGER,
            raw_text TEXT, error TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        conn.close()
        self.mock_config = {
            "amazon_keywords": ["AMZN", "AMAZON"],
            "date_window_days": 3,
        }

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _add(self, tx, metadata=None):
        """Add a transaction, optionally setting its metadata field.

        `Database.add_transaction` already binds `tx.metadata` on
        INSERT — just stamp the dataclass field before calling it.
        """
        if metadata is not None:
            tx.metadata = metadata
        self.db.add_transaction(tx)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_aggregate_match_sums_multi_line_order(self, mock_load):
        """Two CSV rows for one Order ID (18.40 + 7.60 = 26.00) match
        a single 26.00 bank charge — the core new behavior."""
        mock_load.return_value = self.mock_config
        oid = "112-7777777-7777777"
        self._add(Transaction(
            "2024-12-21", "Amazon: Fresh Step Cat Litter",
            Decimal("18.40"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_amazon_meta(oid))
        self._add(Transaction(
            "2024-12-21", "Amazon: Painters Tape",
            Decimal("7.60"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_amazon_meta(oid))
        self._add(Transaction(
            "2024-12-21", "AMAZON MKTPL*AB12CD34E",
            Decimal("26.00"), "Misc", "Chase-Amazon", "UNVERIFIED",
            "chase.pdf",
        ))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()

        self.assertEqual(matches, 1)
        self.assertEqual(orphans, 0)
        m = reconciler.last_matches[0]
        self.assertEqual(m["matcher"], "aggregate")
        self.assertEqual(m["amazon_order_id"], oid)
        self.assertEqual(m["n_lines"], 2)
        self.assertEqual(m["amount"], 26.00)

        # Bank-side hidden; both CSV rows untouched.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        bank = c.execute(
            "SELECT status, category FROM transactions "
            "WHERE source='Chase-Amazon'"
        ).fetchone()
        self.assertEqual(bank["status"], "RECONCILED")
        self.assertEqual(bank["category"], "Transfers & Refunds")
        n_csv_touched = c.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE source='Amazon' AND status != 'AGENT_VERIFIED'"
        ).fetchone()[0]
        self.assertEqual(n_csv_touched, 0)
        conn.close()

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_aggregate_consumes_all_lines_so_fuzzy_cannot_reclaim(
        self, mock_load,
    ):
        """After an aggregate match, NONE of the order's CSV rows
        may be re-claimed by a later fuzzy match. Without this,
        a second bank charge at the same individual-row amount
        would mis-pair against an already-spoken-for CSV row."""
        mock_load.return_value = self.mock_config
        oid = "112-MULTI00-LINE0001"
        # 3-line order summing to 50.00 (10 + 15 + 25)
        for amt in (Decimal("10.00"), Decimal("15.00"), Decimal("25.00")):
            self._add(Transaction(
                "2025-03-15", "Amazon: Item",
                amt, "Shopping", "Amazon", "AGENT_VERIFIED",
                "amazon.csv",
            ), metadata=_amazon_meta(oid))
        # Bank charge at the order total
        self._add(Transaction(
            "2025-03-15", "AMAZON MKTPL*AGG00001",
            Decimal("50.00"), "Misc", "Chase-Amazon", "UNVERIFIED",
            "chase.pdf",
        ))
        # Second bank charge at one of the line amounts — must NOT
        # match the (already-consumed) $10 CSV row.
        self._add(Transaction(
            "2025-03-15", "AMAZON MKTPL*ORPHAN02",
            Decimal("10.00"), "Misc", "Chase-Amazon", "UNVERIFIED",
            "chase.pdf",
        ))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        self.assertEqual(matches, 1)
        self.assertEqual(orphans, 1)
        self.assertEqual(reconciler.last_matches[0]["matcher"], "aggregate")
        self.assertEqual(reconciler.last_orphans[0]["amount"], 10.0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_aggregate_pass_runs_before_fuzzy_pass(self, mock_load):
        """Pass 1 (aggregate) processes ALL bank rows before pass 2
        (fuzzy) runs. So when an order's total matches one bank
        charge AND one of its CSV-row amounts matches a different
        bank charge, the aggregate wins — even if the fuzzy-matching
        bank row appears first in iteration order. The fuzzy bank
        row becomes the orphan."""
        mock_load.return_value = self.mock_config
        oid = "112-PRIORI00-PASS0002"
        # 2-line order: $10 + $20 = $30
        self._add(Transaction(
            "2025-04-01", "Amazon: Line A",
            Decimal("10.00"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_amazon_meta(oid))
        self._add(Transaction(
            "2025-04-01", "Amazon: Line B",
            Decimal("20.00"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_amazon_meta(oid))
        # Insert the would-be-fuzzy bank row FIRST so iteration sees
        # it before the aggregate-matching one. Demonstrates that
        # the two-pass split is what dictates priority, not order.
        self._add(Transaction(
            "2025-04-01", "AMAZON MKTPL*WOULDBE_FUZZY",
            Decimal("10.00"), "Misc", "Chase-Amazon", "UNVERIFIED",
            "chase.pdf",
        ))
        self._add(Transaction(
            "2025-04-01", "AMAZON MKTPL*WOULDBE_AGG",
            Decimal("30.00"), "Misc", "Chase-Amazon", "UNVERIFIED",
            "chase.pdf",
        ))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        # Aggregate wins; fuzzy bank row is the orphan.
        self.assertEqual(matches, 1)
        self.assertEqual(orphans, 1)
        self.assertEqual(
            reconciler.last_matches[0]["matcher"], "aggregate",
        )
        self.assertEqual(
            reconciler.last_matches[0]["amount"], 30.0,
        )
        self.assertEqual(reconciler.last_orphans[0]["amount"], 10.0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_aggregate_excludes_refund_rows_from_sum(self, mock_load):
        """A refund row's metadata carries the original order's Order
        ID (with `csv='refunds'`) and a NEGATIVE amount. Including it
        would zero out a paid order's aggregate. The matcher must
        only sum `csv in ('orders','digital')`."""
        mock_load.return_value = self.mock_config
        oid = "112-REFUND00-CASE003"
        self._add(Transaction(
            "2025-05-01", "Amazon: Item",
            Decimal("18.40"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_amazon_meta(oid, csv_tag="orders"))
        self._add(Transaction(
            "2025-05-01", "Amazon: Item",
            Decimal("7.60"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_amazon_meta(oid, csv_tag="orders"))
        # Refund came later. Carries same Order ID, csv='refunds'.
        self._add(Transaction(
            "2025-05-10", "Amazon REFUND: Item",
            Decimal("-26.00"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_amazon_meta(oid, csv_tag="refunds"))
        # Bank charge for the original purchase, dated near the order.
        self._add(Transaction(
            "2025-05-01", "AMAZON MKTPL*REFUNDCASE",
            Decimal("26.00"), "Misc", "Chase-Amazon", "UNVERIFIED",
            "chase.pdf",
        ))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        # Aggregate must equal $26.00 (purchases only), not $0.
        self.assertEqual(matches, 1)
        self.assertEqual(orphans, 0)
        m = reconciler.last_matches[0]
        self.assertEqual(m["matcher"], "aggregate")
        self.assertEqual(m["n_lines"], 2)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_fuzzy_fallback_still_works_when_metadata_null(
        self, mock_load,
    ):
        """Legacy rows with NULL metadata fall through pass 1 and
        match via pass 2 — backward-compat with un-backfilled data."""
        mock_load.return_value = self.mock_config
        # Both rows have NULL metadata (no _add helper kwarg).
        self._add(Transaction(
            "2025-06-01", "Amazon: Legacy Item",
            Decimal("49.99"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ))
        self._add(Transaction(
            "2025-06-02", "AMZN MKTP US*LEGACY",
            Decimal("49.99"), "Misc", "Amex", "UNVERIFIED", "amex.pdf",
        ))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        self.assertEqual(matches, 1)
        self.assertEqual(orphans, 0)
        self.assertEqual(reconciler.last_matches[0]["matcher"], "fuzzy")


class TestReconcilerBnpl(unittest.TestCase):
    """Tests for the pass-0 BNPL matcher.

    A BNPL order has a single gross Order History row paired against
    N bank-side installment charges (typically 1 downpayment + (N-1)
    regular installments over several months). The aggregate matcher
    can't bridge that because the sum is N small bank rows vs. one
    gross CSV row — opposite shape from the aggregate-by-Order-ID
    case. The BNPL pass uses the plan metadata to pair them.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.db = Database(self.db_path)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE, description TEXT,
            amount REAL, category TEXT,
            source TEXT, status TEXT,
            original_file TEXT,
            trip_id INTEGER, needs_review BOOLEAN,
            profile TEXT, metadata TEXT
        )""")
        c.execute("""CREATE TABLE processed_files (
            file_path TEXT PRIMARY KEY,
            file_hash TEXT,
            last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            statement_start DATE, statement_end DATE
        )""")
        c.execute("""CREATE TABLE ingestion_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT, line_number INTEGER,
            raw_text TEXT, error TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        conn.close()
        self.mock_config = {
            "amazon_keywords": ["AMZN", "AMAZON"],
            "date_window_days": 3,
        }

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _add(self, tx, metadata=None):
        if metadata is not None:
            tx.metadata = metadata
        self.db.add_transaction(tx)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_bnpl_pass_pairs_all_installments_with_gross(self, mock_load):
        """A 5-installment order: gross $260.00, 5 installments
        (downpayment $60.00 + 4× $50.00). All 4 currently-charged bank
        installments should match in one BNPL pass."""
        mock_load.return_value = self.mock_config
        oid = "112-8888888-8888888"
        self._add(Transaction(
            "2026-01-25", "Amazon: Cordless Blower Attachment",
            Decimal("260.00"), "Shopping", "Amazon", "AGENT_VERIFIED",
            "amazon.csv",
        ), metadata=_bnpl_meta(oid, 5, 60.00))
        # 4 bank installments — downpayment + 3 regular monthlies.
        for date, amt in [
            ("2026-02-08", "60.00"),
            ("2026-03-10", "50.00"),
            ("2026-04-09", "50.00"),
            ("2026-05-09", "50.00"),
        ]:
            self._add(Transaction(
                date, "Amazon.com AMZN.COM/BILL WA",
                Decimal(amt), "Misc", "Chase-Amazon",
                "AGENT_VERIFIED", "chase.pdf",
            ))

        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()

        self.assertEqual(matches, 4)
        self.assertEqual(orphans, 0)
        for m in reconciler.last_matches:
            self.assertEqual(m["matcher"], "bnpl")
            self.assertEqual(m["amazon_order_id"], oid)

        # The gross Amazon row is NOT touched — CSV stays source of truth.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        amazon = conn.execute(
            "SELECT status FROM transactions WHERE source='Amazon'"
        ).fetchone()
        conn.close()
        self.assertEqual(amazon["status"], "AGENT_VERIFIED")

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_bnpl_pass_allows_partial_coverage(self, mock_load):
        """If only the first 2 of 5 installments have hit the card,
        those 2 still match. The plan stays open for the remaining 3
        to be picked up on a future reconcile run."""
        mock_load.return_value = self.mock_config
        oid = "112-8888888-8888888"
        self._add(Transaction(
            "2026-01-25", "Amazon: Cordless Tool", Decimal("260.00"), "Shopping",
            "Amazon", "AGENT_VERIFIED", "amazon.csv",
        ), metadata=_bnpl_meta(oid, 5, 60.00))
        self._add(Transaction(
            "2026-02-08", "Amazon.com AMZN.COM/BILL WA",
            Decimal("60.00"), "Misc", "Chase-Amazon", "AGENT_VERIFIED",
            "chase.pdf",
        ))
        self._add(Transaction(
            "2026-03-10", "Amazon.com AMZN.COM/BILL WA",
            Decimal("50.00"), "Misc", "Chase-Amazon", "AGENT_VERIFIED",
            "chase.pdf",
        ))

        reconciler = Reconciler(self.db)
        matches, _ = reconciler.reconcile_amazon()
        self.assertEqual(matches, 2)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_bnpl_safety_rejects_overshoot(self, mock_load):
        """If candidate installments sum to MORE than the gross
        (suggesting amount-collision with unrelated bank rows), the
        whole plan is abandoned for this pass rather than match
        partially. Aggregate / fuzzy passes then get their shot."""
        mock_load.return_value = self.mock_config
        oid = "112-8888888-8888888"
        # Gross only $100 but the user happens to have 3 bank rows at
        # exactly the regular-installment amount within the date
        # window — sum $150 > gross. Don't claim.
        self._add(Transaction(
            "2026-01-25", "Amazon: Small Item", Decimal("100.00"),
            "Shopping", "Amazon", "AGENT_VERIFIED", "amazon.csv",
        ), metadata=_bnpl_meta(oid, 5, 20.00))
        # regular = (100 - 20) / 4 = 20.00 — same as downpayment!
        # Three $20 bank rows = $60 > $100? No, that's under.
        # Let's overshoot: 6 candidate rows at $20 = $120 > $100.
        for date in [
            "2026-02-08", "2026-03-10", "2026-04-09",
            "2026-05-09", "2026-06-09", "2026-07-09",
        ]:
            self._add(Transaction(
                date, "Amazon.com AMZN.COM/BILL WA",
                Decimal("20.00"), "Misc", "Chase-Amazon",
                "AGENT_VERIFIED", "chase.pdf",
            ))

        reconciler = Reconciler(self.db)
        matches, _ = reconciler.reconcile_amazon()
        # BNPL pass refuses (overshoot). Aggregate/fuzzy can still
        # try, but no single bank row equals $100, so 0 matches.
        n_bnpl = sum(
            1 for m in reconciler.last_matches
            if m.get("matcher") == "bnpl"
        )
        self.assertEqual(n_bnpl, 0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_non_bnpl_orders_ignored_by_pass0(self, mock_load):
        """A regular (non-BNPL) order with the same merchant name
        and a same-amount bank row inside the BNPL date window must
        NOT be claimed by the BNPL pass — fall through to aggregate
        or fuzzy."""
        mock_load.return_value = self.mock_config
        oid = "112-9999999-9999999"
        self._add(Transaction(
            "2026-01-25", "Amazon: Coffee Mug", Decimal("12.50"),
            "Shopping", "Amazon", "AGENT_VERIFIED", "amazon.csv",
        ), metadata=_amazon_meta(oid))
        self._add(Transaction(
            "2026-01-27", "Amazon.com AMZN.COM/BILL WA",
            Decimal("12.50"), "Misc", "Chase-Amazon", "AGENT_VERIFIED",
            "chase.pdf",
        ))

        reconciler = Reconciler(self.db)
        matches, _ = reconciler.reconcile_amazon()
        self.assertEqual(matches, 1)
        # Must be aggregate (1-line single match), not bnpl.
        self.assertEqual(
            reconciler.last_matches[0]["matcher"], "aggregate",
        )

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_bnpl_refuses_when_installments_outnumber_plan(
        self, mock_load,
    ):
        """An unrelated charge sharing the installment amount must not
        be hidden as a duplicate.

        Gross $260.00, 5 charges (downpayment $60.00 + 4x $50.00). Here
        the downpayment predates ingested coverage, 4 real installments
        posted, and a 5th unrelated $50.00 Amazon charge sits in the
        window. 5 x 50.00 = $250.00 is under gross, so the total-based
        safety check alone would let all five through — hiding a real
        expense. Nothing distinguishes the impostor, so refuse.
        """
        mock_load.return_value = self.mock_config
        oid = "112-8888888-8888888"
        self._add(Transaction(
            "2026-01-25", "Amazon: Cordless Mower", Decimal("260.00"),
            "Shopping", "Amazon", "AGENT_VERIFIED", "amazon.csv",
        ), metadata=_bnpl_meta(oid, 5, 60.00))
        # No downpayment row (predates coverage); five $50.00 rows —
        # one more than the plan's 4 installments.
        for date in ["2026-02-08", "2026-03-10", "2026-04-09",
                     "2026-05-09", "2026-06-09"]:
            self._add(Transaction(
                date, "Amazon.com AMZN.COM/BILL WA", Decimal("50.00"),
                "Misc", "Chase-Amazon", "AGENT_VERIFIED", "chase.pdf",
            ))

        reconciler = Reconciler(self.db)
        reconciler.reconcile_amazon()
        n_bnpl = sum(1 for m in reconciler.last_matches
                     if m.get("matcher") == "bnpl")
        self.assertEqual(n_bnpl, 0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_bnpl_matches_final_installment_with_rounding(
        self, mock_load,
    ):
        """The last installment absorbs the rounding remainder.

        Gross $120, downpayment $20 → $100 over 4 installments = 33.33,
        33.33, 33.34. Matching only the quantized $33.33 left the
        $33.34 charge a permanent orphan on every future run.
        """
        mock_load.return_value = self.mock_config
        oid = "112-3333333-3333333"
        self._add(Transaction(
            "2026-01-25", "Amazon: Rounding Case", Decimal("120.00"),
            "Shopping", "Amazon", "AGENT_VERIFIED", "amazon.csv",
        ), metadata=_bnpl_meta(oid, 4, 20.00))
        for date, amt in [
            ("2026-01-26", "20.00"),
            ("2026-02-25", "33.33"),
            ("2026-03-25", "33.33"),
            ("2026-04-25", "33.34"),
        ]:
            self._add(Transaction(
                date, "Amazon.com AMZN.COM/BILL WA", Decimal(amt),
                "Misc", "Chase-Amazon", "AGENT_VERIFIED", "chase.pdf",
            ))

        reconciler = Reconciler(self.db)
        reconciler.reconcile_amazon()
        bnpl = [m for m in reconciler.last_matches
                if m.get("matcher") == "bnpl"]
        self.assertEqual(len(bnpl), 4)
        self.assertIn(33.34, [m["amount"] for m in bnpl])

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_bnpl_metadata_garbage_downpayment_skips_plan(
        self, mock_load,
    ):
        """A non-numeric downpayment must skip the plan, not crash.

        decimal.InvalidOperation is an ArithmeticError, not a
        ValueError, so it escaped the tag-parsing guard and aborted the
        entire reconcile run.
        """
        mock_load.return_value = self.mock_config
        self._add(Transaction(
            "2026-01-25", "Amazon: Item", Decimal("50.00"), "Shopping",
            "Amazon", "AGENT_VERIFIED", "amazon.csv",
        ), metadata=json.dumps({
            "amazon_order_id": "112-8888888-8888888", "csv": "orders",
            "is_bnpl": True, "installment_count": 3,
            "downpayment": "not-a-number",
        }))
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        self.assertEqual(matches, 0)

    @patch("housebook.core.reconciler.Reconciler._load_json")
    def test_bnpl_metadata_missing_count_falls_back(self, mock_load):
        """If a row carries `is_bnpl=true` but is missing
        installment_count or downpayment (corrupted tag), skip the
        plan rather than crash. The aggregate matcher can still try
        the gross amount."""
        mock_load.return_value = self.mock_config
        oid = "112-7777777-7777777"
        self._add(Transaction(
            "2026-01-25", "Amazon: Item", Decimal("50.00"), "Shopping",
            "Amazon", "AGENT_VERIFIED", "amazon.csv",
        ), metadata=json.dumps({
            "amazon_order_id": oid, "csv": "orders",
            "is_bnpl": True,
            # installment_count + downpayment missing
        }))
        # No bank rows — just verify it doesn't crash.
        reconciler = Reconciler(self.db)
        matches, orphans = reconciler.reconcile_amazon()
        self.assertEqual(matches, 0)
        self.assertEqual(orphans, 0)


if __name__ == "__main__":
    unittest.main()
