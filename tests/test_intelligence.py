import unittest
from decimal import Decimal
from unittest.mock import patch

from housebook.core.intelligence import Intelligence, load_rules

_PATCH_TARGET = (
    "housebook.core.intelligence"
    ".Intelligence._load_json"
)


class TestIntelligence(unittest.TestCase):

    def setUp(self):
        # Sample rules, shaped like rules.json: category -> keywords.
        self.rules = {
            "Dining & Takeout": ["Starbucks"],
            "Groceries": ["Whole Foods"],
        }

        # Mock external JSON data
        self.mock_exclusions = {
            "transaction_keywords": ["PAYMENT", "TRANSFER"],
            "pdf_line_metadata": ["Page", "Balance"],
        }
        self.mock_heuristics = {
            "Auto & Fuel": ["Shell", "Mobil"],
            "Bills & Utilities": ["Comcast", "Verizon"],
            "Dining & Takeout": ["Starbucks"],
        }

    def _json_side_effect(self, heuristics=None):
        exclusions = self.mock_exclusions
        heuristics = heuristics or self.mock_heuristics

        def side_effect(path, default):
            if "exclusions.json" in path:
                return exclusions
            if "heuristics.json" in path:
                return heuristics
            return default

        return side_effect

    def test_get_category_exclusion(self):
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()

            intel = Intelligence(self.rules)
            cat, conf = intel.get_category("CREDIT CARD PAYMENT")
            self.assertEqual(cat, "Transfers & Refunds")
            self.assertEqual(conf, "rule")

            cat, conf = intel.get_category("WIRE TRANSFER FEE")
            self.assertEqual(cat, "Transfers & Refunds")
            self.assertEqual(conf, "rule")

    def test_get_category_user_rule(self):
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()

            intel = Intelligence(self.rules)
            cat, conf = intel.get_category("STARBUCKS COFFEE")
            self.assertEqual(cat, "Dining & Takeout")
            self.assertEqual(conf, "rule")

            cat, conf = intel.get_category("WHOLE FOODS MARKET")
            self.assertEqual(cat, "Groceries")
            self.assertEqual(conf, "rule")

    def test_get_category_heuristic(self):
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()

            intel = Intelligence(self.rules)
            cat, conf = intel.get_category("SHELL OIL")
            self.assertEqual(cat, "Auto & Fuel")
            self.assertEqual(conf, "heuristic")

            cat, conf = intel.get_category("VERIZON WIRELESS")
            self.assertEqual(cat, "Bills & Utilities")
            self.assertEqual(conf, "heuristic")

    def test_amazon_constraint(self):
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()

            rules = {"Dining & Takeout": ["Amazon"]}
            intel = Intelligence(rules)
            cat, conf = intel.get_category("Amazon: Starbucks Coffee")
            self.assertEqual(cat, "Shopping & Retail")
            self.assertEqual(conf, "heuristic")

    def test_word_boundary_matching(self):
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()

            rules = {"Alcohol & Specialty": ["Wine"]}
            intel = Intelligence(rules)
            cat, conf = intel.get_category("Mountain Spring Cat Litter")
            self.assertEqual(cat, "Miscellaneous")
            self.assertEqual(conf, "guess")

            cat, conf = intel.get_category("Red Wine")
            self.assertEqual(cat, "Alcohol & Specialty")
            self.assertEqual(conf, "rule")

    def test_amazon_never_dining(self):
        heuristics = {"Dining & Takeout": ["Starbucks"]}
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect(
                heuristics=heuristics,
            )

            intel = Intelligence({})
            cat, conf = intel.get_category("Starbucks Coffee")
            self.assertEqual(cat, "Dining & Takeout")
            self.assertEqual(conf, "heuristic")

            cat, conf = intel.get_category("Amazon: Starbucks Pods")
            self.assertEqual(cat, "Shopping & Retail")
            self.assertEqual(conf, "heuristic")


    def test_negative_amount_categorized_normally(self):
        """Negative amounts go through normal categorization."""
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()
            intel = Intelligence(self.rules)

            cat, conf = intel.get_category("WHOLE FOODS MARKET",
                                           amount=Decimal("-15.00"))
            self.assertEqual(cat, "Groceries")
            self.assertEqual(conf, "rule")

            cat, conf = intel.get_category("SOME OBSCURE VENDOR",
                                           amount=Decimal("-5.00"))
            self.assertEqual(cat, "Miscellaneous")
            self.assertEqual(conf, "guess")

    def test_cc_payment_detected(self):
        """CC payments are categorized as CC Payment."""
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()
            intel = Intelligence(self.rules)

            cat, conf = intel.get_category("PAYMENT: THANK YOU",
                                           amount=Decimal("-500.00"))
            self.assertEqual(cat, "CC Payment")
            self.assertEqual(conf, "rule")

            cat, conf = intel.get_category("AUTOPAY PAYMENT - THANK YOU",
                                           amount=Decimal("-1200.00"))
            self.assertEqual(cat, "CC Payment")
            self.assertEqual(conf, "rule")

    def test_longest_keyword_wins_whatever_the_file_order(self):
        """A generic keyword listed first must not shadow a specific
        one listed later: "Amazon" under Shopping & Retail came early
        in the real rules.json and swallowed supplements and pet food
        under first-match-in-file-order."""
        rules = {
            "Shopping & Retail": ["Amazon"],
            "Wellness": ["Fish Oil"],
        }
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect(heuristics={})
            intel = Intelligence(rules)
            self.assertEqual(
                intel.get_category("Amazon: Maple Fish Oil 1000mg")[0],
                "Wellness",
            )
            self.assertEqual(
                intel.get_category("Amazon: Maple Desk Lamp")[0],
                "Shopping & Retail",
            )

    def test_equal_length_keywords_keep_file_order(self):
        rules = {"Groceries": ["Maple"], "Home & Garden": ["Patio"]}
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect(heuristics={})
            intel = Intelligence(rules)
            self.assertEqual(
                intel.get_category("Maple Patio Market")[0], "Groceries",
            )

    def test_load_rules_reads_category_map(self):
        import json
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as f:
            json.dump(self.rules, f)
        self.assertEqual(load_rules(path), self.rules)

    def test_load_rules_missing_file_names_the_fix(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            load_rules("/nonexistent/rules.json")
        self.assertIn("housebook-init-db", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
