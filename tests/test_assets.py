import re
import unittest

from fastapi.testclient import TestClient

from housebook.assets import LICENSE_FILES, STATIC_ROOT, VENDOR_FILES


class TestDashboardAssets(unittest.TestCase):
    def test_generated_assets_and_licenses_are_committed(self):
        expected = [
            STATIC_ROOT / "css" / "dashboard.css",
            *(STATIC_ROOT / path for path in VENDOR_FILES.values()),
            *(STATIC_ROOT / path for path in LICENSE_FILES.values()),
        ]
        for path in expected:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing asset: {path}")
                self.assertGreater(path.stat().st_size, 100)

        css = (STATIC_ROOT / "css" / "dashboard.css").read_text()
        self.assertIn(".bg-blue-600", css)

    def test_templates_have_no_remote_runtime_assets(self):
        templates = STATIC_ROOT.parent / "templates"
        remote_asset = re.compile(
            r"<(?:script|link)[^>]+(?:src|href)=[\"']https?://",
            re.IGNORECASE,
        )
        for path in templates.glob("*.html"):
            with self.subTest(template=path.name):
                self.assertIsNone(remote_asset.search(path.read_text()))

    def test_static_assets_are_served_by_the_app(self):
        from housebook.app import app

        client = TestClient(app)
        for path, content_type in (
            ("/static/css/dashboard.css", "text/css"),
            ("/static/vendor/vue.global.prod.js", "text/javascript"),
            ("/static/vendor/chart.umd.min.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.headers["content-type"])
                self.assertGreater(len(response.content), 100)


if __name__ == "__main__":
    unittest.main()
