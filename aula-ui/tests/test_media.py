"""Attachment detection, safe image links and fresh post details."""

from types import SimpleNamespace
from unittest.mock import Mock

import requests
import pytest

from aula_ui.media import attachments, image_url, download_attachment
from aula_ui.messages import normalise_message
from aula_ui.web import start_server
from test_controller import FakeHA, build_controller


def test_media_and_inline_images_survive_normalisation():
    result = normalise_message({
        "id": 1, "text": '<img src="https://files.aula.dk/photo.png" alt="Trip">',
        "attachments": [{"media": {"mediaType": "image", "file": {
            "name": "Timetable.png", "url": "https://files.aula.dk/timetable.png",
        }}}],
    })
    assert result["original_text"] == ""
    assert len(result["attachments"]) == 2
    assert all(entry["kind"] == "image" for entry in result["attachments"])
    assert result["attachments"][1]["name"] == "Timetable.png"


def test_unavailable_and_unknown_attachments_stay_visible():
    assert attachments({"hasAttachments": True})[0]["kind"] == "unknown"
    entries = attachments({"attachments": [
        {"media": {"mediaType": "image", "title": "Photo"}},
        {"file": {"name": "Plan.pdf", "url": "https://files.aula.dk/plan.pdf"}},
        {"media": "bad shape", "file": []},
    ]})
    assert entries[0] == {"name": "Photo", "kind": "image", "url": ""}
    assert entries[1]["kind"] == "file"
    for value in ("javascript:alert(1)", "http://files.aula.dk/photo.jpg", "//evil.test/a",
                  "https://user:pass@example.test/a", "https://[invalid/a", None):
        assert image_url(value) == ""


def test_fresh_post_attachments_and_errors_are_ingress_only(tmp_path):
    class Source:
        fail = False

        def call_service(self, domain, service, data, **kwargs):
            from aula_ui.aula_api import AulaApiError
            assert data["uri"] == "?method=posts.getById&id=42"
            if self.fail:
                raise AulaApiError("Unavailable")
            return {"service_response": {"data": {"attachments": [{
                "media": {"mediaType": "image", "file": {
                    "name": "Photo", "url": "https://files.aula.dk/photo.jpg",
                }},
            }]}}}

    source = Source()
    controller = SimpleNamespace(store=SimpleNamespace(get=lambda key, default: [
        {"id": "post:42", "attachments": [{"name": "Known image", "kind": "image", "url": ""}]},
    ]), source_client=source)
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/api/feed/media"
    try:
        response = requests.get(url, params={"id": "post:42"}, timeout=5)
        assert response.status_code == 200
        assert response.json()["attachments"][0]["url"].startswith("https://")
        assert requests.get(url, params={"id": "post:99"}, timeout=5).status_code == 404
        source.fail = True
        response = requests.get(url, params={"id": "post:42"}, timeout=5)
        assert response.status_code == 502
        assert response.json()["attachments"][0]["name"] == "Known image"
        assert response.json()["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_source_details_include_pending_calendar_recommendations(tmp_path):
    controller = build_controller(tmp_path, FakeHA())
    item = {"id": "post:1", "title": "Bike ride", "date": "2026-10-23",
            "original_text": "Bakkeskoven bicycle trip on Friday morning", "child_slugs": [], "content_type": "Post"}
    controller.store.set("feed", [item])
    controller.calendar.recommend(item, {"title": "Bike ride", "evidence": "Bakkeskoven bicycle trip on Friday"},
                                  ValueError("Date is missing"))
    controller.publish_feed()
    import json
    result = json.loads(controller.feed_json)["items"]
    source = next(entry for entry in result if entry["id"] == item["id"])
    assert source["recognized_events"][0]["review_required"]
    assert source["recognized_events"][0]["uid"]


def test_nested_files_media_types_and_protected_files():
    entries = attachments({"attachments": [
        {"file": {"file": {"name": "Guide.pdf", "url": "https://media-prod.aula.dk/guide"}}},
        {"media": {"mediaType": "Video", "file": {"name": "Trip.mp4", "url": "https://media-prod.aula.dk/video"}}},
        {"media": {"mediaType": "Audio", "file": {"name": "Song.mp3", "url": "https://media-prod.aula.dk/audio"}}},
        {"file": {"name": "Blocked.pdf", "url": "https://media-prod.aula.dk/no", "scanningStatus": "Blocked"}},
        {"file": {"name": "Scan.pdf", "url": "https://media-prod.aula.dk/no", "scanningStatus": "Processing"}},
        {"document": {"id": 123}},
    ]})
    assert [entry["kind"] for entry in entries[:3]] == ["file", "video", "audio"]
    assert entries[0]["name"] == "Guide.pdf" and entries[0]["url"]
    assert all(not entry["url"] and entry["unavailable_reason"] for entry in entries[3:])


def media_response(data=b"%PDF-1.4\nPDF fixture", content_type="application/pdf", status=200):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status_code = status
    response.headers = {"Content-Type": content_type}
    response.iter_content.return_value = [data]
    return response


def test_download_bounded_no_redirects_no_credentials_and_sniffed_type(monkeypatch):
    get = Mock(return_value=media_response())
    monkeypatch.setattr("aula_ui.media.requests.get", get)
    attachment = {"url": "https://media-prod.aula.dk/private-signed.pdf"}
    data, content_type = download_attachment(attachment)
    assert data.startswith(b"%PDF-") and content_type == "application/pdf"
    get.assert_called_once_with(attachment["url"], timeout=(10, 30), stream=True, allow_redirects=False)
    get.return_value = media_response(b"<script>alert(1)</script>", "text/html")
    assert download_attachment(attachment)[1] == "application/octet-stream"
    get.return_value = media_response(status=302)
    with pytest.raises(ValueError):
        download_attachment(attachment)
    monkeypatch.setattr("aula_ui.media.MAX_ATTACHMENT_BYTES", 3)
    get.return_value = media_response()
    with pytest.raises(ValueError):
        download_attachment(attachment)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/a", "https://evil.example/a", "http://media-prod.aula.dk/a",
    "https://media-prod.aula.dk.evil.example/a", "https://user@media-prod.aula.dk/a",
    "https://media-prod.aula.dk:444/a",
])
def test_file_proxy_never_fetches_untrusted_hosts(monkeypatch, url):
    get = Mock()
    monkeypatch.setattr("aula_ui.media.requests.get", get)
    with pytest.raises(ValueError):
        download_attachment({"url": url})
    get.assert_not_called()


