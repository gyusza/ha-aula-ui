# Third-party code

`vendor/easyiq/` is copied from the MIT-licensed EasyIQ integration:
https://github.com/esbenwiberg/easyiq

Pinned revision: `1d90491bca554a6aff369a3edfa3fae289f8b382`.
The full licence and copyright notice are retained at `vendor/easyiq/LICENSE`.

The Aula login client is reused for its OAuth PKCE → Unilogin/SAML →
interactive MitID app → Aula token exchange. `calendar_shapes.py` contains
the event-shape helpers extracted unchanged from the integration's
`custom_components/aula_easyiq/client.py`; its HA client/configuration and
integration scaffolding are not included or run.

Local changes:

- Removed the unused ASCII QR display thread, which could survive cancellation.
- Use cryptographically secure randomness for the SRP ephemeral private value.

The wrapper in `aula_ui/account_auth.py` supplies bounded, cancellable HTTP requests,
strict callback origin/state validation, browser identity selection, encrypted
token persistence and sanitized error reporting. It never invokes upstream
plaintext token-file helpers. MitID authentication is interactive, not bypassed.

The authentication storage and direct API adapters were adapted from the
author's earlier standalone Aula web app. The add-on contains its own copies;
no standalone server or additional HA integration is required.

The unofficial Aula API usage was informed by the GPL-licensed
[scaarup/aula](https://github.com/scaarup/aula) integration, used as a
reference only; none of its code is included.
