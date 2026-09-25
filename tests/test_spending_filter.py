"""Tests for the canonical spending-view predicate.

The exclusion contract is stated in AGENTS.md and was previously
hand-written in seven places; these tests pin the behavior of the one
definition every view now shares.
"""

import sqlite3
import unittest

from housebook.core.spending import spend_filter

ROWS = [
    # id, description, amount, category, status, linked_transaction_id
    (1, "normal charge", -50.0, "Groceries", "UNVERIFIED", None),
    (2, "null category", -60.0, None, "UNVERIFIED", None),
    (3, "null status", -70.0, "Groceries", None, None),
    (4, "card payment", 500.0, "CC Payment", "UNVERIFIED", None),
    (5, "amazon bank dupe", -80.0, "Shopping", "RECONCILED", None),
    (6, "linked purchase", -90.0, "Shopping", "UNVERIFIED", 7),
    (7, "zero amount", 0.0, "Shopping", "UNVERIFIED", None),
]


class TestSpendFilter(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE transactions ("
            "id INTEGER PRIMARY KEY, description TEXT, amount REAL, "
            "category TEXT, status TEXT, linked_transaction_id INTEGER)"
        )
        self.conn.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?)", ROWS
        )

    def tearDown(self):
        self.conn.close()

    def _visible(self, alias=""):
        prefix = f"{alias}." if alias else ""
        table = f"transactions {alias}" if alias else "transactions"
        rows = self.conn.execute(
            f"SELECT {prefix}description FROM {table} "
            f"WHERE {spend_filter(alias)} ORDER BY {prefix}id"
        ).fetchall()
        return [r[0] for r in rows]

    def test_excludes_payments_dupes_links_and_zeros(self):
        visible = self._visible()
        self.assertNotIn("card payment", visible)
        self.assertNotIn("amazon bank dupe", visible)
        self.assertNotIn("linked purchase", visible)
        self.assertNotIn("zero amount", visible)

    def test_null_category_and_status_stay_visible(self):
        """`category != 'CC Payment'` is NULL for a NULL category.

        Without the NULL-safe form those rows silently disappeared from
        every spending view and total.
        """
        visible = self._visible()
        self.assertIn("null category", visible)
        self.assertIn("null status", visible)

    def test_alias_form_matches_unaliased(self):
        self.assertEqual(self._visible(), self._visible("t"))


if __name__ == "__main__":
    unittest.main()
