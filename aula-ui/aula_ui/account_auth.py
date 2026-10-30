import base64
import logging
import secrets
import threading
import time
from contextvars import copy_context
from urllib.parse import parse_qs, urlparse

import requests

from vendor.easyiq.aula_login_client.client import AulaLoginClient
from vendor.easyiq.aula_login_client.exceptions import OAuthError
from .diagnostics import observed, protect

LOGGER = logging.getLogger(__name__)
TOKEN_URL = "https://login.aula.dk/simplesaml/module.php/oidc/token.php"
CLIENT_ID = "_99949a54b8b65423862aac1bf629599ed64231607a"

# Upstream debug logs can contain OAuth URLs, identities and proofs.
for name in [
    "vendor.easyiq.aula_login_client.client",
    "vendor.easyiq.aula_login_client.mitid_browserclient.BrowserClient",
    "vendor.easyiq.aula_login_client.mitid_browserclient.Helpers",
]:
    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False

# HTTP debug logs include Aula's access-token query parameter.
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)


class AuthenticationError(Exception):
    pass


class BoundedSession(requests.Session):
    def __init__(self, cancelled=None, deadline=None):
        super().__init__()
        self.cancelled = cancelled
        self.deadline = deadline

    def request(self, method, url, **kwargs):
        if self.cancelled and self.cancelled.is_set():
            raise AuthenticationError("Authentication cancelled.")
        if self.deadline and time.monotonic() >= self.deadline:
            raise AuthenticationError("Authentication timed out. Start again.")
        kwargs.setdefault("timeout", (10, 30))
        remaining = self.deadline - time.monotonic() if self.deadline else 30
        if self.deadline:
            kwargs["timeout"] = (min(10, max(0.1, remaining)), min(30, max(0.1, remaining)))
        started = time.monotonic()
        try:
            response = super().request(method, url, **kwargs)
        except requests.RequestException as error:
            LOGGER.warning("event=transport.failed host=%s method=%s error_type=%s duration_ms=%d",
                           urlparse(url).hostname, method,
                           type(error).__name__, round((time.monotonic() - started) * 1000))
            raise
        LOGGER.debug("event=transport.response host=%s method=%s status=%d duration_ms=%d",
                     urlparse(url).hostname, method, response.status_code,
                     round((time.monotonic() - started) * 1000))
        return response


class InteractiveLoginClient(AulaLoginClient):
    def step8_exchange_oauth_code(self, callback_url):
        parsed = urlparse(callback_url)
        states = parse_qs(parsed.query).get("state", [])
        if parsed.scheme != "https" or parsed.netloc != "app-private.aula.dk" or len(states) != 1 or not self.state or not secrets.compare_digest(states[0], self.state):
            raise OAuthError("Invalid OAuth callback origin or state.")
        return super().step8_exchange_oauth_code(callback_url)

    def log(self, message, level="INFO"):
        # Report progress through structured status, never upstream raw logs.
        return


def token_state(tokens, previous_refresh=""):
    if not isinstance(tokens, dict):
        raise AuthenticationError("Aula returned invalid token data.")
    access = tokens.get("access_token") or tokens.get("accessToken")
    refresh = tokens.get("refresh_token") or tokens.get("refreshToken") or previous_refresh
    expiry = tokens.get("expires_at") or tokens.get("expiresAt") or tokens.get("token_expires_at")
    try:
        expiry = float(expiry) if expiry is not None else time.time() + int(tokens.get("expires_in", tokens.get("expiresIn", 3600)))
    except (ValueError, TypeError):
        raise AuthenticationError("Aula returned invalid token expiry.") from None
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh or not time.time() < expiry < time.time() + 366 * 86400:
        raise AuthenticationError("Aula returned missing or expired tokens.")
    return {"access_token": access, "refresh_token": refresh, "expires_at": expiry}


