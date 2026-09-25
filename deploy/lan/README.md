# Authenticated LAN deployment

Status: repository profile implemented; live host not yet configured

This profile gives each authorized person a Google sign-in while keeping
account authorization local and explicit:

```text
LAN browser
  -> Caddy (HTTPS and private hostname)
  -> OAuth2 Proxy (Google authentication and email allowlist)
  -> FastAPI on 127.0.0.1:8000

Browser on the server
  -> http://localhost:8000
  -> explicit account-free loopback path
```

Google proves who owns an account. The one-email-per-line file decides who may
use Housebook. No local passwords, Google access tokens, or Google ID
tokens reach the application.

The account-free path is deliberately narrower than "local network." It is
enabled by `HOUSEBOOK_ALLOW_LOCAL_BYPASS=true` and accepted only when
both the TCP peer and the requested hostname are loopback. Opening the private
LAN hostname from the server still uses Google. State-changing localhost
requests must also carry an exact localhost Origin from the configured list.

The committed files use reserved `example.test` names and fictitious Ledger
Family addresses. Replace them only in copies under
`/etc/housebook`; never commit the live hostname, Gmail addresses,
client credentials, cookie secret, proxy secret, or workspace path.

## 1. Create the Google OAuth client

Use a dedicated Google Cloud project. This deployment uses an ordinary OAuth
2.0 web client and the open-source OAuth2 Proxy; it does not use Google
Identity Platform or an application-managed account database.

1. Configure the Google Auth Platform audience as **External**. Personal Gmail
   accounts cannot use a Google Workspace-only Internal audience.
2. Request only `openid`, `email`, and `profile`. Housebook does not
   need Gmail, Drive, Calendar, or offline-access scopes.
3. During initial testing, add each intended Gmail address as a test user.
   Google's Testing status is capped at 100 test users and authorizations can
   expire after seven days. Move to In production once the configuration has
   been validated. Basic identity scopes are non-sensitive, although Google
   may still require the configured branding/domain details or show an
   unverified-app warning according to the project's publication status.
4. Create an OAuth client of type **Web application**.
5. Set the authorized JavaScript origin to the exact live origin, for example
   `https://housebook.example.test` after replacing the reserved hostname.
6. Set the authorized redirect URI to that same origin plus
   `/oauth2/callback`. It is an exact match, including scheme, hostname, path,
   and absence of a trailing slash.

Google's current setup and redirect rules:

- <https://developers.google.com/identity/openid-connect/openid-connect>
- <https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation>
- <https://support.google.com/cloud/answer/15549945>
- <https://support.google.com/cloud/answer/13463073>

## 2. Keep the hostname private

Create a dedicated host below a domain you control and resolve it to the
server's LAN address through router DNS or split-horizon DNS. Do not add router
port forwarding. Raw IP redirect URIs are not accepted by Google except for
localhost, which is why an owned hostname is required even for a LAN-only
service.

The example Caddyfile uses `tls internal`. Install Caddy's local root CA on
each authorized client and nowhere else. A browser warning is not an acceptable
steady state, and bypassing it with insecure flags defeats the TLS boundary.
If you later use a publicly trusted certificate through a DNS challenge, keep
the service itself LAN-only.

Caddy local HTTPS guidance:

- <https://caddyserver.com/docs/running#local-https-with-systemd>

## 3. Install live configuration outside the data paths

Create `/etc/housebook` with access limited to the service
administrator and the relevant service accounts. Copy and edit:

| Repository example | Live path |
| --- | --- |
| `oauth2-proxy.cfg.example` | `/etc/housebook/oauth2-proxy.cfg` |
| `authorized-emails.example.txt` | `/etc/housebook/authorized-emails.txt` |
| `app.env.example` | `/etc/housebook/app.env` |
| `caddy.env.example` | `/etc/housebook/caddy.env` |
| `oauth2-proxy.env.example` | `/etc/housebook/oauth2-proxy.env` |

Copy `Caddyfile.example` into the host's Caddy configuration and ensure its
service loads `caddy.env`. Configure the app and OAuth2 Proxy services to load
only their respective environment files. Restrict environment and allowlist
files to their owner; they contain credentials or personal data.

Generate independent secrets locally. OAuth2 Proxy documents this cookie
secret form:

```bash
python -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
openssl rand -hex 32
```

Use the first output as `OAUTH2_PROXY_COOKIE_SECRET`. Use the second as
`HOUSEBOOK_PROXY_SECRET` in both `app.env` and `caddy.env`. Put the
Google client ID and client secret only in `oauth2-proxy.env`.

OAuth2 Proxy's current configuration reference:

- <https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/>
- <https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/>
- <https://oauth2-proxy.github.io/oauth2-proxy/configuration/integrations/caddy/>

## 4. Validate before starting services

Replace the reserved hostname consistently in the OAuth2 Proxy config and the
three environment files. Then run the installed versions' native validators:

```bash
oauth2-proxy --config=/etc/housebook/oauth2-proxy.cfg --config-test
caddy validate --config /etc/caddy/Caddyfile
```

Start in this order:

1. Housebook, with `app.env`, on `127.0.0.1:8000`.
2. OAuth2 Proxy, with its config and environment, on `127.0.0.1:4180`.
3. Caddy, with `caddy.env`, on the LAN HTTPS interface.

All three services must run on the same host for this loopback topology. The
app deliberately refuses a LAN bind, and OAuth2 Proxy's trusted-proxy list
accepts forwarded headers only from loopback.

## 5. Acceptance checks

Perform these before considering the dashboard available:

- A browser on the server reaches `http://localhost:8000/api/session` without
  Google and sees `authentication_path` set to `local-bypass`.
- The same unauthenticated request is rejected when either its transport peer
  or requested hostname is not loopback. Ports 8000 and 4180 remain
  unreachable from a second LAN machine.
- A LAN browser cannot connect before it trusts the Caddy CA, then reaches the
  Google sign-in flow over HTTPS.
- A Google account absent from `authorized-emails.txt` receives 403; each
  listed account reaches `/api/session` and sees its own normalized email.
- Static assets, PDFs, `/api/health`, and all application pages prompt for
  authentication just like `/`.
- A mutation from the live origin succeeds, while one with a missing or
  different `Origin` is rejected.
- Removing an address from the live allowlist and restarting OAuth2 Proxy
  revokes that user on the next request. Keep the eight-hour cookie expiry as
  an additional session bound.
- The router has no public port forward for Caddy.

The repository does not install packages, edit DNS, create Google resources,
trust a CA, change the firewall, or start services automatically. Those are
external state changes and require an explicit, host-specific rollout. The
encrypted-workspace backup/restore gate in
`../../docs/architecture/tax-security-roadmap.md` also remains separate.
