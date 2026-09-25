import unittest
from decimal import Decimal
from unittest.mock import patch

from housebook.core.intelligence import Intelligence
from housebook.core.models import CategorizationRule

_PATCH_TARGET = (
    "housebook.core.intelligence"
    ".Intelligence._load_json"
)


class TestIntelligence(unittest.TestCase):

    def setUp(self):
        # Sample rules for testing
        self.rules = [
            CategorizationRule(
                category="Dining & Takeout",
                keyword="Starbucks",
            ),
            CategorizationRule(
                category="Groceries",
                keyword="Whole Foods",
            ),
        ]

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

            rules = [
                CategorizationRule(
                    category="Dining & Takeout",
                    keyword="Amazon",
                ),
            ]
            intel = Intelligence(rules)
            cat, conf = intel.get_category("Amazon: Starbucks Coffee")
            self.assertEqual(cat, "Shopping & Retail")
            self.assertEqual(conf, "heuristic")

    def test_word_boundary_matching(self):
        with patch(_PATCH_TARGET) as mock_load:
            mock_load.side_effect = self._json_side_effect()

            rules = [
                CategorizationRule(
                    category="Alcohol & Specialty",
                    keyword="Wine",
                ),
            ]
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

            intel = Intelligence([])
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


if __name__ == "__main__":
    unittest.main()
