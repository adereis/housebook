import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from housebook.audit import (
    _backup,
    _parse_id_ranges,
    cmd_add_manual,
    cmd_apply_rules,
    cmd_assign,
    cmd_calibrate,
    cmd_close_project,
    cmd_create_project,
    cmd_create_trip,
    cmd_detect_trips,
    cmd_edit_project,
    cmd_link,
    cmd_link_amazon_refunds,
    cmd_linked,
    cmd_match_project,
    cmd_pending,
    cmd_project_summary,
    cmd_projects,
    cmd_summary,
    cmd_trips,
    cmd_unlink,
    cmd_verify,
)


def _printed(mock_print):
    """Join all text passed to a mocked print() into one string."""
    return "\n".join(
        str(c.args[0]) if c.args else "" for c in mock_print.call_args_list
    )


def _create_test_db(db_path):
    """Create a minimal schema matching production."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE,
            description TEXT,
            amount REAL,
            category TEXT,
            source TEXT,
            original_file TEXT,
            status TEXT DEFAULT 'UNVERIFIED',
            trip_id INTEGER,
            project_id INTEGER,
            needs_review BOOLEAN DEFAULT 1,
            profile TEXT,
            metadata TEXT,
            linked_transaction_id INTEGER REFERENCES transactions(id)
        );
        CREATE TABLE trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            start_date DATE,
            end_date DATE,
            status TEXT DEFAULT 'confirmed',
            type TEXT DEFAULT 'unknown',
            location TEXT,
            created_by TEXT DEFAULT 'manual'
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            type TEXT NOT NULL DEFAULT 'unknown',
            location TEXT,
            start_date DATE,
            end_date DATE,
            status TEXT NOT NULL DEFAULT 'open',
            match_keywords TEXT,
            match_categories TEXT,
            budget REAL,
            created_by TEXT NOT NULL DEFAULT 'agent',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        );
        CREATE TABLE manual_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE,
            frequency TEXT NOT NULL DEFAULT 'one-time',
            project_id INTEGER
        );
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY
        );
        INSERT INTO schema_version VALUES (8);
    """)
    conn.commit()
    conn.close()


def _seed_transactions(db_path, txns):
    """Insert test transactions. Each txn is a tuple:
    (date, description, amount, category, source, status, needs_review)
    """
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    for t in txns:
        c.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, "
            "status, needs_review, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*t[:7], t[7] if len(t) > 7 else None),
        )
    conn.commit()
    conn.close()


def _seed_trips(db_path, trips):
    """Insert test trips. Each trip is a tuple:
    (name, start_date, end_date, type, location)
    """
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    for t in trips:
        c.execute(
            "INSERT INTO trips (name, start_date, end_date, type, location) "
            "VALUES (?, ?, ?, ?, ?)", t,
        )
    conn.commit()
    conn.close()


class _Args:
    """Minimal args namespace for testing subcommands."""
    def __init__(self, **kwargs):
        self.json_output = False
        self.db_path = None
        self.source = None
        self.limit = 10
        self.force = False
        self.trip = None
        self.project = None
        # edit-project optional fields (default None = "leave unchanged")
        for attr in ("name", "start", "end", "location", "description",
                     "budget", "status", "keywords", "categories"):
            setattr(self, attr, None)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestParseIdRanges(unittest.TestCase):

    def test_single_id(self):
        self.assertEqual(_parse_id_ranges("42"), [42])

    def test_comma_separated(self):
        self.assertEqual(_parse_id_ranges("1,3,5"), [1, 3, 5])

    def test_range(self):
        self.assertEqual(_parse_id_ranges("10-13"), [10, 11, 12, 13])

    def test_mixed(self):
        result = _parse_id_ranges("1,5-7,10")
        self.assertEqual(result, [1, 5, 6, 7, 10])


