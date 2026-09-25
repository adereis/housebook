"""Tests for the tax estimate engine.

The engine hardcodes 2025 MFJ/MA parameters. These tests pin both the
capital-gains math and the guard that keeps those parameters from
being silently applied to another year, status, or state.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from unittest import mock

from housebook.tax import estimate as est
from housebook.tax.parameters import get_parameter_set


def _seed(db_path, docs):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tax_documents ("
        "id INTEGER PRIMARY KEY, tax_year INTEGER, document_type TEXT, "
        "issuer TEXT, category TEXT, amount REAL, currency TEXT, "
        "original_file TEXT, status TEXT, needs_review INTEGER, "
        "raw_data TEXT)"
    )
    for year, dtype, amount, raw in docs:
        conn.execute(
            "INSERT INTO tax_documents "
            "(tax_year, document_type, issuer, amount, raw_data) "
            "VALUES (?, ?, ?, ?, ?)",
            (year, dtype, "Acme Corp", amount, json.dumps(raw)),
        )
    conn.commit()
    conn.close()


class TestTaxEstimate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        # No profile file → the supported MFJ/MA defaults apply.
        self.no_profile = mock.patch.object(
            est, "USER_PROFILE_JSON",
            os.path.join(self.tmp.name, "absent.json"),
        )
        self.no_profile.start()

    def tearDown(self):
        self.no_profile.stop()
        self.tmp.cleanup()

    def test_deduction_shelters_capital_gains(self):
        """Modest income + LT gains under the 0% threshold owes $0.

        The deduction reduces TOTAL taxable income, and gains below the
        0% LTCG threshold are untaxed. Subtracting the deduction from
        ordinary income only, then taxing gains at a flat 15%, billed
        $15,000 on a return that actually owes nothing.
        """
        _seed(self.db, [
            (2025, "W2", 10000.0, {"federal_tax_withheld": 0}),
            (2025, "1099", 0.0, {"long_term_gain_loss": 100000}),
        ])
        r = est.compute_tax_estimate(self.db, 2025)
        self.assertEqual(r["taxable_income"]["ordinary"], 0.0)
        self.assertEqual(r["taxable_income"]["preferential"], 80000.0)
        self.assertEqual(r["federal"]["preferential_tax"], 0.0)
        self.assertEqual(r["federal"]["total_tax"], 0.0)
        self.assertEqual(
            r["parameter_set"]["id"], "us-2025-mfj-ma-v1",
        )
        self.assertFalse(r["parameter_set"]["filing_comparable"])
        self.assertTrue(r["parameter_set"]["known_gaps"])

    def test_gains_stack_above_ordinary_income(self):
        """Preferential income is taxed by where it stacks, not its size."""
        _seed(self.db, [
            (2025, "W2", 200000.0, {"federal_tax_withheld": 0}),
            (2025, "1099", 0.0, {"long_term_gain_loss": 500000}),
        ])
        r = est.compute_tax_estimate(self.db, 2025)
        bands = {b["rate"]: b["taxed"] for b in r["preferential_brackets"]}
        # Taxable total 670k; ordinary 170k; pref spans 170k → 670k, so
        # it crosses the 15% ceiling at 600,050 into the 20% band.
        self.assertAlmostEqual(bands[0.15], 430050.0, places=2)
        self.assertAlmostEqual(bands[0.20], 69950.0, places=2)
        self.assertNotIn(0.0, bands)

    def test_unsupported_year_is_refused(self):
        _seed(self.db, [
            (2019, "W2", 10000.0, {"federal_tax_withheld": 0}),
        ])
        r = est.compute_tax_estimate(self.db, 2019)
        self.assertIn("error", r)
        self.assertIn("2019", r["error"])
        self.assertNotIn("federal", r)

    def test_unsupported_filing_status_is_refused(self):
        profile = os.path.join(self.tmp.name, "user_profile.json")
        with open(profile, "w") as f:
            json.dump({"tax_filing": {"status": "single"}}, f)
        _seed(self.db, [
            (2025, "W2", 10000.0, {"federal_tax_withheld": 0}),
        ])
        with mock.patch.object(est, "USER_PROFILE_JSON", profile):
            r = est.compute_tax_estimate(self.db, 2025)
        self.assertIn("error", r)
        self.assertIn("single", r["error"])

    def test_unsupported_state_is_refused(self):
        """A CA filer must not receive MA's flat rate labeled 'CA'."""
        profile = os.path.join(self.tmp.name, "user_profile.json")
        with open(profile, "w") as f:
            json.dump({"tax_filing": {"state": "CA"}}, f)
        _seed(self.db, [
            (2025, "W2", 10000.0, {"federal_tax_withheld": 0}),
        ])
        with mock.patch.object(est, "USER_PROFILE_JSON", profile):
            r = est.compute_tax_estimate(self.db, 2025)
        self.assertIn("error", r)
        self.assertIn("CA", r["error"])

    def test_parameter_registry_is_exact_and_immutable(self):
        parameters = get_parameter_set(2025, "MFJ", "MA")
        self.assertIsNotNone(parameters)
        self.assertEqual(
            parameters.federal.standard_deduction,
            Decimal("30000"),
        )
        self.assertIsNone(get_parameter_set(2024, "MFJ", "MA"))
        with self.assertRaises(FrozenInstanceError):
            parameters.tax_year = 2024


if __name__ == "__main__":
    unittest.main()
