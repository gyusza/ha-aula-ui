"""Cloudflare Access JWT gate, SPA fallback and PWA asset serving."""

import base64
import json
import shutil
import time
from types import SimpleNamespace

import pytest
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aula_ui import web
from aula_ui.access import AccessVerifier

TEAM = "example.cloudflareaccess.com"
AUD = "aud-tag-123"


@pytest.fixture(autouse=True)
def fast_shutdown(monkeypatch):
    from aula_ui import web
    monkeypatch.setattr(web, "SERVE_POLL_INTERVAL", 0.01)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwk(key, kid):
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "alg": "RS256",
            "n": b64(n.n.to_bytes(256, "big")), "e": b64(n.e.to_bytes(3, "big"))}


def make_token(key, kid="k1", **overrides):
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "exp": now + 300, "nbf": now - 5,
              "email": "me@example.com"}
    claims.update(overrides)
    head = b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    body = b64(json.dumps({k: v for k, v in claims.items() if v is not None}).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def static(tmp_path, monkeypatch):
    copy = tmp_path / "static"
    shutil.copytree(web.STATIC, copy)
    (copy / "icons").mkdir(exist_ok=True)
    (copy / "icons" / "icon-192.png").write_bytes(b"\x89PNG")
    (copy / "manifest.webmanifest").write_text("{}")
    (copy / "sw.js").write_text("self.x=1")
    (copy / "secret.txt").write_text("nope")
    monkeypatch.setattr(web, "STATIC", copy)
    return copy


@pytest.fixture
def make_server(key, static):
    started = []

    def make(emails=(), team=TEAM, aud=AUD, fetched=None):
        def fetch(url):
            if fetched is not None:
                fetched.append(url)
            return {"keys": [jwk(key, "k1")]}
        verifier = AccessVerifier(team, aud, emails, fetch=fetch)
        controller = SimpleNamespace(feed_json=b'{"items":[]}', request_refresh=lambda: None)
        service = web.start_server(controller, port=0, allowed_ip="10.9.9.9", access=verifier)
        started.append(service)
        return f"http://127.0.0.1:{service.server_port}"

    yield make
    for service in started:
        service.shutdown()
        service.server_close()


def get(url, token=None, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Cf-Access-Jwt-Assertion"] = token
    return requests.get(url, headers=headers, timeout=5, **kw)


def test_valid_jwt_allowed_with_hsts(make_server, key):
    url = make_server()
    response = get(url + "/api/feed", make_token(key))
    assert response.status_code == 200
    assert "max-age" in response.headers["Strict-Transport-Security"]


def test_jwks_fetched_from_team_domain_and_cached(make_server, key):
    fetched = []
    url = make_server(fetched=fetched)
    get(url + "/api/feed", make_token(key))
    get(url + "/api/feed", make_token(key))
    assert fetched == [f"https://{TEAM}/cdn-cgi/access/certs"]


@pytest.mark.parametrize("overrides", [
    {"aud": ["other"]}, {"exp": int(time.time()) - 3600}, {"iss": "https://evil.example"},
    {"nbf": int(time.time()) + 3600}, {"exp": None},
])
def test_invalid_claims_rejected(make_server, key, overrides):
    assert get(make_server() + "/api/feed", make_token(key, **overrides)).status_code == 403


def test_bad_signature_unknown_kid_and_garbage(make_server, key):
    url = make_server()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert get(url + "/api/feed", make_token(other)).status_code == 403
    assert get(url + "/api/feed", make_token(key, kid="nope")).status_code == 403
    assert get(url + "/api/feed", "a.b.c").status_code == 403
    assert get(url + "/api/feed", "garbage").status_code == 403


def test_alg_none_rejected(make_server):
    head = b64(json.dumps({"alg": "none", "kid": "k1"}).encode())
    body = b64(json.dumps({"iss": f"https://{TEAM}", "aud": AUD, "exp": int(time.time()) + 99}).encode())
    assert get(make_server() + "/api/feed", f"{head}.{body}.").status_code == 403


def test_missing_header_and_forged_forwarding_rejected(make_server):
    url = make_server()
    assert get(url + "/api/feed").status_code == 403
    assert get(url + "/api/feed", headers={"X-Forwarded-For": "10.9.9.9",
                                           "X-Ingress-Path": "/api/hassio_ingress/x",
                                           "Host": "aula.example.com"}).status_code == 403


def test_unset_options_fail_closed(key, make_server):
    for team, aud in (("", ""), (TEAM, ""), ("", AUD)):
        assert get(make_server(team=team, aud=aud) + "/api/feed", make_token(key)).status_code == 403


def test_email_allowlist(make_server, key):
    url = make_server(emails=("ME@example.com",))
    assert get(url + "/api/feed", make_token(key)).status_code == 200
    assert get(url + "/api/feed", make_token(key, email="x@example.com")).status_code == 403
    assert get(url + "/api/feed", make_token(key, email=None)).status_code == 403


def test_post_works_with_jwt_and_still_needs_header(make_server, key):
    url = make_server()
    headers = {"Cf-Access-Jwt-Assertion": make_token(key)}
    assert requests.post(url + "/api/refresh", headers=headers, timeout=5).status_code == 403
    headers["X-Aula-Request"] = "refresh"
    assert requests.post(url + "/api/refresh", headers=headers, timeout=5).status_code == 202
    assert requests.post(url + "/api/refresh", headers={"X-Aula-Request": "refresh"},
                         timeout=5).status_code == 403


def test_jwks_refreshes_on_unknown_kid(key):
    new = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    clock = [1000.0]
    sets = [{"keys": [jwk(key, "k1")]}, {"keys": [jwk(key, "k1"), jwk(new, "k2")]}]
    verifier = AccessVerifier(TEAM, AUD, fetch=lambda url: sets.pop(0) if len(sets) > 1 else sets[0],
                              clock=lambda: clock[0])
    now = int(time.time())
    clock[0] = now
    assert verifier.verify(make_token(key))
    clock[0] = now + 60
    assert verifier.verify(make_token(new, kid="k2"))


@pytest.fixture
def ingress(static):
    controller = SimpleNamespace(feed_json=b"{}", request_refresh=lambda: None)
    service = web.start_server(controller, port=0, allowed_ip="127.0.0.1")
    yield f"http://127.0.0.1:{service.server_port}"
    service.shutdown()
    service.server_close()


def test_ingress_still_works_without_hsts_and_with_prefix(ingress):
    response = get(ingress + "/post/42", headers={"X-Ingress-Path": "/api/hassio_ingress/abc_D-1"})
    assert response.status_code == 200
    assert "Strict-Transport-Security" not in response.headers
    assert '<base href="/api/hassio_ingress/abc_D-1/">' in response.text
    bad = get(ingress + "/", headers={"X-Ingress-Path": '"><script>'})
    assert '<base href="/">' in bad.text


def test_spa_fallback_and_unchanged_routes(make_server, key, ingress):
    url = make_server()
    token = make_token(key)
    for path in ("/post/12", "/thread/9/x/y", "/child/anna", "/calendar-view", "/calendar-view/2026"):
        response = get(url + path, token)
        assert response.status_code == 200 and '<base href="/">' in response.text, path
    assert get(url + "/post", token).status_code == 404
    assert get(url + "/api/nothing", token).status_code == 404
    assert get(ingress + "/api/nothing").status_code == 404


def test_manifest_sw_and_icons(ingress):
    manifest = get(ingress + "/manifest.webmanifest")
    assert manifest.headers["Content-Type"].startswith("application/manifest+json")
    sw = get(ingress + "/sw.js")
    assert sw.headers["Service-Worker-Allowed"] == "/"
    assert sw.headers["Cache-Control"] == "no-cache"
    assert sw.headers["Content-Type"].startswith("text/javascript")
    icon = get(ingress + "/icons/icon-192.png")
    assert icon.status_code == 200 and icon.headers["Content-Type"] == "image/png"
    csp = sw.headers["Content-Security-Policy"]
    assert "manifest-src 'self'" in csp and "worker-src 'self'" in csp


def test_static_traversal_and_private_files(ingress):
    for path in ("/secret.txt", "/icons/../app.py", "/%2e%2e/icon.png", "/icons/%2e%2e/x.png",
                 "/icons/missing.png", "/.hidden.png"):
        assert get(ingress + path).status_code == 404, path


def test_whoami_reports_ha_for_ingress_and_email_for_access(make_server, key, ingress):
    assert get(ingress + "/api/whoami").json() == {"user": "HA"}
    url = make_server()
    assert get(url + "/api/whoami", make_token(key)).json()["user"]
    assert get(url + "/api/whoami").status_code == 403
