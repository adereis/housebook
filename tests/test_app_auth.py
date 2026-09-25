import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from housebook.app import _is_loopback_host, app


class TestProxyAuthentication(unittest.TestCase):
    """The app trusts identity only through its private proxy contract."""

    PROXY_SECRET = "fictitious-proxy-secret-0123456789abcdef"
    EMAIL = "sterling.ledger@gmail.example.test"
    ORIGIN = "https://housebook.example.test"

    def setUp(self):
        self.client = TestClient(app)
        self.local_client = TestClient(
            app,
            base_url="http://localhost:8000",
            client=("127.0.0.1", 50000),
        )

    def _proxy_environment(self, **overrides):
        values = {
            "HOUSEBOOK_AUTH_MODE": "proxy",
            "HOUSEBOOK_PROXY_SECRET": self.PROXY_SECRET,
            "HOUSEBOOK_ALLOWED_ORIGINS": self.ORIGIN,
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=True)

    def _proxy_headers(self, **overrides):
        values = {
            "x-housebook-proxy-secret": self.PROXY_SECRET,
            "x-auth-request-email": self.EMAIL,
        }
        values.update(overrides)
        return values

    def test_disabled_mode_keeps_loopback_development_available(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "authentication_mode": "disabled",
                "email": "local-user",
                "authentication_path": "local-disabled",
            },
        )

    def _disabled_post(self, **headers):
        with patch.dict(os.environ, {}, clear=True):
            return self.local_client.post("/not-a-route", headers=headers)

    def test_disabled_mode_refuses_cross_site_writes(self):
        """A page on another site must not drive the local dashboard."""
        for headers in (
            {"origin": "https://attacker.example.test"},
            {"origin": "http://localhost:3000"},
            {"origin": "null"},
            {"sec-fetch-site": "cross-site"},
            {"sec-fetch-site": "same-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self._disabled_post(**headers).status_code,
                                 403)

    def test_disabled_mode_allows_same_origin_and_non_browser_writes(self):
        for headers in (
            {"origin": "http://localhost:8000",
             "sec-fetch-site": "same-origin"},
            {"sec-fetch-site": "none"},
            {},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self._disabled_post(**headers).status_code,
                                 404)

    def test_disabled_mode_leaves_reads_alone(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.local_client.get(
                "/api/session",
                headers={"origin": "https://attacker.example.test"},
            )
        self.assertEqual(response.status_code, 200)

    def test_proxy_mode_fails_closed_when_not_configured(self):
        with self._proxy_environment(HOUSEBOOK_PROXY_SECRET="short"):
            response = self.client.get("/api/session")

        self.assertEqual(response.status_code, 503)

    def test_proxy_mode_rejects_missing_private_secret(self):
        with self._proxy_environment():
            response = self.client.get(
                "/api/session",
                headers={"x-auth-request-email": self.EMAIL},
            )

        self.assertEqual(response.status_code, 401)

    def test_proxy_mode_rejects_missing_identity(self):
        with self._proxy_environment():
            response = self.client.get(
                "/api/session",
                headers={
                    "x-housebook-proxy-secret": self.PROXY_SECRET,
                },
            )

        self.assertEqual(response.status_code, 401)

    def test_proxy_identity_is_normalized(self):
        headers = self._proxy_headers(
            **{"x-auth-request-email": self.EMAIL.upper()},
        )
        with self._proxy_environment():
            response = self.client.get("/api/session", headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], self.EMAIL)
        self.assertEqual(response.json()["authentication_path"], "proxy")

    def test_explicit_local_bypass_needs_no_account(self):
        with self._proxy_environment(
            HOUSEBOOK_ALLOW_LOCAL_BYPASS="true",
        ):
            response = self.local_client.get("/api/session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "local-user")
        self.assertEqual(
            response.json()["authentication_path"], "local-bypass",
        )

    def test_local_bypass_requires_a_loopback_peer(self):
        remote_client = TestClient(
            app,
            base_url="http://localhost:8000",
            client=("192.0.2.10", 50000),
        )
        with self._proxy_environment(
            HOUSEBOOK_ALLOW_LOCAL_BYPASS="true",
        ):
            response = remote_client.get("/api/session")

        self.assertEqual(response.status_code, 401)

    def test_local_bypass_requires_a_loopback_host(self):
        proxy_host_client = TestClient(
            app,
            base_url="https://housebook.example.test",
            client=("127.0.0.1", 50000),
        )
        with self._proxy_environment(
            HOUSEBOOK_ALLOW_LOCAL_BYPASS="true",
        ):
            response = proxy_host_client.get("/api/session")

        self.assertEqual(response.status_code, 401)

    def test_local_bypass_still_requires_origin_for_mutations(self):
        environment = {
            "HOUSEBOOK_ALLOW_LOCAL_BYPASS": "true",
            "HOUSEBOOK_ALLOWED_ORIGINS": (
                f"{self.ORIGIN},http://localhost:8000"
            ),
        }
        with self._proxy_environment(**environment):
            missing = self.local_client.post("/not-a-route")
            allowed = self.local_client.post(
                "/not-a-route",
                headers={"origin": "http://localhost:8000"},
            )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(allowed.status_code, 404)

    def test_invalid_local_bypass_setting_fails_closed(self):
        with self._proxy_environment(
            HOUSEBOOK_ALLOW_LOCAL_BYPASS="sometimes",
        ):
            response = self.client.get("/api/session")

        self.assertEqual(response.status_code, 503)

    def test_static_assets_are_also_protected(self):
        with self._proxy_environment():
            response = self.client.get("/static/css/dashboard.css")

        self.assertEqual(response.status_code, 401)

    def test_proxy_mode_rejects_unsafe_cross_origin_request(self):
        headers = self._proxy_headers(origin="https://attacker.example.test")
        with self._proxy_environment():
            response = self.client.post("/not-a-route", headers=headers)

        self.assertEqual(response.status_code, 403)

    def test_proxy_mode_allows_exact_origin(self):
        headers = self._proxy_headers(origin=self.ORIGIN)
        with self._proxy_environment():
            response = self.client.post("/not-a-route", headers=headers)

        self.assertEqual(response.status_code, 404)

    def test_unsafe_request_fails_closed_without_origin_configuration(self):
        headers = self._proxy_headers(origin=self.ORIGIN)
        with self._proxy_environment(
            HOUSEBOOK_ALLOWED_ORIGINS="",
        ):
            response = self.client.post("/not-a-route", headers=headers)

        self.assertEqual(response.status_code, 503)

    def test_only_loopback_bind_targets_are_supported(self):
        for host in ("127.0.0.1", "::1", "localhost", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertTrue(_is_loopback_host(host))

        for host in ("0.0.0.0", "192.0.2.10", "housebook.example.test"):
            with self.subTest(host=host):
                self.assertFalse(_is_loopback_host(host))


if __name__ == "__main__":
    unittest.main()
