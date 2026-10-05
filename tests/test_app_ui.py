"""Interaction invariants the dashboard templates must keep.

The templates are Vue apps compiled in the browser, and the test suite
has no browser. These tests read the template source instead and check
the markup each behavior depends on, across every page, so a page
added or edited later is held to the same rule.
"""
import re
import unittest
from html.parser import HTMLParser

from housebook.assets import STATIC_ROOT

TEMPLATES = STATIC_ROOT.parent / "templates"


class _Tags(HTMLParser):
    """Collect every start tag with its attributes."""

    def __init__(self):
        super().__init__()
        self.tags = []
        self.options = []  # (select attrs, option attrs) pairs
        self._select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "select":
            self._select = attrs
        elif tag == "option" and self._select is not None:
            self.options.append((self._select, attrs))

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


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


class TestUrlState(unittest.TestCase):
    """A page that keeps its state in the hash follows hash changes."""

    def test_pages_writing_the_hash_listen_for_hashchange(self):
        """replaceState fires no event, so the page must listen itself.

        Without the listener, Back, Forward or an edited address change
        the hash while the screen keeps the old filters.
        """
        writers = [
            path for path in _templates()
            if "history.replaceState" in path.read_text()
        ]
        self.assertTrue(writers, "no page writes the hash; check is stale")
        for path in writers:
            with self.subTest(template=path.name):
                self.assertTrue(
                    "addEventListener('hashchange'" in path.read_text(),
                    "writes the hash but never listens for hashchange")


class TestCategorySelects(unittest.TestCase):
    """A category select never shows blank for a row's own category."""

    def test_row_category_selects_offer_the_current_category(self):
        """`categories` lists only assignable categories.

        A row can still carry another one, such as Uncategorized. A
        select bound to it must offer that value, or the browser shows
        an empty box.
        """
        bound = re.compile(r"^(\w+)\.category$")
        found = 0
        for path in _templates():
            parser = _parse(path)
            # Selects filled from the transaction category list and
            # bound to a row; a tax document's list is a different one.
            selects = [
                select for select, option in parser.options
                if option.get("v-for") == "cat in categories"
                and bound.match(select.get(":value", ""))
            ]
            for select in selects:
                found += 1
                row = bound.match(select[":value"]).group(1)
                guards = [
                    option.get("v-if") for parent, option in parser.options
                    if parent is select
                ]
                with self.subTest(template=path.name, row=row):
                    self.assertIn(
                        f"!categories.includes({row}.category)", guards)
        self.assertGreater(found, 0, "no category select; check is stale")

    def test_category_filter_offers_categories_found_in_the_data(self):
        """The filter lists the rows' categories, not just assignable ones."""
        parser = _parse(TEMPLATES / "spending.html")
        options = [
            option.get("v-for") for select, option in parser.options
            if select.get("v-model") == "filters.category"
        ]
        self.assertIn("cat in filterCategories", options)


if __name__ == "__main__":
    unittest.main()
