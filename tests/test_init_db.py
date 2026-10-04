"""`housebook-init-db` gives a workspace its live rules.json."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch


class TestInitDbRules(unittest.TestCase):

    def setUp(self):
        import shutil
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws)
        self.rules = os.path.join(self.ws, "config", "rules.json")
        self.template = os.path.join(self.ws, "rules.example.json")
        with open(self.template, "w") as f:
            json.dump({"Groceries": ["Maple Market"]}, f)

    def _init(self):
        from housebook import init_db
        with patch.object(init_db, "DB_PATH",
                          os.path.join(self.ws, "data", "finance.db")), \
             patch.object(init_db, "BACKUP_DIR",
                          os.path.join(self.ws, "data", "backups")), \
             patch.object(init_db, "RULES_JSON", self.rules), \
             patch.object(init_db, "EXAMPLE_RULES_JSON", self.template), \
             patch("builtins.print"):
            init_db.init_db()

    def test_missing_rules_json_is_created_from_template(self):
        self._init()
        with open(self.rules) as f:
            self.assertEqual(json.load(f), {"Groceries": ["Maple Market"]})

    def test_existing_rules_json_is_never_touched(self):
        os.makedirs(os.path.dirname(self.rules))
        with open(self.rules, "w") as f:
            json.dump({"Pets": ["Penny Pet Shop"]}, f)
        self._init()
        with open(self.rules) as f:
            self.assertEqual(json.load(f), {"Pets": ["Penny Pet Shop"]})


if __name__ == "__main__":
    unittest.main()
