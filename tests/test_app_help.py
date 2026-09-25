import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

PAGES = {
    "/spending": "spending",
    "/tax": "tax",
    "/hsa": "hsa",
    "/spending/trip/1": "trips",
}


class TestHelpDrawer(unittest.TestCase):
    """Every page carries the help drawer, opening at its own topic."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE transactions (id INTEGER, date TEXT)")
        conn.close()
        self.patcher = patch("housebook.app.DB_PATH", self.db_path)
        self.patcher.start()

        from housebook.app import app
        self.client = TestClient(app)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def test_every_page_has_every_topic(self):
        from housebook.app import HELP_SECTIONS

        for page in PAGES:
            html = self.client.get(page).text
            for anchor, _ in HELP_SECTIONS:
                with self.subTest(page=page, anchor=anchor):
                    self.assertIn(f'id="help-{anchor}"', html)
                    self.assertIn(f'data-help-goto="{anchor}"', html)

    def test_help_opens_at_the_current_pages_topic(self):
        for page, topic in PAGES.items():
            with self.subTest(page=page):
                html = self.client.get(page).text
                self.assertRegex(
                    html, rf'id="help-open"\s+data-help-topic="{topic}"')

    def test_default_topics_exist(self):
        """A page's topic must name a section, or the drawer opens nowhere."""
        from housebook.app import HELP_SECTIONS

        anchors = {anchor for anchor, _ in HELP_SECTIONS}
        self.assertLessEqual(set(PAGES.values()), anchors)

    def test_drawer_is_a_dialog_outside_the_vue_app(self):
        """Static help markup must not be compiled by a page's Vue app."""
        html = self.client.get("/spending").text
        app_start = html.index('<div id="app"')
        drawer = html.index('<dialog id="help-drawer"')
        script = html.index("<script>", app_start)
        self.assertLess(app_start, drawer)
        self.assertLess(drawer, script)
        self.assertEqual(
            len(re.findall(r'<div id="app"', html)), 1)
        self.assertNotIn("{{", html[drawer:script])

    def test_standalone_help_page_is_gone(self):
        self.assertEqual(self.client.get("/help").status_code, 404)


if __name__ == "__main__":
    unittest.main()
