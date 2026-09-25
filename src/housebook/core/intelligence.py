import json
import os
import re
from decimal import Decimal
from typing import Dict, List, Union

from housebook.config.settings import EXCLUSIONS_JSON, HEURISTICS_JSON

from .models import CATEGORY_CC_PAYMENT, CATEGORY_TRANSFERS_REFUNDS, CategorizationRule

CC_PAYMENT_PATTERNS = [
    r"(?i)\bpayment[:\s]*thank\s+you\b",
    r"(?i)\bautopay\s+payment\b",
    r"(?i)\bonline\s+payment\b",
    r"(?i)\bint\s+sch\s+pymt\s+transfer\b",
]


class Intelligence:
    def __init__(self, rules: List[CategorizationRule]):
        self.rules = rules
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

    def _organize_rules(self, rules: List[CategorizationRule]) -> List[Dict]:
        # Flatten rules into a list of (keyword, category) and sort by length descending
        flat_rules = [
            {"keyword": r.keyword.lower(), "category": r.category} for r in rules
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
