import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class TestHelpPage(unittest.TestCase):
    """GET /help documents every feature, and each page links to it."""

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

    def test_help_has_a_section_for_every_topic(self):
        from housebook.app import HELP_SECTIONS

        response = self.client.get("/help")
        self.assertEqual(response.status_code, 200)
        for anchor, label in HELP_SECTIONS:
            with self.subTest(anchor=anchor):
                self.assertIn(f'id="{anchor}"', response.text)
                self.assertIn(f'href="#{anchor}"', response.text)

    def test_help_link_opens_the_current_pages_topic(self):
        for page in ("spending", "tax", "hsa"):
            with self.subTest(page=page):
                response = self.client.get(f"/{page}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'href="/help#{page}"', response.text)

    def test_help_page_marks_itself_current(self):
        response = self.client.get("/help")
        self.assertRegex(
            response.text, r'href="/help"\s+aria-current="page"')


if __name__ == "__main__":
    unittest.main()
