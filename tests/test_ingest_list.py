import json
import os
import sqlite3
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from housebook.ingest import _cmd_list, _detect_gaps, _pick_latest


def _create_test_db(db_path):
    """Create minimal transactions table."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
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
            needs_review BOOLEAN DEFAULT 1,
            profile TEXT,
            metadata TEXT
        )
    """)
    conn.commit()
    conn.close()


def _seed(db_path, transactions):
    """Insert transactions.

    transactions: list of (date, desc, amount, category, source, file)
    """
    conn = sqlite3.connect(db_path)
    for t in transactions:
        conn.execute(
            "INSERT INTO transactions "
            "(date, description, amount, category, source, original_file) "
            "VALUES (?, ?, ?, ?, ?, ?)", t,
        )
    conn.commit()
    conn.close()


def _make_args(db_path, source=None, latest=False, json_output=False):
    """Build a namespace matching the argparse output."""
    import argparse
    return argparse.Namespace(
        db_path=db_path,
        source=source,
        latest=latest,
        json_output=json_output,
    )


class TestDetectGaps(unittest.TestCase):
    """Unit tests for the gap detection helper."""

    def test_no_gaps(self):
        files = [
            {"first_date": "2026-01-01", "last_date": "2026-01-31"},
            {"first_date": "2026-02-01", "last_date": "2026-02-28"},
        ]
        self.assertEqual(_detect_gaps(files), [])

    def test_gap_detected(self):
        files = [
            {"first_date": "2026-01-01", "last_date": "2026-01-31"},
            {"first_date": "2026-03-01", "last_date": "2026-03-31"},
        ]
        gaps = _detect_gaps(files)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["after"], "2026-01-31")
        self.assertEqual(gaps[0]["before"], "2026-03-01")

    def test_overlapping_no_gap(self):
        files = [
            {"first_date": "2026-01-28", "last_date": "2026-02-27"},
            {"first_date": "2026-02-28", "last_date": "2026-03-27"},
        ]
        self.assertEqual(_detect_gaps(files), [])

    def test_small_gap_under_threshold(self):
        files = [
            {"first_date": "2026-01-01", "last_date": "2026-01-28"},
            {"first_date": "2026-02-02", "last_date": "2026-02-28"},
        ]
        self.assertEqual(_detect_gaps(files), [])

    def test_single_file(self):
        files = [
            {"first_date": "2026-01-01", "last_date": "2026-01-31"},
        ]
        self.assertEqual(_detect_gaps(files), [])

    def test_empty(self):
        self.assertEqual(_detect_gaps([]), [])

    def test_out_of_order_input(self):
        files = [
            {"first_date": "2026-03-01", "last_date": "2026-03-31"},
            {"first_date": "2026-01-01", "last_date": "2026-01-31"},
        ]
        gaps = _detect_gaps(files)
        self.assertEqual(len(gaps), 1)