class Connection:
    def __init__(self, vault, session=None):
        self.vault = vault
        self.session = session or BoundedSession()
        self.lock = threading.RLock()
        self.value = vault.read()
        if self.value:
            protect(self.value["username"], *self.value["tokens"].values())
        self.error = ""

    def install(self, username, tokens, children):
        with self.lock:
            if self.value and username == self.value["username"] and tokens == self.value["tokens"]:
                # Re-installing the stored tokens only updates the children. Discovery can be
                # served from cache, so the access token may have expired meanwhile; it is
                # refreshed on its next use instead of being rejected here.
                self.update_children(children)
                return
            protect(username, *tokens.values())
            value = {"username": username, "tokens": token_state(tokens), "children": children}
            self.vault.write(value)
            self.value = value
            self.error = ""

    def update_children(self, children):
        """Store rediscovered children without re-validating (possibly expired) access tokens."""
        with self.lock:
            if not self.value:
                raise AuthenticationError("Connect your Aula account in Account setup.")
            if children != self.value.get("children"):
                updated = {**self.value, "children": children}
                self.vault.write(updated)
                self.value = updated

    def access_token(self, force=False):
        with self.lock:
            if not self.value:
                raise AuthenticationError("Connect your Aula account in Account setup.")
            tokens = self.value["tokens"]
            if force or tokens["expires_at"] <= time.time() + 60:
                LOGGER.info("event=auth.refresh phase=start forced=%s", force)
                try:
                    response = self.session.post(TOKEN_URL, data={
                        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": CLIENT_ID,
                    }, headers={"Accept": "application/json"}, timeout=(10, 30))
                    if response.status_code in (400, 401, 403):
                        raise AuthenticationError("Aula login has expired. Reconnect with MitID in Account setup.")
                    if response.status_code != 200:
                        raise AuthenticationError(f"Aula token refresh failed (HTTP {response.status_code}). Retry later.")
                    refreshed = token_state(response.json(), tokens["refresh_token"])
                    protect(*refreshed.values())
                    updated = {**self.value, "tokens": refreshed}
                    self.vault.write(updated)
                    self.value = updated
                    self.error = ""
                    LOGGER.info("event=auth.refresh phase=complete")
                except (requests.RequestException, ValueError):
                    LOGGER.warning("event=auth.refresh phase=failed reason=transport_or_response")
                    self.error = "Aula token refresh failed. Retry or reconnect in Account setup."
                    raise AuthenticationError(self.error) from None
                except AuthenticationError as err:
                    LOGGER.warning("event=auth.refresh phase=failed reason=authentication")
                    self.error = str(err)
                    raise
            return self.value["tokens"]["access_token"]


