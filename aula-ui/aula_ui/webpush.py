"""Standard Web Push (RFC 8030/8291/8292) delivery to installed PWA devices.

Keys and subscriptions stay local: a VAPID key pair is generated on first use
and stored in the add-on data directory; payloads are end-to-end encrypted to
each browser so the push service only sees ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .diagnostics import describe

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "push_devices"
MAX_DEVICES = 20
RECORD_SIZE = 4096
TTL_SECONDS = 24 * 3600
# Only real browser push services; anything else would let a client make the
# add-on POST to arbitrary hosts.
PUSH_HOSTS = (
    "fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com",
    "push.apple.com", "notify.windows.com",
)
DEVICE_ID = re.compile(r"^[a-f0-9]{16}$")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            private_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """aes128gcm content coding per RFC 8291 (single record)."""
    ua_public = unb64url(p256dh)
    auth_secret = unb64url(auth)
    if len(ua_public) != 65 or len(auth_secret) != 16:
        raise ValueError("Invalid push subscription keys")
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("Push payload too large")
    salt = salt or os.urandom(16)
    local = private_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(local.public_key())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = local.exchange(ec.ECDH(), peer)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + body


def valid_endpoint(endpoint: Any) -> bool:
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        return False
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.port and any(
        host == allowed or host.endswith("." + allowed) for allowed in PUSH_HOSTS)


def device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


class Vapid:
    """Persistent VAPID identity (application server key)."""

    def __init__(self, data_dir: str, subject: str = "") -> None:
        self.path = Path(data_dir) / "webpush_vapid.pem"
        self.subject = subject if re.match(r"^(https://|mailto:)", subject or "") else \
            "mailto:aula-ui@users.noreply.github.com"
        self._key: ec.EllipticCurvePrivateKey | None = None
        self._lock = threading.Lock()

    def key(self) -> ec.EllipticCurvePrivateKey:
        with self._lock:
            if self._key is None:
                if self.path.exists():
                    loaded = serialization.load_pem_private_key(self.path.read_bytes(), password=None)
                    if not isinstance(loaded, ec.EllipticCurvePrivateKey):
                        raise ValueError("Stored VAPID key is not an EC key")
                    self._key = loaded
                else:
                    self._key = ec.generate_private_key(ec.SECP256R1())
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.path.with_suffix(".tmp")
                    tmp.unlink(missing_ok=True)
                    # Create owner-only from the start; never briefly world-readable.
                    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                        handle.write(self._key.private_bytes(
                            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()))
                    os.chmod(tmp, 0o600)
                    tmp.replace(self.path)
                    _LOGGER.info("event=webpush.vapid_created")
            return self._key

    def public_key(self) -> str:
        return b64url(_raw_public(self.key().public_key()))

    def authorization(self, endpoint: str, now: float | None = None) -> str:
        parts = urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}",
                  "exp": int((now or time.time()) + 12 * 3600), "sub": self.subject}
        signing = (b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
                   + "." + b64url(json.dumps(claims, separators=(",", ":")).encode()))
        r, s = decode_dss_signature(self.key().sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        token = signing + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={token}, k={self.public_key()}"


class PushDevices:
    """Subscribed devices, each with its own category/child filters."""

    def __init__(self, controller: Any, categories: dict[str, str], default_categories: list[str]) -> None:
        self.controller = controller
        self.categories = categories
        self.default_categories = default_categories
        self.vapid = Vapid(controller.config.data_dir, getattr(controller.config, "public_url", ""))
        self.lock = threading.RLock()
        self.session = requests.Session()

    def all(self) -> list[dict[str, Any]]:
        devices = self.controller.store.get(STORE_KEY) or []
        return [d for d in devices if isinstance(d, dict) and d.get("id") and d.get("endpoint")]

    def public(self, device: dict[str, Any]) -> dict[str, Any]:
        return {key: device.get(key) for key in (
            "id", "name", "categories", "children", "created", "last_sent", "last_error")}

    def _save(self, devices: list[dict[str, Any]]) -> None:
        self.controller.store.set(STORE_KEY, devices)
        self.controller.store.save()

    def _filters(self, value: dict[str, Any]) -> tuple[list[str], list[str]]:
        categories, children = value.get("categories"), value.get("children")
        for name, values in (("categories", categories), ("children", children)):
            if not isinstance(values, list) or len(values) > 20 or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{name} must be a list of text values")
        if not set(categories) <= set(self.categories):
            raise ValueError("Unknown notification category")
        slugs = {p["slug"] for p in self.controller.profiles.snapshot()}
        if not set(children) <= slugs:
            raise ValueError("Unknown child")
        return list(dict.fromkeys(categories)), list(dict.fromkeys(children))

    def subscribe(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("subscription"), dict):
            raise ValueError("Supply a push subscription")
        sub = value["subscription"]
        endpoint, keys = sub.get("endpoint"), sub.get("keys") or {}
        if not valid_endpoint(endpoint):
            raise ValueError("Unsupported push service")
        p256dh, auth = keys.get("p256dh"), keys.get("auth")
        try:
            if len(unb64url(str(p256dh))) != 65 or len(unb64url(str(auth))) != 16:
                raise ValueError
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(str(p256dh)))
        except (ValueError, TypeError):
            raise ValueError("Invalid push subscription keys") from None
        name = re.sub(r"\s+", " ", str(value.get("name") or "This device")).strip()[:60] or "This device"
        categories, children = self._filters({
            "categories": value.get("categories", self.default_categories),
            "children": value.get("children", [])})
        ident = device_id(endpoint)
        with self.lock:
            devices = [d for d in self.all() if d["id"] != ident]
            previous = next((d for d in self.all() if d["id"] == ident), {})
            if len(devices) >= MAX_DEVICES:
                raise ValueError(f"At most {MAX_DEVICES} devices can receive notifications")
            device = {
                "id": ident, "endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth},
                "name": name, "categories": categories, "children": children,
                "created": previous.get("created") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_sent": previous.get("last_sent"), "last_error": "",
            }
            self._save(devices + [device])
        _LOGGER.info("event=webpush.subscribed device=%s host=%s categories=%s", ident,
                     urlsplit(endpoint).hostname, ",".join(categories) or "none")
        return self.public(device)

    def update(self, ident: Any, value: Any) -> dict[str, Any]:
        if not isinstance(ident, str) or not DEVICE_ID.match(ident) or not isinstance(value, dict):
            raise ValueError("Unknown device")
        categories, children = self._filters(value)
        with self.lock:
            devices = self.all()
            device = next((d for d in devices if d["id"] == ident), None)
            if device is None:
                raise LookupError("Unknown device")
            device.update(categories=categories, children=children)
            if isinstance(value.get("name"), str) and value["name"].strip():
                device["name"] = re.sub(r"\s+", " ", value["name"]).strip()[:60]
            self._save(devices)
        return self.public(device)

    def remove(self, ident: Any) -> bool:
        with self.lock:
            devices = self.all()
            kept = [d for d in devices if d["id"] != ident]
            if len(kept) != len(devices):
                self._save(kept)
                _LOGGER.info("event=webpush.unsubscribed device=%s", ident)
                return True
        return False

    def wants(self, device: dict[str, Any], item: dict[str, Any], category: str) -> bool:
        children = device.get("children") or []
        return category in (device.get("categories") or []) and (
            not children or not item.get("child_slugs") or bool(set(item["child_slugs"]) & set(children)))

    def send(self, device: dict[str, Any], message: dict[str, Any]) -> str:
        """Deliver one message; returns an error string, or "" on success."""
        payload = json.dumps({k: message[k] for k in ("title", "message", "tag", "url") if message.get(k)},
                             ensure_ascii=False).encode()
        error = ""
        gone = False
        try:
            body = encrypt(payload, device["keys"]["p256dh"], device["keys"]["auth"])
            response = self.session.post(device["endpoint"], data=body, timeout=(10, 20), headers={
                "Authorization": self.vapid.authorization(device["endpoint"]),
                "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                "TTL": str(TTL_SECONDS), "Urgency": "normal",
            }, allow_redirects=False)
            if response.status_code in (404, 410):
                gone = True
                error = "Subscription expired; open the app on this device to re-enable notifications."
            elif response.status_code >= 400:
                error = f"Push service refused the notification (HTTP {response.status_code})."
                _LOGGER.warning("event=webpush.rejected host=%s status=%d reason=%s",
                                urlsplit(device["endpoint"]).hostname, response.status_code,
                                re.sub(r"\s+", " ", getattr(response, "text", "") or "")[:200])
        except (requests.RequestException, ValueError) as err:
            error = f"Push delivery failed: {describe(err)}"
        with self.lock:
            devices = self.all()
            if gone:
                devices = [d for d in devices if d["id"] != device["id"]]
            for stored in devices:
                if stored["id"] == device["id"]:
                    stored["last_error"] = error
                    if not error:
                        stored["last_sent"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save(devices)
        if error:
            _LOGGER.warning("event=webpush.failed device=%s gone=%s error=%s", device["id"], gone, error)
        else:
            _LOGGER.info("event=webpush.sent device=%s title=%r", device["id"], message.get("title"))
        return error
