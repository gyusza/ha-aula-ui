"""Audit regressions: response hardening, revalidation, timeouts, JWKS throttling, workers."""

import base64
import os
import queue
import socket
import stat
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from aula_ui import web
from aula_ui.access import AccessVerifier
from aula_ui.webpush import Vapid


@pytest.fixture(autouse=True)
def fast_shutdown(monkeypatch):
    monkeypatch.setattr(web, "SERVE_POLL_INTERVAL", 0.01)


def serve(controller, allowed_ip="127.0.0.1", access=None):
    service = web.start_server(controller, port=0, allowed_ip=allowed_ip, access=access)
    return service, f"http://127.0.0.1:{service.server_port}"


@pytest.fixture
def ingress():
    controller = SimpleNamespace(feed_json=b'{"items":[]}', request_refresh=lambda: None,
                                 store={"feed": [{"title": "legacy entry without id"}]})
    service, url = serve(controller)
    yield url
    service.shutdown()
    service.server_close()


def test_every_response_has_security_headers_and_hides_python_version(ingress):
    denied_service, denied = serve(SimpleNamespace(feed_json=b"private"), allowed_ip="192.0.2.1")
    try:
        responses = [requests.get(ingress + "/missing", timeout=5),
                     requests.post(ingress + "/api/refresh", timeout=5),
                     requests.get(denied + "/api/feed", timeout=5),
                     requests.get(ingress + "/", timeout=5)]
    finally:
        denied_service.shutdown()
        denied_service.server_close()
    assert [r.status_code for r in responses] == [404, 403, 403, 200]
    for response in responses:
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
        assert "Python" not in response.headers["Server"]
        assert response.headers["Content-Security-Policy"].count("default-src") == 1
    assert "Strict-Transport-Security" in responses[2].headers
    assert "Strict-Transport-Security" not in responses[0].headers


def test_shell_assets_revalidate_with_etag_but_private_data_is_never_stored(ingress):
    first = requests.get(ingress + "/app.js", timeout=5)
    assert first.status_code == 200 and first.headers["Cache-Control"] == "no-cache"
    etag = first.headers["ETag"]
    again = requests.get(ingress + "/app.js", headers={"If-None-Match": etag}, timeout=5)
    assert again.status_code == 304 and again.content == b"" and again.headers["ETag"] == etag
    weak = requests.get(ingress + "/app.js", headers={"If-None-Match": f'"x", W/{etag}'}, timeout=5)
    assert weak.status_code == 304
    assert requests.get(ingress + "/app.js", headers={"If-None-Match": '"stale"'}, timeout=5).status_code == 200
    for path in ("/", "/api/feed", "/account"):
        response = requests.get(ingress + path, headers={"If-None-Match": "*"}, timeout=5)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store", path
        assert "ETag" not in response.headers


def test_feed_media_tolerates_entries_without_id(ingress):
    response = requests.get(ingress + "/api/feed/media?id=post:1", timeout=5)
    assert response.status_code == 404


def test_half_sent_and_idle_requests_time_out(monkeypatch):
    monkeypatch.setattr(web, "REQUEST_TIMEOUT", 0.3)
    service, _ = serve(SimpleNamespace(feed_json=b"{}", request_refresh=lambda: None))
    try:
        for payload in (b"", b"POST /api/feed/read HTTP/1.1\r\nHost: x\r\nX-Aula-Request: settings\r\n"
                             b"Content-Length: 100\r\n\r\n{\"id\""):
            with socket.create_connection(("127.0.0.1", service.server_port), timeout=5) as client:
                client.sendall(payload)
                started = time.monotonic()
                assert client.recv(1024) == b""
                assert time.monotonic() - started < 4
    finally:
        service.shutdown()
        service.server_close()


def test_failed_jwks_fetch_is_throttled_and_nested_tokens_rejected():
    calls = []
    clock = [1000.0]

    def fetch(url):
        calls.append(url)
        raise requests.ConnectionError("down")

    verifier = AccessVerifier("team.cloudflareaccess.com", "aud", fetch=fetch, clock=lambda: clock[0])
    token = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.e30.c2ln"
    for _ in range(5):
        assert verifier.verify(token) is None
    assert len(calls) == 1
    clock[0] += 31
    assert verifier.verify(token) is None and len(calls) == 2
    nested = base64.urlsafe_b64encode(b"[" * 50000).rstrip(b"=").decode()
    assert verifier.verify(f"{nested}.e30.c2ln") is None


def test_notification_job_queued_while_worker_times_out_is_still_delivered(tmp_path):
    from aula_ui.notifications import Notifier
    from test_controller import FakeHA, build_controller

    notifier = Notifier(build_controller(tmp_path, FakeHA()))
    ran = []

    class RacyQueue(queue.Queue):
        raced = False

        def get(self, block=True, timeout=None):
            if not self.raced:
                self.raced = True
                self.put_nowait(lambda: ran.append(True))
                raise queue.Empty
            return super().get(block, timeout=0.01)

    notifier._queue = RacyQueue()
    notifier._worker = threading.current_thread()
    notifier._drain()
    assert ran == [True] and notifier._worker is None


def test_pending_items_that_leave_the_feed_are_forgotten(tmp_path):
    from aula_ui.notifications import Notifier
    from test_controller import FakeHA, build_controller

    notifier = Notifier(build_controller(tmp_path, FakeHA()))
    notifier.observe([{"id": "post:old", "content_type": "Post"}])
    notifier.push.wants = lambda device, item, category: True
    notifier.push.all = lambda: [{"id": "d1", "endpoint": "https://fcm.googleapis.com/x"}]
    notifier.observe([{"id": "post:new", "content_type": "Post", "translation_pending": True}])
    assert "post:new" in notifier.first_seen
    notifier.observe([{"id": "post:other", "content_type": "Post", "translation_pending": True}])
    assert set(notifier.first_seen) == {"post:other"}


def test_vapid_key_is_owner_only_even_with_permissive_umask_and_stale_tmp(tmp_path):
    (tmp_path / "webpush_vapid.tmp").write_text("stale")
    previous = os.umask(0o022)
    try:
        Vapid(str(tmp_path)).key()
    finally:
        os.umask(previous)
    mode = stat.S_IMODE((tmp_path / "webpush_vapid.pem").stat().st_mode)
    assert mode == 0o600
    assert not (tmp_path / "webpush_vapid.tmp").exists()
    assert Vapid(str(tmp_path)).public_key()
