"""Verify all console_scripts entry points are importable.

This catches the regression where a module is deleted or renamed
but its entry point in pyproject.toml is not updated — the CLI
command silently breaks with a ModuleNotFoundError at runtime.
"""

import importlib
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _load_entry_points():
    """Parse console_scripts from pyproject.toml."""
    toml_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    return data.get("project", {}).get("scripts", {})


class TestEntryPoints(unittest.TestCase):
    """Every console_scripts entry point must resolve to an importable module."""

    def test_all_entry_points_importable(self):
        scripts = _load_entry_points()
        self.assertTrue(scripts, "No console_scripts found in pyproject.toml")

        failures = []
        for name, target in scripts.items():
            module_path, func_name = target.rsplit(":", 1)
            try:
                mod = importlib.import_module(module_path)
            except (ImportError, ModuleNotFoundError) as e:
                failures.append(f"{name} -> {target}: {e}")
                continue
            if not hasattr(mod, func_name):
                failures.append(
                    f"{name} -> {target}: module exists but "
                    f"'{func_name}' not found"
                )

        if failures:
            self.fail(
                "Broken entry points:\n  " + "\n  ".join(failures)
            )


if __name__ == "__main__":
    unittest.main()
