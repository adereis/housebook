import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from housebook.core.project_matcher import match_project


def _make_db():
    fd, path = tempfile.mkstemp()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE, description TEXT, amount REAL, category TEXT,
            source TEXT, status TEXT DEFAULT 'UNVERIFIED',
            trip_id INTEGER, project_id INTEGER,
            needs_review INTEGER DEFAULT 1,
            linked_transaction_id INTEGER
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, start_date DATE, end_date DATE,
            status TEXT DEFAULT 'open',
            match_keywords TEXT, match_categories TEXT
        );
    """)
    conn.commit()
    conn.close()
    return fd, path


def _add_project(path, start="2025-09-01", end=None,
                 keywords=("HOME DEPOT", "TILE"),
                 categories=("Home Improvement",)):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO projects (name, start_date, end_date, status, "
        "match_keywords, match_categories) VALUES (?,?,?,'open',?,?)",
        ("Reno", start, end, json.dumps(list(keywords)),
         json.dumps(list(categories))),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def _add_tx(path, date_, desc, amount, category, **over):
    conn = sqlite3.connect(path)
    cols = dict(date=date_, description=desc, amount=amount,
                category=category, source="BoA")
    cols.update(over)
    keys = ",".join(cols)
    qs = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO transactions ({keys}) VALUES ({qs})",
        tuple(cols.values()))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


class TestProjectMatcher(unittest.TestCase):

    def setUp(self):
        self.fd, self.path = _make_db()

    def tearDown(self):
        os.close(self.fd)
        os.unlink(self.path)

    def test_missing_project_raises(self):
        with self.assertRaises(ValueError):
            match_project(self.path, 999)

    def test_keyword_scores_higher_than_category(self):
        pid = _add_project(self.path)
        _add_tx(self.path, "2025-09-10", "HOME DEPOT BOSTON", 50.0,
                "Home Improvement")   # keyword(3)+category(2) = 5
        _add_tx(self.path, "2025-09-11", "GENERIC STORE", 50.0,
                "Home Improvement")   # category(2) only
        res = match_project(self.path, pid)
        cands = res["candidates"]
        self.assertEqual(cands[0]["description"], "HOME DEPOT BOSTON")
        self.assertEqual(cands[0]["score"], 5)
        self.assertEqual(cands[1]["score"], 2)

    def test_high_signal_gate(self):
        pid = _add_project(self.path)
        # No keyword, non-matching category, large amount → still excluded.
        _add_tx(self.path, "2025-09-10", "RANDOM VENDOR", 5000.0, "Dining")
        self.assertEqual(match_project(self.path, pid)["candidates"], [])

    def test_large_amount_boost(self):
        pid = _add_project(self.path)
        _add_tx(self.path, "2025-09-10", "TILE SHOP", 250.0, "Dining")
        c = match_project(self.path, pid)["candidates"][0]
        self.assertIn("large_amount", c["signals"])
        self.assertEqual(c["score"], 4)  # keyword(3) + large(1)

    def test_known_vendor_boost_after_assignment(self):
        pid = _add_project(self.path)
        # Already-assigned charge teaches the matcher this vendor.
        _add_tx(self.path, "2025-09-05", "VAULT PLUMBING CO", 900.0,
                "Home Improvement", project_id=pid)
        # New unassigned charge from same vendor; not in keywords.
        _add_tx(self.path, "2025-09-20", "VAULT PLUMBING CO", 120.0,
                "Home Improvement")
        c = match_project(self.path, pid)["candidates"][0]
        self.assertIn("known_vendor", c["signals"])

    def test_window_excludes_outside_dates(self):
        pid = _add_project(self.path, start="2025-09-01", end="2025-09-30")
        _add_tx(self.path, "2025-08-30", "HOME DEPOT", 50.0, "Home Improvement")
        _add_tx(self.path, "2025-10-02", "HOME DEPOT", 50.0, "Home Improvement")
        _add_tx(self.path, "2025-09-15", "HOME DEPOT", 50.0, "Home Improvement")
        cands = match_project(self.path, pid)["candidates"]
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["date"], "2025-09-15")

    def test_open_project_scans_through_today(self):
        pid = _add_project(self.path, start="2025-09-01", end=None)
        today = date.today().isoformat()
        recent = (date.today() - timedelta(days=1)).isoformat()
        _add_tx(self.path, recent, "HOME DEPOT", 50.0, "Home Improvement")
        cands = match_project(self.path, pid)["candidates"]
        self.assertEqual(len(cands), 1)
        self.assertLessEqual(cands[0]["date"], today)

    def test_excludes_assigned_trip_and_already_in_project(self):
        pid = _add_project(self.path)
        _add_tx(self.path, "2025-09-10", "HOME DEPOT", 50.0,
                "Home Improvement", project_id=pid)        # already assigned
        _add_tx(self.path, "2025-09-11", "HOME DEPOT", 50.0,
                "Home Improvement", trip_id=7)              # owned by a trip
        self.assertEqual(match_project(self.path, pid)["candidates"], [])

    def test_min_score_threshold(self):
        pid = _add_project(self.path)
        # category-only, small amount → score 2; raise threshold to 3.
        _add_tx(self.path, "2025-09-10", "GENERIC", 10.0, "Home Improvement")
        self.assertEqual(
            match_project(self.path, pid, min_score=3)["candidates"], [])
        self.assertEqual(
            len(match_project(self.path, pid, min_score=2)["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
