"""Workspace resolution: no default, and never the code checkout."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from housebook.config.settings import resolve_workspace


class TestResolveWorkspace(unittest.TestCase):
    def test_unset_exits_with_guidance(self):
        with self.assertRaises(SystemExit) as cm:
            resolve_workspace(None)
        self.assertIn("HOUSEBOOK_WORKSPACE_DIR", str(cm.exception))

    def test_blank_counts_as_unset(self):
        """.env.example ships the key with an empty value."""
        for blank in ("", "   "):
            with self.assertRaises(SystemExit):
                resolve_workspace(blank)

    def test_missing_directory_exits(self):
        """A typo or unmounted drive must not grow a second ledger."""
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                resolve_workspace(os.path.join(d, "absent"))
        self.assertIn("does not exist", str(cm.exception))

    def test_existing_directory_is_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                resolve_workspace(f"  {d}  "), Path(d).resolve(),
            )


class TestDemoSeedPinsItsWorkspace(unittest.TestCase):
    def test_importing_demo_seed_leaves_settings_unloaded(self):
        """demo_seed.main() pins the workspace through the environment,
        which only works while settings has not been imported yet."""
        code = (
            "import sys, housebook.demo_seed; "
            "print('housebook.config.settings' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
