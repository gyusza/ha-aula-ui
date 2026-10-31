# Optional: publishing Aula UI behind Cloudflare Access

Aula UI works out of the box through Home Assistant ingress (the sidebar
panel). This guide is only needed if you want to open the app directly on its
own hostname, for example to install it as a standalone PWA on a phone,
receive Web Push deep links, or connect an MCP client to the protected endpoint.

The examples use `aula.example.com` and a Cloudflare Tunnel; replace them with
your own hostname. Cloudflare Access sits in front of the app, and the add-on
additionally validates the `Cf-Access-Jwt-Assertion` header itself, so a
request that bypasses Access is rejected (403), except the secret-token
calendar subscription described below. Without the Access options configured,
normal app routes only serve Home Assistant ingress.

## 0. Route a hostname to the add-on

Expose the add-on's port 8099 through a Cloudflare Tunnel. With the community
**Cloudflared** add-on, add an additional host to its configuration:

```yaml
additional_hosts:
  - hostname: aula.example.com
    service: http://<aula-ui-hostname>:8099
```

`<aula-ui-hostname>` is the internal hostname shown on the Aula UI add-on's
Info page (for example `local-aula-ui` for a locally installed copy, or
`<repository-id>-aula-ui` when installed from a repository). Depending on how
the tunnel is managed, create the public hostname/DNS record in the Cloudflare
dashboard or let the Cloudflared add-on create it. Until Access is configured
the site answers 403 ("Access denied") to everyone, which is safe.

## 1. Create the Access application

1. Cloudflare dashboard → **Zero Trust** (one.dash.cloudflare.com) →
   **Access controls → Applications** (older UI: *Access → Applications*) →
   **Add an application → Self-hosted**.
2. Application name: `Aula UI`. **Session duration**: e.g. `24 hours`
   (shorter = safer; the PWA re-authenticates when it expires).
3. **Add public hostname**: your app hostname (e.g. `aula.example.com`), path empty.
4. Identity providers: enable only the one(s) you trust (see MFA below). Turn
   on *Instant auth* if only one is enabled.
5. Next → create the policies (section 2 and 3), then **Save**.

## 2. Allow policy with MFA

Create policy **"Family"**, action **Allow**:

- **Include → Emails**: the exact addresses of the people who may use the app.
- **Require → Authentication method → `mfa`**.

`Authentication method` (the `amr` claim) only works with identity providers
that report it: Azure AD/Entra, Okta, or generic OIDC/SAML IdPs that send
`amr`. It does **not** work with the built-in One-time PIN, Google or GitHub
logins (they never report `mfa`), so the rule would deny everyone. Options:

- Use Google/GitHub/Apple and enforce MFA/passkeys **in that account** (the
  simplest; Access cannot verify it), plus the email allow-list; or
- Use an Entra/Okta/OIDC IdP that sends `amr` and keep the `mfa` Require rule; or
- Add **Require → Login Methods** (pin to a single IdP) so the One-time PIN
  fallback cannot be used instead of the MFA-protected IdP.

Add a final catch-all? Not needed: anything not matching Allow is denied.

## 3. PWA files: manifest, service worker, icons

Installing the PWA fetches `/manifest.webmanifest`, `/sw.js` and `/icons/*`.
Access redirects unauthenticated requests to a login page; browsers fetch the
manifest without credentials by default, which makes install fail.

**Recommended (safer, nothing bypassed):** keep everything protected and have
the manifest link use credentials:
`<link rel="manifest" href="manifest.webmanifest" crossorigin="use-credentials">`
(the front-end change). The `CF_Authorization` cookie is then sent. Service
worker script fetches already include cookies for same-origin requests.

**Do not use Bypass for these paths.** The add-on rejects every request that
has no valid Access JWT (bypassed requests carry none), so a bypass would
only turn the redirect into a 403. Keep the whole hostname protected.

## 3b. Calendar subscription

Phone calendar apps cannot log in to Access. Add a second self-hosted Access
application for `<host>/api/aula_calendar/*` with a single **Bypass /
Everyone** policy. The more specific path takes precedence; everything else
on the hostname stays protected. The add-on serves only the exact current
secret-token `.ics` path without a JWT (wrong or rotated tokens get 404) and
still requires Access for normal app routes.

## 3c. MCP endpoint

The in-add-on MCP endpoint is `<host>/mcp`. Keep it covered by the same
Cloudflare Access application as the rest of Aula UI. The add-on verifies the
Access JWT itself; do not add a Bypass policy for this path. MCP clients must
be able to complete the existing Access sign-in. From Home Assistant ingress,
the endpoint is `/mcp` under the authenticated add-on panel; do not share the
ingress URL because its path contains a session token.

## 4. Values for the add-on

- **Team domain**: Zero Trust → **Settings → Custom pages**, or the top of
  Settings → General: `<team>.cloudflareaccess.com`.
- **Audience (AUD) tag**: Access controls → Applications → *Aula UI* →
  **Edit** → **Overview**/Basic information → *Application Audience (AUD) Tag*
  (copy the 64-character hex string).

In Home Assistant → Settings → Apps → **Aula UI** → Configuration:

| Option | Value |
| --- | --- |
| `access_team_domain` | `<team>.cloudflareaccess.com` |
| `access_aud` | the AUD tag |
| `access_allowed_emails` | the same addresses as the policy |
| `public_url` | `https://aula.example.com` (your app hostname) |

Save and restart the add-on. Ingress (the HA panel) keeps working unchanged.

## 5. Test checklist

1. `curl -i https://aula.example.com/` without logging in → redirect to
   `*.cloudflareaccess.com` (or 403 if Access is not yet configured).
2. Open it in a private window → log in with an allowed email and MFA → the
   feed loads.
3. Log in with a non-listed email → Access denies.
4. Check the add-on log shows no JWT validation errors.
5. **iOS**: Safari → Share → *Add to Home Screen*; open the icon; it shows the
   feed standalone. **Android**: Chrome → *Install app*.
6. Settings → Notifications → **Send test**, then trigger a real item: tapping
   the push opens `https://aula.example.com/post/<id>` (login may be prompted
   once; the PWA reuses the Access session).
7. Any other hostnames served by the same tunnel still work.

## 6. Service tokens and CORS

Not needed: the browser talks only to the same origin and everything is
same-site. Only add an Access **Service Auth** policy with a service token if
a non-browser client (script, monitoring) needs access; it must send
`CF-Access-Client-Id` / `CF-Access-Client-Secret`, and the add-on's email
allow-list applies to the resulting JWT (service-token JWTs carry no email,
so they would be rejected). Do not configure CORS.

When the session expires, background `fetch` calls get a redirect to the Access
login; the PWA shows a failed load until you reopen it and re-authenticate.

## 7. Rollback

- Remove public access: remove the hostname from the tunnel (for the
  Cloudflared add-on: its *Additional hosts* option, then restart it), delete
  the DNS record and the Access application.
- Disable deep links: set `public_url` to empty.
- Emergency lockout of the public hostname without touching the tunnel:
  disable the Access application policies (everyone denied), or leave the
  `access_*` options empty so the add-on serves ingress only.

## API alternatives

Applications and policies can also be created with the Cloudflare API
(`POST /accounts/{id}/access/apps`) or Terraform
(`cloudflare_zero_trust_access_application`).
