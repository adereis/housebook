import json
import os
import re
from decimal import Decimal
from typing import Dict, List, Union

from housebook.config.settings import EXCLUSIONS_JSON, HEURISTICS_JSON

from .models import CATEGORY_CC_PAYMENT, CATEGORY_TRANSFERS_REFUNDS

CC_PAYMENT_PATTERNS = [
    r"(?i)\bpayment[:\s]*thank\s+you\b",
    r"(?i)\bautopay\s+payment\b",
    r"(?i)\bonline\s+payment\b",
    r"(?i)\bint\s+sch\s+pymt\s+transfer\b",
]


def load_rules(path: str) -> Dict[str, List[str]]:
    """Read the category → keywords map from the workspace rules.json.

    rules.json is the only rule store: ingest and `apply-rules` both
    read it, and it also lists the categories the dashboard offers.
    A DB copy of the rules, seeded once and edited separately, used to
    drift from it.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found; run housebook-init-db to create it "
            f"from the template"
        )
    with open(path, "r") as f:
        return json.load(f)


class Intelligence:
    """Suggest a category for a description; used at ingest and by
    `housebook-audit apply-rules`, so both always agree."""

    def __init__(self, rules: Dict[str, List[str]]):
        self.rules_by_category = self._organize_rules(rules)

        # Load externalized configurations
        exclusions_data = self._load_json(EXCLUSIONS_JSON, {})
        self.exclusions = exclusions_data.get("transaction_keywords", [])
        heuristics_data = self._load_json(HEURISTICS_JSON, {})
        self.heuristics_flat = self._prepare_heuristics(heuristics_data)

    def _load_json(self, path: str, default):
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        return default

    def _organize_rules(self, rules: Dict[str, List[str]]) -> List[Dict]:
        # The longest matching keyword wins, whichever category it is
        # in, so a specific keyword ("fish oil") beats a generic one
        # ("amazon") without any care for file order. sorted() is
        # stable, so keywords of equal length keep their file order.
        flat_rules = [
            {"keyword": kw.lower(), "category": cat}
            for cat, keywords in rules.items()
            for kw in keywords
        ]
        return sorted(flat_rules, key=lambda x: len(x["keyword"]), reverse=True)

    def _prepare_heuristics(self, heuristics: Dict[str, List[str]]) -> List[Dict]:
        flat = []
        for cat, keywords in heuristics.items():
            for kw in keywords:
                flat.append({"keyword": kw.lower(), "category": cat})
        return sorted(flat, key=lambda x: len(x["keyword"]), reverse=True)

    def get_category(
        self, desc: str, amount: Union[Decimal, float, int] = 0
    ) -> tuple[str, str]:
        """Returns (category, confidence_level).
        Confidence levels: 'rule', 'heuristic', 'guess'.
        """
        d = desc.lower()

        # 1. CC Payment Detection (before exclusions — more specific)
        if amount < 0:
            for pat in CC_PAYMENT_PATTERNS:
                if re.search(pat, desc):
                    return CATEGORY_CC_PAYMENT, "rule"

        # 1b. EXCLUSIONS (High Priority)
        for excl in self.exclusions:
            if re.search(excl, desc, re.IGNORECASE):
                return CATEGORY_TRANSFERS_REFUNDS, "rule"

        # 2. USER RULES (Prioritize Longest/Most Specific Match)
        for rule in self.rules_by_category:
            # Word boundaries avoid partial matches
            pattern = r"\b" + re.escape(rule["keyword"]) + r"\b"
            if re.search(pattern, d):
                # Hard Constraint: Amazon items are never Dining & Takeout
                if rule["category"] == "Dining & Takeout" and "amazon" in d:
                    continue
                return rule["category"], "rule"

        # 3. HEURISTICS (Prioritize Longest/Most Specific Match)
        for h in self.heuristics_flat:
            pattern = r"\b" + re.escape(h["keyword"]) + r"\b"
            if re.search(pattern, d):
                # Hard Constraint: Amazon items are never Dining & Takeout
                if h["category"] == "Dining & Takeout" and "amazon" in d:
                    return "Shopping & Retail", "heuristic"

                if h["category"] == "Bills & Utilities" and "amazon" in d:
                    return "Shopping & Retail", "heuristic"

                return h["category"], "heuristic"

        return "Miscellaneous", "guess"