class LoginFlow:
    def __init__(self, runtime, factory=InteractiveLoginClient):
        self.runtime = runtime
        self.factory = factory
        self.lock = threading.RLock()
        self.client = None
        self.cancelled = threading.Event()
        self.identity_ready = threading.Event()
        self.identity = None
        self.identities = []
        self.status = "idle"
        self.message = "Connect your guardian account using the MitID app."
        self.worker = None
        self.started = 0
        self.finished = 0

    @observed("auth.start")
    def start(self, username):
        if not isinstance(username, str) or not 1 <= len(username.strip()) <= 128:
            raise ValueError("Enter your MitID username (1-128 characters).")
        username = username.strip()
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("A login is already running. Cancel it or wait for completion.")
            current = self.runtime.connection.value
            if current and current["username"] != username:
                raise ValueError("Reconnect the same guardian account. Use a separate add-on data backup for a different family.")
            self.cancelled = threading.Event()
            self.identity_ready = threading.Event()
            self.identity = None
            self.identities = []
            self.client = None
            self.status = "running"
            self.message = "Starting Aula login. Approve only the request you initiated."
            self.started = time.monotonic()
            protect(username)
            context = copy_context()
            self.worker = threading.Thread(target=context.run, args=(self._run, username),
                                           daemon=True, name="mitid-login")
            self.worker.start()

    def choose_identity(self, names):
        LOGGER.info("event=auth.identity_requested choice_count=%d", len(names))
        with self.lock:
            self.identities = list(names)
            self.message = "Choose your guardian identity."
        while not self.identity_ready.wait(0.25):
            if self.cancelled.is_set() or time.monotonic() - self.started > 300:
                raise AuthenticationError("Identity selection cancelled or timed out.")
        with self.lock:
            self.identities = []
            return self.identity

    def select_identity(self, index):
        with self.lock:
            if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(self.identities):
                raise ValueError("Choose an available identity.")
            self.identity = index
            self.identity_ready.set()
            LOGGER.info("event=auth.identity_selected")

    def _run(self, username):
        LOGGER.info("event=auth.worker phase=start")
        try:
            client = self.factory(mitid_username=username, auth_method="APP", verbose=False, debug=False)
            session = BoundedSession(self.cancelled, self.started + 300)
            session.headers.update(client.session.headers)
            session.headers["Accept-Encoding"] = "gzip, deflate"
            client.session.close()
            client.session = session
            client.identity_selector = self.choose_identity
            with self.lock:
                self.client = client
            result = client.authenticate()
            if not result.get("success"):
                raise AuthenticationError("MitID approval was not completed.")
            if self.cancelled.is_set():
                raise AuthenticationError("Authentication cancelled.")
            self.runtime.connect(username, result["tokens"], self.cancelled)
            with self.lock:
                self.status = "complete"
                self.message = "Connected. Your family's feed is syncing."
                LOGGER.info("event=auth.worker phase=complete")
        except Exception as err:
            # This is the worker boundary; failures are surfaced, not swallowed.
            LOGGER.warning("MitID login failed (%s); details omitted to protect authentication data.", type(err).__name__)
            from aula_ui.aula_api import AulaApiError
            if isinstance(err, (AuthenticationError, AulaApiError)):
                message = str(err)
            elif isinstance(err, OSError) and not isinstance(err, requests.RequestException):
                message = "Could not persist the Aula login. Check the data volume's permissions and free space."
            else:
                message = (
                    "Aula/MitID login failed or expired. Check your username, approve in the MitID app, "
                    "and retry. Do not start parallel MitID logins.")
            with self.lock:
                self.status = "cancelled" if self.cancelled.is_set() else "failed"
                self.message = "Login cancelled." if self.cancelled.is_set() else message
        finally:
            with self.lock:
                if self.client:
                    self.client.session.close()
                self.client = None
                self.identities = []
                self.finished = time.monotonic()
                LOGGER.info("event=auth.worker phase=finished outcome=%s duration_ms=%d",
                            self.status, round((self.finished - self.started) * 1000))

    def cancel(self):
        if self.worker and self.worker.is_alive():
            LOGGER.info("event=auth.cancel_requested")
        self.cancelled.set()

    def snapshot(self):
        with self.lock:
            if self.status == "running" and time.monotonic() - self.started > 300:
                self.cancelled.set()
                self.message = "Login expired. Waiting for the current request to end before retry."
            result = {"status": self.status, "message": self.message, "identities": list(self.identities)}
            if not self.client or self.status != "running" or self.identities:
                return result
            mitid = self.client.get_mitid_client()
            if mitid:
                result["message"] = str(getattr(mitid, "status_message", self.message))
                codes = mitid.get_current_qr_codes()
                if codes:
                    qr = codes[int(time.time()) % len(codes)]
                    # Build SVG from the matrix; never inject upstream markup.
                    matrix = qr.get_matrix()
                    size = len(matrix)
                    squares = "".join(f'<rect x="{x}" y="{y}" width="1" height="1"/>'
                                      for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell)
                    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="100%" height="100%" fill="white"/><g fill="black">{squares}</g></svg>'
                    result["qr"] = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
            return result
