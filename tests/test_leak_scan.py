"""housebook-leak-scan: real workspace data must not reach tracked files.

Every value below is invented. The workspace and git repo are temporary.
"""

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from housebook import leak_scan
from housebook.leak_scan import (
    Finding,
    is_allowed,
    load_allowlist,
    load_needles,
    scan,
    scan_text,
)
from housebook.migrations.runner import run_migrations


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class _Workspace(unittest.TestCase):
    """A fictitious workspace: the Ledger family's private records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ws = Path(self.tmp.name) / "workspace"
        cfg = ws / "config"
        cfg.mkdir(parents=True)
        (cfg / "pii-denylist.txt").write_text(
            "# comment\n\nQuillfeather\n",
        )
        _write_json(cfg / "user_profile.json", {
            "full_name": "REDACTED",
            "home_location": {"city": "Anytown", "zip": "99901"},
        })
        _write_json(cfg / "hsa" / "patients.json",
                    {"patients": [{"name": "Penny Ledger"}]})
        _write_json(cfg / "hsa" / "providers.json", {"providers": [
            {"canonical_name": "Maple Dental", "aliases": ["MAPLE DENT"]},
        ]})
        _write_json(
            ws / "cc" / "2025" / "2025-04__Amex__7319__2025-03-05_to_2025-04-03.json",
            {"data": {
                "account": {"last4": "7319", "name": "Blue Cash Preferred"},
                "balances": {"opening": 2418.60, "closing": 3107.45},
                "transactions": [{
                    "date": "2025-03-07", "amount": 18.75,
                    "metadata": {"passenger_name": "HOLLOWAY/PAID ECONOMY"},
                }],
            }},
        )
        _write_json(ws / "hsa" / "2025" / "eob.json", {"data": {
            "date": "2025-02-11", "amount": 87.19, "claim_id": "5550001234",
            "financials": {"billed": 412.33, "patient_responsibility": 87.19},
        }})
        _write_json(ws / "tax" / "2025" / "2025__Acme__1099__B.json",
                    {"data": {"amount": 1523.40}})

        self.db = ws / "data" / "finance.db"
        self.db.parent.mkdir()
        run_migrations(str(self.db), verbose=False)
        conn = sqlite3.connect(self.db)
        for date, amount, desc, meta in [
            ("2025-03-09", 64.37, "MARKETPLACE*QZ7XK2M4P WA",
             {"amazon_order_id": "112-1234567-7654321"}),
            ("2025-03-19", 19.00, "PARKING", None),
            ("2025-05-02", 40.00, "ROUND CHARGE", None),
        ]:
            conn.execute(
                "INSERT INTO transactions (date, description, amount, "
                "category, source, status, original_file, needs_review, "
                "metadata) VALUES (?, ?, ?, 'X', 'Amex', 'UNVERIFIED', "
                "'s.pdf', 1, ?)",
                (date, desc, amount, json.dumps(meta) if meta else None),
            )
        conn.execute(
            "INSERT INTO manual_expenses (description, amount, category, "
            "start_date, frequency) VALUES ('Workmanship', 8765, "
            "'Home & Garden', '2025-06-01', 'one-time')",
        )
        conn.commit()
        conn.close()
        self.workspace = ws
        self.needles = load_needles(ws, self.db)

    def kinds(self, text: str) -> list[tuple[str, str]]:
        return [(f.kind, f.text)
                for f in scan_text("f.py", text, self.needles)]


class TestDetectors(_Workspace):
    def test_identifiers_match_anywhere(self):
        found = self.kinds(
            'oid = "112-1234567-7654321"\n'
            "ref MKTPL*QZ7XK2M4P\n"
            "claim 5550001234\n"
        )
        self.assertIn(("identifier", "112-1234567-7654321"), found)
        self.assertIn(("identifier", "QZ7XK2M4P"), found)
        self.assertIn(("identifier", "5550001234"), found)

    def test_card_last4_needs_card_context(self):
        self.assertIn(("card last4", "7319"),
                      self.kinds('"last4": "7319"'))
        self.assertIn(("card last4", "7319"),
                      self.kinds("2025-04__Amex__7319__x.pdf"))
        self.assertIn(("card last4", "7319"),
                      self.kinds("Credit Card (* 7319)"))
        self.assertEqual(self.kinds("processed 7319 rows"), [])

    def test_tax_form_numbers_are_not_card_digits(self):
        """Tax sidecar names carry form numbers (__1099__)."""
        self.assertEqual(self.kinds('"last4": "1099"'), [])

    def test_date_and_amount_pair_within_window(self):
        text = '("2025-03-09",\n "desc",\n "cat",\n 64.37)'
        self.assertIn(("date + amount", "2025-03-09 64.37"),
                      self.kinds(text))

    def test_pair_too_far_apart_is_ignored(self):
        text = "2025-03-09\n" + "x\n" * 6 + "64.37\n"
        self.assertEqual(self.kinds(text), [])

    def test_round_amounts_are_treated_as_invented(self):
        self.assertEqual(self.kinds('("2025-05-02", 40.00)'), [])

    def test_date_digits_are_not_amounts(self):
        """The "19" in 2025-03-19 must not pair with a real $19 charge."""
        self.assertEqual(self.kinds('"date": "2025-03-19"'), [])

    def test_transaction_amount_alone_is_not_flagged(self):
        """Card amounts only count next to their date: alone they are
        too common to identify anything."""
        self.assertEqual(self.kinds("total = 64.37"), [])

    def test_distinctive_standalone_amounts(self):
        found = self.kinds(
            "billed 412.33\nowed $87.19\nlabor 8,765\nclosing 3107.45\n"
            "tax 1523.40\n"
        )
        for shown in ("412.33", "$87.19", "8,765", "3107.45", "1523.40"):
            self.assertIn(("amount", shown), found)

    def test_names_and_home_location(self):
        found = {kind for kind, _ in self.kinds(
            "Quillfeather\nAnytown 99901\nPenny\nMaple Dental\nHOLLOWAY\n"
        )}
        self.assertIn("denylist", found)
        self.assertIn("home location", found)
        self.assertIn("name (HSA patients)", found)
        self.assertIn("name (HSA providers)", found)
        self.assertTrue(any(k.startswith("name (cc sidecar") for k in found))

    def test_fare_text_and_card_products_are_not_names(self):
        """Parsers glue fare text onto passenger names, and
        `account.name` sometimes holds a card product."""
        self.assertEqual(self.kinds("PAID ECONOMY Blue Cash Preferred"), [])


class TestUnreadableWorkspace(_Workspace):
    def test_corrupt_sidecar_stops_the_scan(self):
        """Skipping it would silently shrink what the scan can detect."""
        broken = self.workspace / "hsa" / "2025" / "broken.json"
        broken.write_text("{not json")
        with self.assertRaises(SystemExit) as cm:
            load_needles(self.workspace, self.db)
        self.assertIn("broken.json", str(cm.exception))


class TestAllowlist(unittest.TestCase):
    def test_path_glob_and_case_insensitive_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "allow.txt"
            path.write_text("# intentional\nLICENSE penny\n* maple dental\n")
            allow = load_allowlist(path)
        self.assertTrue(is_allowed(
            Finding("LICENSE", 3, "name", "Penny", ""), allow))
        self.assertFalse(is_allowed(
            Finding("tests/x.py", 3, "name", "Penny", ""), allow))
        self.assertTrue(is_allowed(
            Finding("docs/a.md", 1, "name", "MAPLE DENTAL", ""), allow))

    def test_missing_file_allows_nothing(self):
        self.assertEqual(load_allowlist(Path("/nonexistent/allow.txt")), [])


class TestGitSources(_Workspace):
    def setUp(self):
        super().setUp()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")

    def git(self, *args):
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c",
             "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null",
             "-C", str(self.repo), *args],
            check=True, capture_output=True,
        )

    def write(self, rel, content):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)

    def test_index_is_scanned_not_the_working_tree(self):
        self.write("a.md", "claim 5550001234\n")
        self.git("add", "a.md")
        self.write("a.md", "clean now, but not staged\n")
        found = scan(self.repo, self.needles, [])
        self.assertEqual([(f.path, f.text) for f in found],
                         [("a.md", "5550001234")])

    def test_rev_scans_a_commit_while_index_is_clean(self):
        self.write("a.md", "claim 5550001234\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "leak")
        self.write("a.md", "fixed\n")
        self.git("add", "a.md")
        self.assertEqual(scan(self.repo, self.needles, []), [])
        self.assertEqual(len(scan(self.repo, self.needles, [], rev="HEAD")), 1)

    def test_binary_and_vendored_files_are_skipped(self):
        self.write("img.png", b"\x89PNG\0 5550001234")
        self.write("src/housebook/static/vendor/lib.js",
                   "5550001234")
        self.git("add", "-A")
        self.assertEqual(scan(self.repo, self.needles, []), [])

    def test_allowlist_is_applied(self):
        self.write("LICENSE", "Copyright 2026 Penny\n")
        self.git("add", "LICENSE")
        self.assertEqual(
            scan(self.repo, self.needles, [("LICENSE", "penny")]), [],
        )


class TestMain(unittest.TestCase):
    def test_no_workspace_skips_cleanly(self):
        env = {k: v for k, v in os.environ.items()
               if k != "HOUSEBOOK_WORKSPACE_DIR"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(leak_scan, "load_dotenv"), \
             patch("sys.stderr") as stderr:
            self.assertEqual(leak_scan.main([]), 0)
        written = "".join(c.args[0] for c in stderr.write.call_args_list)
        self.assertIn("Skipped", written)


if __name__ == "__main__":
    unittest.main()
