"""Interaction invariants the dashboard templates must keep.

The templates are Vue apps compiled in the browser, and the test suite
has no browser. These tests read the template source instead and check
the markup each behavior depends on, across every page, so a page
added or edited later is held to the same rule.
"""
import unittest
from html.parser import HTMLParser

from housebook.assets import STATIC_ROOT

TEMPLATES = STATIC_ROOT.parent / "templates"


class _Tags(HTMLParser):
    """Collect every start tag with its attributes."""

    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _parse(path):
    parser = _Tags()
    parser.feed(path.read_text())
    return parser


def _classes(attrs):
    return set((attrs.get("class") or "").split())


def _templates():
    return sorted(TEMPLATES.glob("*.html"))


class TestOverlays(unittest.TestCase):
    """Every overlay closes from its backdrop and from Escape."""

    def test_backdrop_click_closes_the_overlay(self):
        """A dim layer covers the whole overlay, so it gets the click.

        An `@click.self` on the overlay root never fires, because the
        click lands on the dim layer above it. The dim layer must close
        the overlay itself.
        """
        found = 0
        for path in _templates():
            for tag, attrs in _parse(path).tags:
                classes = _classes(attrs)
                is_backdrop = {"fixed", "inset-0"} <= classes and any(
                    c.startswith("bg-black/") for c in classes)
                if not is_backdrop:
                    continue
                found += 1
                with self.subTest(template=path.name):
                    self.assertIn("@click", attrs)
        self.assertGreater(found, 0, "no backdrop found; selector is stale")

    def test_escape_closes_the_overlay(self):
        """Escape reaches an overlay only while focus is inside it.

        So each overlay root handles `keydown.escape`, can take focus,
        and is focused through its ref when it opens.
        """
        found = 0
        for path in _templates():
            source = path.read_text()
            for tag, attrs in _parse(path).tags:
                classes = _classes(attrs)
                is_overlay = {"fixed", "inset-0"} <= classes and any(
                    c.startswith("z-") for c in classes)
                if not is_overlay:
                    continue
                found += 1
                with self.subTest(template=path.name, ref=attrs.get("ref")):
                    self.assertIn("@keydown.escape", attrs)
                    self.assertEqual(attrs.get("tabindex"), "-1")
                    ref = attrs.get("ref")
                    self.assertTrue(ref, "overlay has no ref to focus")
                    self.assertTrue(
                        f"{ref}.value?.focus()" in source,
                        f"nothing focuses {ref} when it opens")
        self.assertGreater(found, 0, "no overlay found; selector is stale")


if __name__ == "__main__":
    unittest.main()