class TestCmdList(unittest.TestCase):
    """Integration tests for the list subcommand."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _create_test_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_empty_db(self):
        args = _make_args(self.db_path)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        self.assertIn("No ingested files found", out.getvalue())

    def test_table_output(self):
        _seed(self.db_path, [
            ("2026-01-01", "Store A", -50.0, "Shopping", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-01-15", "Store B", -30.0, "Dining", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-02-01", "Store C", -40.0, "Shopping", "Amex",
             "cc/2026/Amex_2026-02.pdf"),
        ])

        args = _make_args(self.db_path)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        output = out.getvalue()
        self.assertIn("Amex", output)
        self.assertIn("2 file(s)", output)
        self.assertIn("2026-01-01", output)
        self.assertIn("2 txns", output)

    def test_source_filter(self):
        _seed(self.db_path, [
            ("2026-01-05", "X", -10, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-01-05", "Y", -20, "Cat", "BoA",
             "cc/2026/BoA_2026-01.pdf"),
        ])

        args = _make_args(self.db_path, source="Amex")
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        output = out.getvalue()
        self.assertIn("Amex", output)
        self.assertNotIn("BoA", output)

    def test_json_output(self):
        _seed(self.db_path, [
            ("2026-01-05", "X", -10, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
        ])

        args = _make_args(self.db_path, json_output=True)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        data = json.loads(out.getvalue())
        self.assertIn("Amex", data)
        self.assertEqual(data["Amex"]["total_files"], 1)
        self.assertEqual(len(data["Amex"]["files"]), 1)

    def test_gap_detection_in_table(self):
        _seed(self.db_path, [
            ("2026-01-01", "X", -10, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-01-31", "Y", -20, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-03-01", "Z", -30, "Cat", "Amex",
             "cc/2026/Amex_2026-03.pdf"),
            ("2026-03-31", "W", -40, "Cat", "Amex",
             "cc/2026/Amex_2026-03.pdf"),
        ])

        args = _make_args(self.db_path)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        self.assertIn("Gap", out.getvalue())

    def test_no_gap_for_amazon(self):
        _seed(self.db_path, [
            ("2025-06-01", "A", -10, "Cat", "Amazon",
             "amazon/john/orders-2025.csv"),
            ("2026-03-01", "B", -20, "Cat", "Amazon",
             "amazon/john/orders-2026.csv"),
        ])

        args = _make_args(self.db_path)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        self.assertNotIn("Gap", out.getvalue())

    def test_gap_detection_in_json(self):
        _seed(self.db_path, [
            ("2026-01-05", "X", -10, "Cat", "BoA",
             "cc/2026/BoA_2026-01.pdf"),
            ("2026-01-31", "Y", -20, "Cat", "BoA",
             "cc/2026/BoA_2026-01.pdf"),
            ("2026-03-05", "Z", -30, "Cat", "BoA",
             "cc/2026/BoA_2026-03.pdf"),
        ])

        args = _make_args(self.db_path, json_output=True)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        data = json.loads(out.getvalue())
        self.assertEqual(len(data["BoA"]["gaps"]), 1)
        self.assertEqual(data["BoA"]["gaps"][0]["after"], "2026-01-31")

    def test_latest_table(self):
        _seed(self.db_path, [
            ("2026-01-05", "X", -10, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-02-05", "Y", -20, "Cat", "Amex",
             "cc/2026/Amex_2026-02.pdf"),
            ("2026-01-10", "Z", -30, "Cat", "BoA",
             "cc/2026/BoA_2026-01.pdf"),
        ])

        args = _make_args(self.db_path, latest=True)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        output = out.getvalue()
        self.assertIn("2026-02-05", output)
        self.assertNotIn("2026-01-05", output)
        self.assertIn("BoA", output)

    def test_latest_json(self):
        _seed(self.db_path, [
            ("2026-01-05", "X", -10, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-02-05", "Y", -20, "Cat", "Amex",
             "cc/2026/Amex_2026-02.pdf"),
        ])

        args = _make_args(self.db_path, latest=True, json_output=True)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        data = json.loads(out.getvalue())
        self.assertEqual(data["Amex"]["total_files"], 1)
        self.assertEqual(
            data["Amex"]["files"][0]["last_date"], "2026-02-05",
        )

    def test_source_filter_no_match(self):
        _seed(self.db_path, [
            ("2026-01-05", "X", -10, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
        ])

        args = _make_args(self.db_path, source="BoA")
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        self.assertIn("No ingested files found for source 'BoA'",
                       out.getvalue())

    def test_multiple_sources(self):
        _seed(self.db_path, [
            ("2026-01-05", "X", -10, "Cat", "Amex",
             "cc/2026/Amex_2026-01.pdf"),
            ("2026-02-10", "Y", -20, "Cat", "BoA",
             "cc/2026/BoA_2026-02.pdf"),
            ("2026-03-15", "Z", -30, "Cat", "Amazon",
             "amazon/john/orders.csv"),
        ])

        args = _make_args(self.db_path)
        with patch("sys.stdout", new_callable=StringIO) as out:
            _cmd_list(args)
        output = out.getvalue()
        self.assertIn("Amex", output)
        self.assertIn("BoA", output)
        self.assertIn("Amazon", output)
        self.assertIn("3 file(s) total", output)


class TestPickLatest(unittest.TestCase):
    """Unit tests for _pick_latest helper."""

    def _row(self, source, last_date):
        return {"source": source, "last_date": last_date,
                "file_path": f"{source}/{last_date}.pdf",
                "first_date": last_date, "tx_count": 1}

    def test_picks_latest_per_source(self):
        rows = [
            self._row("Amex", "2026-01-31"),
            self._row("Amex", "2026-02-28"),
            self._row("BoA", "2026-01-15"),
        ]
        result = _pick_latest(rows)
        sources = {r["source"]: r["last_date"] for r in result}
        self.assertEqual(sources["Amex"], "2026-02-28")
        self.assertEqual(sources["BoA"], "2026-01-15")
        self.assertEqual(len(result), 2)

    def test_single_source(self):
        rows = [self._row("Amex", "2026-03-31")]
        result = _pick_latest(rows)
        self.assertEqual(len(result), 1)

    def test_empty(self):
        self.assertEqual(_pick_latest([]), [])


if __name__ == "__main__":
    unittest.main()
