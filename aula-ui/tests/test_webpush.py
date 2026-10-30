"""Web Push encryption, VAPID and device store."""

import json
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aula_ui import webpush
from aula_ui.webpush import PushDevices, Vapid, b64url, encrypt, unb64url, valid_endpoint

CATEGORIES = {"post": "Posts", "message": "Messages"}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def browser_keys():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = webpush._raw_public(key.public_key())
    return key, b64url(raw), b64url(b"0123456789abcdef")


def decrypt(body: bytes, ua_key, auth: str) -> bytes:
    """The user agent side of RFC 8291."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public = body[21:21 + idlen]
    assert rs == 4096 and idlen == 65
    ua_public = webpush._raw_public(ua_key.public_key())
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = webpush._hkdf(unb64url(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_rfc8291_payload_round_trips():
    ua_key, p256dh, auth = browser_keys()
    body = encrypt("Hej – ny besked".encode(), p256dh, auth)
    assert decrypt(body, ua_key, auth) == "Hej – ny besked".encode()
    with pytest.raises(ValueError):
        encrypt(b"x", p256dh, b64url(b"short"))
    with pytest.raises(ValueError, match="too large"):
        encrypt(b"x" * 5000, p256dh, auth)


def test_vapid_key_persists_and_token_verifies(tmp_path):
    vapid = Vapid(str(tmp_path), "https://aula.example.com")
    header = vapid.authorization(ENDPOINT, now=1000)
    assert (tmp_path / "webpush_vapid.pem").stat().st_mode & 0o777 == 0o600
    assert Vapid(str(tmp_path)).public_key() == vapid.public_key()
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert key == vapid.public_key() and len(unb64url(key)) == 65
    head, claims, signature = token.split(".")
    assert json.loads(unb64url(claims)) == {
        "aud": "https://fcm.googleapis.com", "exp": 1000 + 12 * 3600, "sub": "https://aula.example.com"}
    raw = unb64url(signature)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(key))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(InvalidSignature):
        public.verify(encode_dss_signature(1, 1), b"x", ec.ECDSA(hashes.SHA256()))
    assert Vapid(str(tmp_path), "http://insecure").subject.startswith("mailto:")


@pytest.mark.parametrize("endpoint,ok", [
    (ENDPOINT, True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
    ("https://web.push.apple.com/QK", True),
    ("https://wns2-db5p.notify.windows.com/w/?token=x", True),
    ("http://fcm.googleapis.com/x", False),
    ("https://fcm.googleapis.com:8443/x", False),
    ("https://evilfcm.googleapis.com.attacker.net/x", False),
    ("https://192.168.1.252/api", False),
    (None, False),
])
def test_only_known_push_services_are_accepted(endpoint, ok):
    assert valid_endpoint(endpoint) is ok


class Store:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass


class Session:
    def __init__(self, status=201):
        self.status = status
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=self.status)


def devices(tmp_path):
    controller = SimpleNamespace(
        config=SimpleNamespace(data_dir=str(tmp_path), public_url="https://aula.example.com"),
        store=Store(),
        profiles=SimpleNamespace(snapshot=lambda: [{"slug": "alma"}, {"slug": "emil"}]))
    return PushDevices(controller, CATEGORIES, ["post"])


def test_subscribe_update_send_and_remove(tmp_path):
    push = devices(tmp_path)
    ua_key, p256dh, auth = browser_keys()
    device = push.subscribe({"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": p256dh, "auth": auth}},
                             "name": "  Lars's   iPhone ", "children": ["emil"]})
    assert device["name"] == "Lars's iPhone" and device["categories"] == ["post"]
    assert "endpoint" not in device and "keys" not in device
    again = push.subscribe({"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": p256dh, "auth": auth}}})
    assert len(push.all()) == 1 and again["created"] == device["created"]
    updated = push.update(device["id"], {"categories": ["message"], "children": []})
    assert updated["categories"] == ["message"]
    assert push.wants(push.all()[0], {"child_slugs": ["emil"]}, "message")
    assert not push.wants(push.all()[0], {"child_slugs": ["emil"]}, "post")
    with pytest.raises(ValueError):
        push.update(device["id"], {"categories": ["nope"], "children": []})
    with pytest.raises(ValueError):
        push.update(device["id"], {"categories": [], "children": ["stranger"]})
    with pytest.raises(LookupError):
        push.update("0" * 16, {"categories": [], "children": []})

    push.session = Session()
    assert push.send(push.all()[0], {"title": "Hi", "message": "There", "tag": "aula-post:1",
                                     "url": "https://aula.example.com/post/1"}) == ""
    url, kwargs = push.session.calls[0]
    assert url == ENDPOINT and kwargs["headers"]["Content-Encoding"] == "aes128gcm"
    assert "Topic" not in kwargs["headers"] and not kwargs["allow_redirects"]
    assert json.loads(decrypt(kwargs["data"], ua_key, auth)) == {
        "title": "Hi", "message": "There", "tag": "aula-post:1", "url": "https://aula.example.com/post/1"}
    assert push.all()[0]["last_sent"]

    push.session = Session(500)
    assert "HTTP 500" in push.send(push.all()[0], {"title": "x", "message": "y"})
    assert "HTTP 500" in push.all()[0]["last_error"]
    push.session = Session(410)
    assert "expired" in push.send(push.all()[0], {"title": "x", "message": "y"})
    assert push.all() == []
    assert not push.remove(device["id"])


@pytest.mark.parametrize("value", [
    None, {}, {"subscription": {"endpoint": "https://attacker.net/x", "keys": {}}},
    {"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": "AAAA", "auth": "AAAA"}}},
    {"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": b64url(b"\x04" + b"\x01" * 64),
                                                      "auth": b64url(b"0" * 16)}}},
])
def test_invalid_subscriptions_are_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        devices(tmp_path).subscribe(value)


def test_device_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(webpush, "MAX_DEVICES", 2)
    push = devices(tmp_path)
    for index in range(2):
        _, p256dh, auth = browser_keys()
        push.subscribe({"subscription": {"endpoint": f"{ENDPOINT}{index}", "keys": {"p256dh": p256dh, "auth": auth}}})
    _, p256dh, auth = browser_keys()
    with pytest.raises(ValueError, match="At most"):
        push.subscribe({"subscription": {"endpoint": ENDPOINT + "x", "keys": {"p256dh": p256dh, "auth": auth}}})