def test_attachment_endpoint_refreshes_source_and_serves_inline_and_download(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    controller.store.set("feed", [{"id": "post:42"}])
    controller.source_client = SimpleNamespace(call_service=Mock(return_value={
        "service_response": {"data": {"attachments": [
            {"file": {"name": 'Guide "notes".pdf', "url": "https://media-prod.aula.dk/new"}}]}}}))
    get = Mock(return_value=media_response())
    monkeypatch.setattr("aula_ui.media.requests.get", get)
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/api/feed/attachment"
    # Keep the HTTP test client separate from the mocked CDN transport.
    client = requests.Session()
    try:
        response = client.get(url, params={"id": "post:42", "index": "0"}, timeout=5)
        assert response.status_code == 200 and response.content.startswith(b"%PDF-")
        assert response.headers["Content-Type"] == "application/pdf"
        assert response.headers["Content-Disposition"].startswith("inline; filename*=")
        assert response.headers["Cache-Control"] == "no-store"
        assert "blob:" in response.headers["Content-Security-Policy"]
        response = client.get(url, params={"id": "post:42", "index": "0", "download": "1"}, timeout=5)
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert client.get(url, params={"id": "missing", "index": "0"}, timeout=5).status_code == 404
        before = get.call_count
        assert client.get(url, params={"id": "post:42", "index": "-1"}, timeout=5).status_code == 502
        assert get.call_count == before
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        controller.close()


def test_attachment_endpoint_requires_app_authentication(tmp_path):
    controller = build_controller(tmp_path, FakeHA())
    server = start_server(controller, port=0, allowed_ip="192.0.2.1")
    try:
        response = requests.get(f"http://127.0.0.1:{server.server_port}/api/feed/attachment?id=post:42&index=0", timeout=5)
        assert response.status_code == 403
    finally:
        server.shutdown()
        server.server_close()
        controller.close()


def test_thread_file_uses_message_identity_and_refreshed_metadata(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA())
    controller.store.set("feed", [{"id": "thread:4", "thread_messages": [
        {"id": "7", "attachments": [{"name": "Old.pdf", "kind": "file", "url": ""}]}]}])
    refresh = Mock(return_value=[{"id": "7", "attachments": [
        {"name": "New.pdf", "kind": "file", "url": "https://media-prod.aula.dk/new"}]}])
    monkeypatch.setattr("aula_ui.web.fetch_thread_messages", refresh)
    monkeypatch.setattr("aula_ui.media.requests.get", Mock(return_value=media_response()))
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    client = requests.Session()
    url = f"http://127.0.0.1:{server.server_port}/api/feed/attachment"
    try:
        response = client.get(url, params={"id": "thread:4", "message": "7", "index": "0"}, timeout=5)
        assert response.status_code == 200 and response.content.startswith(b"%PDF-")
        refresh.assert_called_once_with(controller.source_client, "4")
        assert client.get(url, params={"id": "thread:4", "message": "99", "index": "0"}, timeout=5).status_code == 502
        assert refresh.call_count == 1
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        controller.close()