class TestCmdPending(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_transactions(self.db_path, [
            ("2026-03-08", "Uber Trip", 64.00, "Local Transit",
             "Amex", "UNVERIFIED", 1),
            ("2026-03-15", "UNITED AIRLINES", 612.00, "Miscellaneous",
             "Amex", "UNVERIFIED", 1),
            ("2026-02-13", "T.J. MAXX", 48.00, "Shopping & Retail",
             "Home Goods", "UNVERIFIED", 1),
            ("2026-01-15", "Amazon Purchase", 25.00, "Shopping & Retail",
             "Amazon", "AGENT_VERIFIED", 0),
        ])

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_lists_only_unverified(self):
        args = _Args(db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mock_print:
            cmd_pending(args)
        output = json.loads(mock_print.call_args[0][0])
        # 3 UNVERIFIED txns; the AGENT_VERIFIED one is excluded
        self.assertEqual(len(output), 3)
        descs = {item["description"] for item in output}
        self.assertIn("Uber Trip", descs)
        self.assertNotIn("Amazon Purchase", descs)

    def test_filter_by_source(self):
        args = _Args(db_path=self.db_path, json_output=True,
                      source="Home Goods")
        with patch("builtins.print") as mock_print:
            cmd_pending(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["source"], "Home Goods")

    def test_human_output_runs(self):
        args = _Args(db_path=self.db_path)
        with patch("builtins.print"):
            cmd_pending(args)  # Should not raise


class TestCmdCalibrate(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_transactions(self.db_path, [
            ("2026-01-01", "Store A", 10.0, "Groceries",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-01-02", "Store B", 20.0, "Groceries",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-01-03", "Gas Station", 40.0, "Auto & Fuel",
             "BoA", "AGENT_VERIFIED", 0),
            ("2026-01-04", "Pending Item", 5.0, "Groceries",
             "Amex", "UNVERIFIED", 1),
        ])

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_shows_only_verified(self):
        args = _Args(db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mock_print:
            cmd_calibrate(args)
        output = json.loads(mock_print.call_args[0][0])
        categories = {r["category"]: r["count"] for r in output}
        self.assertEqual(categories["Groceries"], 2)
        self.assertEqual(categories["Auto & Fuel"], 1)


class TestCmdTrips(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_trips(self.db_path, [
            ("Work Trip", "2026-03-01", "2026-03-05",
             "work", "Denver, CO"),
            ("Vacation", "2026-04-09", "2026-04-28",
             "personal", "Italy, France"),
        ])

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_lists_trips(self):
        args = _Args(db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(output), 2)
        names = {r["name"] for r in output}
        self.assertIn("Vacation", names)

    def test_limit(self):
        args = _Args(db_path=self.db_path, json_output=True, limit=1)
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(output), 1)


class TestCmdCreateTrip(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_creates_trip(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            name="Test Trip", start="2026-05-01",
            end="2026-05-10", type="personal",
            location="Paris",
        )
        with patch("builtins.print") as mock_print:
            cmd_create_trip(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["name"], "Test Trip")
        self.assertIn("id", output)

        # Verify in DB
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM trips WHERE id = ?", (output["id"],)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)


class TestCmdVerify(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_transactions(self.db_path, [
            ("2026-03-08", "Uber Trip", 64.00, "Local Transit",
             "Amex", "UNVERIFIED", 1),
            ("2026-03-15", "UNITED AIRLINES", 612.00, "Miscellaneous",
             "Amex", "UNVERIFIED", 1),
            ("2026-03-15", "UNITED AIRLINES 2", 612.00, "Miscellaneous",
             "Amex", "UNVERIFIED", 1),
        ])
        _seed_trips(self.db_path, [
            ("Test Trip", "2026-03-01", "2026-03-20",
             "personal", "Italy"),
        ])

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_verify_with_category(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            ids="2,3", category="Flights", trip=None,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print") as mock_print:
                cmd_verify(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["updated"], 2)

        # Check DB state
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM transactions WHERE id IN (2, 3)"
        ).fetchall()
        conn.close()
        for r in rows:
            self.assertEqual(r["status"], "AGENT_VERIFIED")
            self.assertEqual(r["needs_review"], 0)
            self.assertEqual(r["category"], "Flights")

    def test_verify_with_trip_assignment(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            ids="2-3", category="Flights", trip=1,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print") as mock_print:
                cmd_verify(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["trip_id"], 1)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT trip_id FROM transactions WHERE id IN (2, 3)"
        ).fetchall()
        conn.close()
        for r in rows:
            self.assertEqual(r["trip_id"], 1)

    def test_verify_missing_ids_fails(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            ids="999", category="Flights", trip=None,
        )
        with self.assertRaises(SystemExit):
            with patch("builtins.print"):
                cmd_verify(args)

    def test_verify_already_verified_blocked(self):
        # First verify ID 1
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE transactions SET status='AGENT_VERIFIED', "
            "needs_review=0 WHERE id=1"
        )
        conn.commit()
        conn.close()

        args = _Args(
            db_path=self.db_path, json_output=True,
            ids="1", category="Flights", trip=None,
        )
        with self.assertRaises(SystemExit):
            with patch("builtins.print"):
                cmd_verify(args)

    def test_verify_already_verified_with_force(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE transactions SET status='AGENT_VERIFIED', "
            "needs_review=0 WHERE id=1"
        )
        conn.commit()
        conn.close()

        args = _Args(
            db_path=self.db_path, json_output=True,
            ids="1", category="Work (Reimbursable)",
            trip=None, force=True,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print") as mock_print:
                cmd_verify(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["updated"], 1)

    def test_verify_without_category_keeps_existing(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            ids="1", category=None, trip=None,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print"):
                cmd_verify(args)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "AGENT_VERIFIED")
        self.assertEqual(row["category"], "Local Transit")


class TestCmdSummary(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_transactions(self.db_path, [
            ("2026-03-08", "Uber Trip", 64.00, "Work (Reimbursable)",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-03-15", "UNITED AIRLINES", 612.00, "Flights",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-03-28", "Pending Item", 50.00, "Miscellaneous",
             "Amex", "UNVERIFIED", 1),
        ])

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_summary_counts(self):
        args = _Args(db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mock_print:
            cmd_summary(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["verified_count"], 2)
        self.assertEqual(output["remaining_unreviewed"], 1)
        self.assertIn("Work (Reimbursable)", output["by_category"])


class TestCmdLink(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_transactions(self.db_path, [
            ("2026-03-08", "Hotel Booking", 500.00, "Lodging",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-04-02", "Hotel Refund", -500.00, "Transfers & Refunds",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-03-10", "Flight", 300.00, "Flights",
             "Amex", "AGENT_VERIFIED", 0),
        ])

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_link_creates_bidirectional(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            purchase_id=1, refund_id=2,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print") as mock_print:
                cmd_link(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["purchase"]["id"], 1)
        self.assertEqual(output["refund"]["id"], 2)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        r1 = conn.execute(
            "SELECT linked_transaction_id FROM transactions WHERE id=1"
        ).fetchone()
        r2 = conn.execute(
            "SELECT linked_transaction_id FROM transactions WHERE id=2"
        ).fetchone()
        conn.close()
        self.assertEqual(r1["linked_transaction_id"], 2)
        self.assertEqual(r2["linked_transaction_id"], 1)

    def test_link_same_id_fails(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            purchase_id=1, refund_id=1,
        )
        with self.assertRaises(SystemExit):
            with patch("builtins.print"):
                cmd_link(args)

    def test_link_missing_id_fails(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            purchase_id=1, refund_id=999,
        )
        with self.assertRaises(SystemExit):
            with patch("builtins.print"):
                cmd_link(args)

    def test_link_already_linked_blocked(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            purchase_id=1, refund_id=2,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print"):
                cmd_link(args)

        args2 = _Args(
            db_path=self.db_path, json_output=True,
            purchase_id=1, refund_id=3,
        )
        with self.assertRaises(SystemExit):
            with patch("builtins.print"):
                cmd_link(args2)

    def test_link_force_relink(self):
        args = _Args(
            db_path=self.db_path, json_output=True,
            purchase_id=1, refund_id=2,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print"):
                cmd_link(args)

        args2 = _Args(
            db_path=self.db_path, json_output=True,
            purchase_id=1, refund_id=3, force=True,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print") as mock_print:
                cmd_link(args2)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["refund"]["id"], 3)


class TestCmdLinkAmazonRefunds(unittest.TestCase):
    """Tests for the deterministic Amazon refund linker.

    Scope: only 1:1 (one unlinked purchase, one unlinked refund per
    Order ID, amounts cancel within 0.5¢). Multi-line orders and
    non-cancelling amounts are reported but not linked.
    """

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _meta(self, oid, csv_tag):
        return json.dumps({"amazon_order_id": oid, "csv": csv_tag})

    def _run(self, dry_run=False):
        args = _Args(
            db_path=self.db_path, json_output=True, dry_run=dry_run,
        )
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print") as mock_print:
                cmd_link_amazon_refunds(args)
        return json.loads(mock_print.call_args[0][0])

    def test_clean_1to1_pair_links_bidirectionally(self):
        _seed_transactions(self.db_path, [
            ("2024-08-03", "Amazon: USB Hub", 42.00, "Shopping",
             "Amazon", "UNVERIFIED", 1,
             self._meta("111-1111111-1111111", "orders")),
            ("2024-08-17", "Amazon REFUND: USB Hub", -42.00,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta("111-1111111-1111111", "refunds")),
        ])
        result = self._run()
        self.assertEqual(result["linked_count"], 1)
        self.assertEqual(result["skipped_count"], 0)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, linked_transaction_id "
            "FROM transactions ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(rows[0]["linked_transaction_id"], 2)
        self.assertEqual(rows[1]["linked_transaction_id"], 1)

    def test_digital_refund_pairs_uniformly_with_digital_purchase(self):
        """Both physical and digital refunds use the same Order ID
        linking; the csv discriminator is what tells them apart but
        the linker treats both as refund-side."""
        _seed_transactions(self.db_path, [
            ("2023-07-09", "Amazon Digital: Sample Chapter", 2.99,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta("D01-1111111-1111111", "digital")),
            ("2023-07-09", "Amazon Digital Refund: Sample Chapter", -2.99,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta("D01-1111111-1111111", "digital_refunds")),
        ])
        result = self._run()
        self.assertEqual(result["linked_count"], 1)

    def test_multi_line_order_is_skipped(self):
        """An Order ID with multiple purchase rows can't be cleanly
        linked to a single refund (the single-FK schema would leave
        the other purchase rows visible while hiding the refund)."""
        oid = "112-2222222-2222222"
        _seed_transactions(self.db_path, [
            ("2024-08-03", "Amazon: Line 1", 20.00, "Shopping",
             "Amazon", "UNVERIFIED", 1, self._meta(oid, "orders")),
            ("2024-08-03", "Amazon: Line 2", 22.00, "Shopping",
             "Amazon", "UNVERIFIED", 1, self._meta(oid, "orders")),
            ("2024-08-17", "Amazon REFUND: Order", -42.00,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta(oid, "refunds")),
        ])
        result = self._run()
        self.assertEqual(result["linked_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn(
            "multi-line", result["skipped"][0]["reason"],
        )

    def test_partial_refund_not_linked(self):
        """Amounts that don't cancel exactly (e.g. partial refund
        of $59.98 against $59.99 purchase) are skipped — per
        AGENTS.md, only *full* refunds are auto-linked."""
        oid = "113-3333333-3333333"
        _seed_transactions(self.db_path, [
            ("2024-12-27", "Amazon: Party Dress", 59.99,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta(oid, "orders")),
            ("2025-01-09", "Amazon REFUND: Party Dress",
             -59.98, "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta(oid, "refunds")),
        ])
        result = self._run()
        self.assertEqual(result["linked_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("cancel", result["skipped"][0]["reason"])

    def test_multi_refund_consumes_purchase_only_once(self):
        """When the same Order ID has 1 purchase but multiple refund
        rows (e.g. duplicate refund events), the linker must claim
        the purchase only once and skip subsequent refunds — otherwise
        the single-FK column ends up asymmetric (purchase points to
        last refund; first refund still points back at purchase)."""
        oid = "114-4444444-4444444"
        _seed_transactions(self.db_path, [
            ("2024-08-03", "Amazon: Railing Post", 44.10, "Shopping",
             "Amazon", "UNVERIFIED", 1, self._meta(oid, "orders")),
            ("2024-08-17", "Amazon REFUND: Railing Post", -44.10,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta(oid, "refunds")),
            ("2024-08-17", "Amazon REFUND: Railing Post (dup)",
             -44.10, "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta(oid, "refunds")),
        ])
        result = self._run()
        self.assertEqual(result["linked_count"], 1)
        self.assertEqual(result["skipped_count"], 1)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, linked_transaction_id "
            "FROM transactions WHERE id IN (1, 2, 3) ORDER BY id"
        ).fetchall()
        conn.close()
        # Purchase ↔ first refund linked symmetrically;
        # second refund left unlinked.
        self.assertEqual(rows[0]["linked_transaction_id"], 2)
        self.assertEqual(rows[1]["linked_transaction_id"], 1)
        self.assertIsNone(rows[2]["linked_transaction_id"])

    def test_dry_run_writes_nothing(self):
        _seed_transactions(self.db_path, [
            ("2024-08-03", "Amazon: USB Hub", 42.00, "Shopping",
             "Amazon", "UNVERIFIED", 1,
             self._meta("111-9999999-9999999", "orders")),
            ("2024-08-17", "Amazon REFUND: USB Hub", -42.00,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta("111-9999999-9999999", "refunds")),
        ])
        result = self._run(dry_run=True)
        self.assertEqual(result["linked_count"], 1)
        self.assertTrue(result["dry_run"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT linked_transaction_id FROM transactions"
        ).fetchall()
        conn.close()
        self.assertTrue(all(
            r["linked_transaction_id"] is None for r in rows
        ))

    def test_already_linked_rows_are_not_revisited(self):
        """Refund rows whose linked_transaction_id is already set
        (from a prior manual `housebook-audit link`) must be skipped
        so the linker is idempotent across runs."""
        oid = "115-5555555-5555555"
        _seed_transactions(self.db_path, [
            ("2024-08-03", "Amazon: USB Hub", 42.00, "Shopping",
             "Amazon", "UNVERIFIED", 1, self._meta(oid, "orders")),
            ("2024-08-17", "Amazon REFUND: USB Hub", -42.00,
             "Shopping", "Amazon", "UNVERIFIED", 1,
             self._meta(oid, "refunds")),
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE transactions SET linked_transaction_id = 2 WHERE id = 1"
        )
        conn.execute(
            "UPDATE transactions SET linked_transaction_id = 1 WHERE id = 2"
        )
        conn.commit()
        conn.close()

        result = self._run()
        self.assertEqual(result["linked_count"], 0)
        self.assertEqual(result["skipped_count"], 0)


class TestCmdUnlink(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_transactions(self.db_path, [
            ("2026-03-08", "Hotel Booking", 500.00, "Lodging",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-04-02", "Hotel Refund", -500.00, "Transfers & Refunds",
             "Amex", "AGENT_VERIFIED", 0),
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE transactions SET linked_transaction_id=2 WHERE id=1")
        conn.execute(
            "UPDATE transactions SET linked_transaction_id=1 WHERE id=2")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_unlink_clears_both(self):
        args = _Args(db_path=self.db_path, json_output=True, id=1)
        with patch("housebook.audit.backup_database"):
            with patch("builtins.print") as mock_print:
                cmd_unlink(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertIn(1, output["unlinked"])
        self.assertIn(2, output["unlinked"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        for tid in (1, 2):
            row = conn.execute(
                "SELECT linked_transaction_id FROM transactions "
                "WHERE id=?", (tid,)
            ).fetchone()
            self.assertIsNone(row["linked_transaction_id"])
        conn.close()

    def test_unlink_not_linked_fails(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE transactions SET linked_transaction_id=NULL")
        conn.commit()
        conn.close()

        args = _Args(db_path=self.db_path, json_output=True, id=1)
        with self.assertRaises(SystemExit):
            with patch("builtins.print"):
                cmd_unlink(args)

    def test_unlink_missing_id_fails(self):
        args = _Args(db_path=self.db_path, json_output=True, id=999)
        with self.assertRaises(SystemExit):
            with patch("builtins.print"):
                cmd_unlink(args)


class TestCmdLinked(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        _seed_transactions(self.db_path, [
            ("2026-03-08", "Hotel Booking", 500.00, "Lodging",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-04-02", "Hotel Refund", -500.00, "Transfers & Refunds",
             "Amex", "AGENT_VERIFIED", 0),
            ("2026-03-10", "Unlinked Flight", 300.00, "Flights",
             "Amex", "AGENT_VERIFIED", 0),
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE transactions SET linked_transaction_id=2 WHERE id=1")
        conn.execute(
            "UPDATE transactions SET linked_transaction_id=1 WHERE id=2")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_lists_linked_pairs(self):
        args = _Args(db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mock_print:
            cmd_linked(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["purchase_id"], 1)
        self.assertEqual(output[0]["refund_id"], 2)

    def test_empty_when_no_links(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE transactions SET linked_transaction_id=NULL")
        conn.commit()
        conn.close()

        args = _Args(db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mock_print:
            cmd_linked(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(output), 0)


class TestCmdTripsScoping(unittest.TestCase):
    """Self-documenting scope + targeted filters for `trips`."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        # 19 trips, one per month spanning 2023-08 .. 2025-02.
        trips = []
        for i in range(19):
            year = 2023 + (7 + i) // 12
            month = (7 + i) % 12 + 1
            start = f"{year}-{month:02d}-05"
            end = f"{year}-{month:02d}-10"
            trips.append((f"Trip {i}", start, end, "personal", "Somewhere"))
        _seed_trips(self.db_path, trips)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_default_limit_shows_truncation_footer(self):
        args = _Args(db_path=self.db_path)  # default limit 10
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        out = _printed(mock_print)
        self.assertIn("Showing 10 of 19 trips", out)
        self.assertIn("--limit 0", out)  # widen hint present

    def test_limit_zero_shows_all_no_truncation_hint(self):
        args = _Args(db_path=self.db_path, json_output=True, limit=0)
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(output), 19)

    def test_limit_zero_human_says_showing_all(self):
        args = _Args(db_path=self.db_path, limit=0)
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        out = _printed(mock_print)
        self.assertIn("Showing all 19 trips", out)
        self.assertNotIn("of 19 trips (newest first)", out)

    def test_all_flag_disables_cap(self):
        args = _Args(db_path=self.db_path, json_output=True, limit=10)
        args.all = True
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(len(output), 19)

    def test_year_filter_overlap(self):
        args = _Args(db_path=self.db_path, json_output=True, limit=0)
        args.year = 2024
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        output = json.loads(mock_print.call_args[0][0])
        # Trips 5..16 fall in 2024 (2024-01 .. 2024-12) -> 12 trips.
        self.assertEqual(len(output), 12)
        for r in output:
            self.assertTrue(r["start_date"].startswith("2024"))

    def test_since_until_range(self):
        args = _Args(db_path=self.db_path, json_output=True, limit=0)
        args.since = "2024-06-01"
        args.until = "2024-08-31"
        with patch("builtins.print") as mock_print:
            cmd_trips(args)
        output = json.loads(mock_print.call_args[0][0])
        months = {r["start_date"][:7] for r in output}
        self.assertEqual(months, {"2024-06", "2024-07", "2024-08"})


class TestCmdApplyRulesScoping(unittest.TestCase):
    """Visible date floor + --since/--all widening for `apply-rules`."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        recent = (date.today() - timedelta(days=30)).isoformat()
        _seed_transactions(self.db_path, [
            (recent, "WHOLEFOODS MARKET", 40.0, "Uncategorized",
             "Amex", "UNVERIFIED", 1),
            ("2020-01-01", "WHOLEFOODS MARKET", 50.0, "Uncategorized",
             "Amex", "UNVERIFIED", 1),
        ])
        # Minimal rules.json keyed by category -> keyword list.
        self.rules_fd, self.rules_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(self.rules_fd, "w") as f:
            json.dump({"Groceries": ["WHOLEFOODS"]}, f)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)
        os.unlink(self.rules_path)

    def _run(self, **kwargs):
        args = _Args(db_path=self.db_path, **kwargs)
        with patch("housebook.audit.DB_PATH", self.db_path), \
             patch("housebook.config.settings.RULES_JSON",
                   self.rules_path), \
             patch("housebook.audit.backup_database",
                   return_value=None), \
             patch("builtins.print") as mock_print:
            cmd_apply_rules(args)
        return _printed(mock_print)

    def test_default_window_names_date_and_skipped(self):
        out = self._run()
        self.assertIn("dated >=", out)
        self.assertIn("1 older row(s) NOT considered", out)
        self.assertIn("--all", out)
        # Only the recent row was recategorized.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, category FROM transactions ORDER BY date"
        ).fetchall()
        conn.close()
        self.assertEqual(rows[0]["category"], "Uncategorized")  # 2020 row
        self.assertEqual(rows[1]["category"], "Groceries")      # recent row

    def test_all_examines_every_row(self):
        out = self._run(all=True)
        self.assertIn("all dates", out)
        self.assertNotIn("NOT considered", out)
        conn = sqlite3.connect(self.db_path)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE category='Groceries'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 2)


class TestCmdDetectTripsHeader(unittest.TestCase):
    """detect-trips echoes its effective look-back window."""

    def test_header_reflects_months(self):
        args = _Args(months=24, min_transactions=3, gap_days=3)
        with patch(
            "housebook.core.trip_detector.detect_trips",
            return_value={"candidates": [], "advance_payments": []},
        ), patch("builtins.print") as mock_print:
            cmd_detect_trips(args)
        out = _printed(mock_print)
        self.assertIn("last 24 months", out)
        expected_since = (date.today() - timedelta(days=24 * 30)).isoformat()
        self.assertIn(expected_since, out)


class TestProjects(unittest.TestCase):
    """Lifecycle: create-project → match-project → verify → summary → close."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _create(self, **over):
        defaults = dict(
            name="Master Bath Reno", description="reno", type="renovation",
            location="Master Bathroom", start="2025-09-01", end=None,
            keywords="HOME DEPOT,TILE", categories="Home Improvement",
            budget=5000.0, db_path=self.db_path,
        )
        defaults.update(over)
        with patch("builtins.print"):
            cmd_create_project(_Args(**defaults))
        conn = sqlite3.connect(self.db_path)
        pid = conn.execute("SELECT MAX(id) FROM projects").fetchone()[0]
        conn.close()
        return pid

    def test_create_project_stores_open_with_json_criteria(self):
        pid = self._create()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT status, match_keywords, match_categories, budget, "
            "created_by FROM projects WHERE id = ?", (pid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "open")
        self.assertEqual(json.loads(row[1]), ["HOME DEPOT", "TILE"])
        self.assertEqual(json.loads(row[2]), ["Home Improvement"])
        self.assertEqual(row[3], 5000.0)
        self.assertEqual(row[4], "agent")

    def test_match_high_signal_gate_excludes_unqualified(self):
        pid = self._create()
        _seed_transactions(self.db_path, [
            # qualifies: keyword + category
            ("2025-09-10", "HOME DEPOT BOSTON", 450.0, "Home Improvement",
             "BoA", "UNVERIFIED", 1),
            # qualifies: keyword only (TILE)
            ("2025-09-15", "MAPLE TILE CO", 1200.0, "Shopping & Retail",
             "BoA", "UNVERIFIED", 1),
            # does NOT qualify: no keyword, non-matching category
            ("2025-09-20", "STARBUCKS COFFEE", 6.5, "Dining & Takeout",
             "BoA", "UNVERIFIED", 1),
        ])
        args = _Args(project_id=pid, min_score=2, db_path=self.db_path,
                     json_output=True)
        with patch("builtins.print") as mp:
            cmd_match_project(args)
        out = json.loads(_printed(mp))
        cands = out["projects"][0]["candidates"]
        descs = {c["description"] for c in cands}
        self.assertIn("HOME DEPOT BOSTON", descs)
        self.assertIn("MAPLE TILE CO", descs)
        self.assertNotIn("STARBUCKS COFFEE", descs)

    def test_match_excludes_assigned_linked_and_reconciled(self):
        pid = self._create()
        _seed_transactions(self.db_path, [
            ("2025-09-10", "HOME DEPOT A", 450.0, "Home Improvement",
             "BoA", "UNVERIFIED", 1),       # id 1 — candidate
            ("2025-09-11", "HOME DEPOT B", 200.0, "Home Improvement",
             "BoA", "RECONCILED", 0),        # id 2 — reconciled, excluded
            ("2025-09-12", "HOME DEPOT C", 300.0, "Home Improvement",
             "BoA", "UNVERIFIED", 1),        # id 3 — linked, excluded
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE transactions SET linked_transaction_id = 1 "
                     "WHERE id = 3")
        conn.commit()
        conn.close()
        args = _Args(project_id=pid, min_score=2, db_path=self.db_path,
                     json_output=True)
        with patch("builtins.print") as mp:
            cmd_match_project(args)
        cands = json.loads(_printed(mp))["projects"][0]["candidates"]
        ids = {c["id"] for c in cands}
        self.assertEqual(ids, {1})

    def test_match_sweep_only_open_projects(self):
        open_pid = self._create(name="Open Reno")
        closed_pid = self._create(name="Closed Reno")
        with patch("builtins.print"):
            cmd_close_project(_Args(project_id=closed_pid, freeze_end=False,
                                    db_path=self.db_path))
        # No project_id arg ⇒ sweep all open.
        args = _Args(project_id=None, min_score=2, db_path=self.db_path,
                     json_output=True)
        with patch("builtins.print") as mp:
            cmd_match_project(args)
        swept = {p["project"]["id"]
                 for p in json.loads(_printed(mp))["projects"]}
        self.assertEqual(swept, {open_pid})

    def test_verify_assigns_project_and_clears_review(self):
        pid = self._create()
        _seed_transactions(self.db_path, [
            ("2025-09-10", "HOME DEPOT", 450.0, "Home Improvement",
             "BoA", "UNVERIFIED", 1),
        ])
        with patch("builtins.print"):
            cmd_verify(_Args(ids="1", project=pid, category="Home Improvement",
                             db_path=self.db_path))
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT project_id, status, needs_review FROM transactions "
            "WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(row[0], pid)
        self.assertEqual(row[1], "AGENT_VERIFIED")
        self.assertEqual(row[2], 0)

    def test_project_summary_nets_signed_amounts(self):
        pid = self._create()
        _seed_transactions(self.db_path, [
            ("2025-09-10", "HOME DEPOT", 450.0, "Home Improvement",
             "BoA", "AGENT_VERIFIED", 0),
            ("2025-09-12", "HOME DEPOT REFUND", -50.0, "Home Improvement",
             "BoA", "AGENT_VERIFIED", 0),
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE transactions SET project_id = ?", (pid,))
        conn.commit()
        conn.close()
        args = _Args(project_id=pid, db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mp:
            cmd_project_summary(args)
        out = json.loads(_printed(mp))
        self.assertAlmostEqual(out["net_spend"], 400.0)
        self.assertAlmostEqual(out["remaining"], 4600.0)

    def test_close_project_freeze_end_sets_last_tx_date(self):
        pid = self._create()
        _seed_transactions(self.db_path, [
            ("2025-09-10", "HOME DEPOT", 450.0, "Home Improvement",
             "BoA", "AGENT_VERIFIED", 0),
            ("2025-10-05", "HOME DEPOT", 200.0, "Home Improvement",
             "BoA", "AGENT_VERIFIED", 0),
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE transactions SET project_id = ?", (pid,))
        conn.commit()
        conn.close()
        with patch("builtins.print"):
            cmd_close_project(_Args(project_id=pid, freeze_end=True,
                                    db_path=self.db_path))
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT status, end_date FROM projects WHERE id = ?", (pid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "closed")
        self.assertEqual(row[1], "2025-10-05")

    def test_projects_list_json_reports_net_and_budget(self):
        pid = self._create(budget=1000.0)
        _seed_transactions(self.db_path, [
            ("2025-09-10", "HOME DEPOT", 250.0, "Home Improvement",
             "BoA", "AGENT_VERIFIED", 0),
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE transactions SET project_id = ?", (pid,))
        conn.commit()
        conn.close()
        args = _Args(status="open", db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mp:
            cmd_projects(args)
        rows = json.loads(_printed(mp))
        self.assertEqual(rows[0]["tx_count"], 1)
        self.assertAlmostEqual(rows[0]["net_spend"], 250.0)
        self.assertEqual(rows[0]["budget"], 1000.0)


class TestProjectManualExpenses(unittest.TestCase):
    """Project-linked manual (off-ledger) expenses roll into totals."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        with patch("builtins.print"):
            cmd_create_project(_Args(
                name="Reno", description="d", type="renovation",
                location="L", start="2025-01-01", end=None,
                keywords="", categories="", budget=20000.0,
                db_path=self.db_path))

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _add_manual(self, **over):
        defaults = dict(
            description="Workmanship", amount=12000.0,
            category="Home & Garden", date="2025-02-01", end=None,
            frequency="one-time", project=1, db_path=self.db_path)
        defaults.update(over)
        with patch("builtins.print"):
            cmd_add_manual(_Args(**defaults))

    def test_add_manual_creates_row(self):
        self._add_manual()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT description, amount, category, frequency, project_id "
            "FROM manual_expenses").fetchone()
        conn.close()
        self.assertEqual(row, ("Workmanship", 12000.0, "Home & Garden",
                               "one-time", 1))

    def test_add_manual_rejects_missing_project(self):
        with patch("builtins.print"), self.assertRaises(SystemExit):
            self._add_manual(project=999)

    def test_summary_includes_linked_manual(self):
        _seed_transactions(self.db_path, [
            ("2025-02-05", "HOME DEPOT", 500.0, "Home & Garden",
             "BoA", "AGENT_VERIFIED", 0),
        ])
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE transactions SET project_id = 1")
        conn.commit()
        conn.close()
        self._add_manual(amount=12000.0)
        args = _Args(project_id=1, db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mp:
            cmd_project_summary(args)
        out = json.loads(_printed(mp))
        self.assertAlmostEqual(out["net_spend"], 12500.0)
        self.assertEqual(out["transaction_count"], 1)
        self.assertEqual(out["manual_count"], 1)
        self.assertAlmostEqual(out["by_category"]["Home & Garden"], 12500.0)

    def test_unlinked_manual_excluded_from_project(self):
        self._add_manual(amount=12000.0, project=1)
        # An unrelated manual expense not linked to the project.
        self._add_manual(description="Rent", amount=3000.0,
                         category="Bills", project=None)
        args = _Args(project_id=1, db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mp:
            cmd_project_summary(args)
        out = json.loads(_printed(mp))
        self.assertAlmostEqual(out["net_spend"], 12000.0)

    def test_projects_list_net_includes_only_linked_manual(self):
        self._add_manual(amount=12000.0, project=1)
        self._add_manual(description="Rent", amount=3000.0,
                         category="Bills", project=None)
        args = _Args(status="open", db_path=self.db_path, json_output=True)
        with patch("builtins.print") as mp:
            cmd_projects(args)
        rows = json.loads(_printed(mp))
        self.assertAlmostEqual(rows[0]["net_spend"], 12000.0)


class TestAssignAndEditProject(unittest.TestCase):
    """assign (pure tag) + edit-project + verify status-preservation."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        _create_test_db(self.db_path)
        with patch("builtins.print"):
            cmd_create_project(_Args(
                name="Reno", description="d", type="renovation",
                location="L", start="2025-03-01", end="2025-06-30",
                keywords="", categories="", budget=1000.0,
                db_path=self.db_path))
        _seed_transactions(self.db_path, [
            ("2025-04-01", "TILE SHOP", 443.84, "Home & Garden",
             "Lowes", "USER_VERIFIED", 0),
            ("2025-04-02", "HOME DEPOT", 58.0, "Home & Garden",
             "BoA", "AGENT_VERIFIED", 0),
        ])

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _status(self, tid):
        conn = sqlite3.connect(self.db_path)
        s = conn.execute("SELECT status, project_id FROM transactions "
                         "WHERE id = ?", (tid,)).fetchone()
        conn.close()
        return s

    def test_assign_tags_without_changing_status(self):
        with patch("builtins.print"):
            cmd_assign(_Args(ids="1,2", project=1, db_path=self.db_path))
        # USER_VERIFIED preserved, project set on both.
        self.assertEqual(self._status(1), ("USER_VERIFIED", 1))
        self.assertEqual(self._status(2), ("AGENT_VERIFIED", 1))

    def test_assign_requires_a_target(self):
        with patch("builtins.print"), self.assertRaises(SystemExit):
            cmd_assign(_Args(ids="1", db_path=self.db_path))

    def test_assign_rejects_unknown_project(self):
        with patch("builtins.print"), self.assertRaises(SystemExit):
            cmd_assign(_Args(ids="1", project=999, db_path=self.db_path))

    def test_verify_does_not_downgrade_user_verified(self):
        with patch("builtins.print"):
            cmd_verify(_Args(ids="1", category="Home & Garden",
                             project=1, force=True, db_path=self.db_path))
        # Status must remain USER_VERIFIED, not drop to AGENT_VERIFIED.
        self.assertEqual(self._status(1)[0], "USER_VERIFIED")

    def test_verify_still_promotes_unverified(self):
        _seed_transactions(self.db_path, [
            ("2025-04-03", "X", 5.0, "Misc", "BoA", "UNVERIFIED", 1),
        ])
        with patch("builtins.print"):
            cmd_verify(_Args(ids="3", category="Misc", db_path=self.db_path))
        self.assertEqual(self._status(3)[0], "AGENT_VERIFIED")

    def test_edit_project_updates_only_passed_fields(self):
        with patch("builtins.print"):
            cmd_edit_project(_Args(project_id=1, start="2022-12-01",
                                   budget=24506.65, db_path=self.db_path))
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT start_date, end_date, budget, name FROM projects "
            "WHERE id = 1").fetchone()
        conn.close()
        # start_date and budget changed; end_date and name untouched.
        self.assertEqual(row[0], "2022-12-01")
        self.assertEqual(row[1], "2025-06-30")
        self.assertAlmostEqual(row[2], 24506.65)
        self.assertEqual(row[3], "Reno")

    def test_edit_project_requires_a_field(self):
        with patch("builtins.print"), self.assertRaises(SystemExit):
            cmd_edit_project(_Args(project_id=1, db_path=self.db_path))

    def test_edit_project_unknown_id(self):
        with patch("builtins.print"), self.assertRaises(SystemExit):
            cmd_edit_project(_Args(project_id=99, name="X",
                                   db_path=self.db_path))


class TestBackupIsolation(unittest.TestCase):
    """The audit `_backup` helper must never write a throwaway DB's
    backup into the production BACKUP_DIR — that pollution gets mirrored
    to the remote on the next `housebook-sync push`.
    """

    def test_default_db_uses_prod_backup_dir(self):
        with patch("housebook.audit.backup_database") as mock_bk, \
                patch("housebook.audit.DB_PATH", "/live/finance.db"), \
                patch("housebook.audit.BACKUP_DIR", "/live/backups"):
            _backup(None)
            mock_bk.assert_called_once_with("/live/finance.db", "/live/backups")

    def test_overridden_db_path_isolates_backup_dir(self):
        with patch("housebook.audit.backup_database") as mock_bk, \
                patch("housebook.audit.DB_PATH", "/live/finance.db"), \
                patch("housebook.audit.BACKUP_DIR", "/live/backups"):
            _backup("/tmp/tmp2r4zmf3k.db")
            # backup_dir is None → backup_database defaults beside the temp
            # DB, NOT into the live workspace backups.
            mock_bk.assert_called_once_with("/tmp/tmp2r4zmf3k.db", None)

    def test_explicit_default_path_still_uses_prod_dir(self):
        # Passing the production path explicitly is treated as default.
        with patch("housebook.audit.backup_database") as mock_bk, \
                patch("housebook.audit.DB_PATH", "/live/finance.db"), \
                patch("housebook.audit.BACKUP_DIR", "/live/backups"):
            _backup("/live/finance.db")
            mock_bk.assert_called_once_with("/live/finance.db", "/live/backups")


if __name__ == "__main__":
    unittest.main()
