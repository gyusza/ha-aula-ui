"""Cloudflare Access JWT verification for requests arriving via the tunnel."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Any, Callable

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_LOGGER = logging.getLogger(__name__)
LEEWAY = 60
JWKS_TTL = 3600
JWKS_MIN_REFRESH = 30


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _fetch_jwks(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


class AccessVerifier:
    """Verifies RS256 Cloudflare Access assertions; fails closed."""

    def __init__(self, team_domain: str, aud: str, allowed_emails: tuple[str, ...] = (),
                 fetch: Callable[[str], dict[str, Any]] = _fetch_jwks,
                 clock: Callable[[], float] = time.time) -> None:
        self.team_domain = team_domain.strip().lower().removeprefix("https://").rstrip("/")
        self.aud = aud.strip()
        self.allowed_emails = frozenset(e.strip().lower() for e in allowed_emails if e.strip())
        self._fetch = fetch
        self._clock = clock
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched = 0.0
        self._attempted = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.team_domain and self.aud)

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    def _refresh(self, force: bool) -> None:
        now = self._clock()
        age = now - self._fetched
        if age < JWKS_MIN_REFRESH and self._fetched:
            return
        if not force and self._fetched and age < JWKS_TTL:
            return
        # Failed fetches are throttled too: otherwise every request (or every
        # forged kid) would block all others on a fresh 10 s fetch under the lock.
        if self._attempted and now - self._attempted < JWKS_MIN_REFRESH:
            return
        self._attempted = now
        data = self._fetch(f"{self.issuer}/cdn-cgi/access/certs")
        keys: dict[str, rsa.RSAPublicKey] = {}
        for jwk in data.get("keys", []):
            if jwk.get("kty") != "RSA" or not jwk.get("kid"):
                continue
            numbers = rsa.RSAPublicNumbers(int.from_bytes(_b64(jwk["e"]), "big"),
                                           int.from_bytes(_b64(jwk["n"]), "big"))
            keys[jwk["kid"]] = numbers.public_key()
        self._keys = keys
        self._fetched = now

    def _key(self, kid: str) -> rsa.RSAPublicKey | None:
        with self._lock:
            try:
                self._refresh(force=False)
                if kid not in self._keys:
                    self._refresh(force=True)
            except (requests.RequestException, ValueError, KeyError, TypeError) as err:
                _LOGGER.warning("event=access.jwks_failed error=%s", type(err).__name__)
            return self._keys.get(kid)

    def verify(self, token: str | None) -> dict[str, Any] | None:
        """Return claims for a valid assertion, otherwise None."""
        if not self.enabled or not token:
            return None
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
            header = json.loads(_b64(header_b64))
            claims = json.loads(_b64(payload_b64))
            signature = _b64(sig_b64)
            if header.get("alg") != "RS256" or not isinstance(claims, dict):
                return None
            key = self._key(str(header.get("kid", "")))
            if key is None:
                return None
            key.verify(signature, f"{header_b64}.{payload_b64}".encode(),
                       padding.PKCS1v15(), hashes.SHA256())
            now = self._clock()
            if claims.get("iss") != self.issuer:
                return None
            aud = claims.get("aud")
            if self.aud not in (aud if isinstance(aud, list) else [aud]):
                return None
            exp = claims.get("exp")
            if not isinstance(exp, (int, float)) or now > exp + LEEWAY:
                return None
            nbf = claims.get("nbf")
            if nbf is not None and (not isinstance(nbf, (int, float)) or now < nbf - LEEWAY):
                return None
            if self.allowed_emails:
                email = claims.get("email")
                if not isinstance(email, str) or email.lower() not in self.allowed_emails:
                    return None
            return claims
        except (ValueError, TypeError, AttributeError, InvalidSignature, RecursionError):
            return None
