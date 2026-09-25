import re
import unittest
from pathlib import Path

DEPLOY_DIR = Path(__file__).parents[1] / "deploy" / "lan"


class TestLanDeploymentProfile(unittest.TestCase):
    """Committed examples preserve the app/proxy security contract."""

    def test_caddy_authenticates_every_application_path(self):
        config = (DEPLOY_DIR / "Caddyfile.example").read_text()

        self.assertIn("handle /oauth2/*", config)
        self.assertIn("forward_auth 127.0.0.1:4180", config)
        self.assertIn("uri /oauth2/auth", config)
        self.assertIn("copy_headers X-Auth-Request-Email", config)
        self.assertIn("reverse_proxy 127.0.0.1:8000", config)
        self.assertIn("X-Housebook-Proxy-Secret", config)
        self.assertNotIn("handle /api/health", config)

    def test_oauth_proxy_uses_only_identity_scopes_and_email_file(self):
        config = (DEPLOY_DIR / "oauth2-proxy.cfg.example").read_text()

        self.assertIn('scope = "openid email profile"', config)
        self.assertIn("authenticated_emails_file", config)
        self.assertIn("set_xauthrequest = true", config)
        self.assertIn("pass_access_token = false", config)
        self.assertIn("pass_authorization_header = false", config)
        self.assertNotRegex(
            config,
            re.compile(r'^email_domains\s*=\s*\[\s*"\*"', re.MULTILINE),
        )

    def test_examples_contain_only_reserved_identities(self):
        addresses = (
            DEPLOY_DIR / "authorized-emails.example.txt"
        ).read_text().splitlines()

        self.assertGreater(len(addresses), 0)
        self.assertTrue(
            all(address.endswith("@example.test") for address in addresses),
        )
        for path in DEPLOY_DIR.iterdir():
            if path.is_file():
                self.assertNotIn("@gmail.com", path.read_text())

    def test_example_secret_values_fail_closed(self):
        app_environment = (DEPLOY_DIR / "app.env.example").read_text()
        caddy_environment = (DEPLOY_DIR / "caddy.env.example").read_text()
        oauth_environment = (
            DEPLOY_DIR / "oauth2-proxy.env.example"
        ).read_text()

        self.assertIn("HOUSEBOOK_PROXY_SECRET=\n", app_environment)
        self.assertIn("HOUSEBOOK_PROXY_SECRET=\n", caddy_environment)
        self.assertIn("OAUTH2_PROXY_CLIENT_ID=\n", oauth_environment)
        self.assertIn("OAUTH2_PROXY_CLIENT_SECRET=\n", oauth_environment)
        self.assertIn("OAUTH2_PROXY_COOKIE_SECRET=\n", oauth_environment)

    def test_app_profile_preserves_only_direct_loopback_bypass(self):
        environment = (DEPLOY_DIR / "app.env.example").read_text()

        self.assertIn(
            "HOUSEBOOK_ALLOW_LOCAL_BYPASS=true",
            environment,
        )
        self.assertIn("http://localhost:8000", environment)
        self.assertIn("http://127.0.0.1:8000", environment)
        self.assertNotIn("HOUSEBOOK_HOST=0.0.0.0", environment)


if __name__ == "__main__":
    unittest.main()
