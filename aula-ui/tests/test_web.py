"""Ingress authorization, relative assets, API and refresh protection."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from aula_ui.web import start_server


@pytest.fixture(autouse=True)
def fast_shutdown(monkeypatch):
    from aula_ui import web
    monkeypatch.setattr(web, "SERVE_POLL_INTERVAL", 0.01)


@pytest.fixture
def server():
    controller = SimpleNamespace(feed_json=b'{"items":[]}', request_refresh=lambda: None)
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    yield f"http://127.0.0.1:{service.server_port}"
    service.shutdown()
    service.server_close()


def test_ingress_assets_and_feed(server):
    for path in ("/", "/app.js", "/theme.js", "/style.css", "/api/feed"):
        response = requests.get(server + path, timeout=5)
        assert response.status_code == 200
        # Shell assets are revalidated (ETag); pages and private data are never stored.
        assert response.headers["Cache-Control"] == ("no-cache" if "." in path else "no-store")
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert 'src="app.js"' in requests.get(server, timeout=5).text


def test_version_endpoint_and_page_stamp(server):
    from aula_ui import __version__

    assert requests.get(server + "/api/version", timeout=5).json() == {"version": __version__}
    assert f'<meta name="aula-version" content="{__version__}">' in requests.get(server, timeout=5).text


def test_retired_siri_endpoints_are_not_served(server):
    assert requests.get(server + "/api/siri/settings", timeout=5).status_code == 404
    response = requests.post(server + "/api/siri/command", json={"action": "recap"},
                             headers={"X-Aula-Request": "settings"}, timeout=5)
    assert response.status_code == 404


def test_refresh_requires_same_origin_header(server):
    assert requests.post(server + "/api/refresh", timeout=5).status_code == 403
    assert requests.post(server + "/api/refresh", headers={"X-Aula-Request": "refresh"},
                         timeout=5).status_code == 202


def test_photo_tag_api_validates_requests_and_surfaces_errors(server, monkeypatch):
    from aula_ui import gallery
    from aula_ui.aula_api import AulaApiError

    calls = []

    def tag(*args):
        calls.append(args[2:])
        if args[4] == "denied":
            raise PermissionError("Tagging not permitted")
        if args[4] == "missing":
            raise LookupError("Photo missing")
        if args[4] == "failed":
            raise AulaApiError("Aula rejected tag")
        return {"id": args[3], "tagged_children": [args[4]]}

    monkeypatch.setattr(gallery, "set_child_tag", tag)
    controller = SimpleNamespace(source_client=object(), config=SimpleNamespace(children=[]))
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}/api/gallery/tag"
    value = {"album": "77", "media": "1", "child": "maja", "tagged": True}
    try:
        for headers in ({}, {"X-Aula-Request": "refresh"}):
            assert requests.post(url, json=value, headers=headers, timeout=5).status_code == 403
        assert not calls
        headers = {"X-Aula-Request": "settings"}
        for invalid in ({}, {**value, "extra": True}, []):
            assert requests.post(url, json=invalid, headers=headers, timeout=5).status_code == 400
        response = requests.post(url, json=value, headers=headers, timeout=5)
        assert response.status_code == 200 and response.json()["tagged_children"] == ["maja"]
        assert calls == [("77", "1", "maja", True)]
        for child, status in (("denied", 403), ("missing", 404), ("failed", 502)):
            response = requests.post(url, json={**value, "child": child}, headers=headers, timeout=5)
            assert response.status_code == status and response.json()["error"]
    finally:
        service.shutdown()
        service.server_close()


def test_unknown_paths_never_serve_private_files(server):
    assert requests.get(server + "/state.json", timeout=5).status_code == 404
    assert requests.get(server + "/%2e%2e/config.py", timeout=5).status_code == 404


def test_direct_access_denied_even_with_forged_headers():
    service = start_server(SimpleNamespace(feed_json=b"private"), port=0)
    try:
        response = requests.get(f"http://127.0.0.1:{service.server_port}/api/feed",
                                headers={"X-Forwarded-For": "172.30.32.2"}, timeout=5)
        assert response.status_code == 403
        assert "private" not in response.text
    finally:
        service.shutdown()
        service.server_close()


def test_profiles_api_persistence_calendar_and_validation(tmp_path):
    from test_controller import FakeHA, TWO_CHILDREN, build_controller

    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    controller.config = replace(controller.config, public_url="https://aula.example.com")
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}"
    try:
        result = requests.get(url + "/api/profiles", timeout=5).json()
        assert len(result["children"]) == 2
        assert result["calendar_path"].endswith(".ics")
        assert result["calendar_url"] == "https://aula.example.com" + result["calendar_path"]
        assert requests.get(url + "/calendar.ics", timeout=5).status_code == 200
        value = {"display_name": "Jo", "institution_type": "daycare",
                 "calendar_enabled": False}
        response = requests.post(url + "/api/profiles/alma", json=value,
                                 headers={"X-Aula-Request": "settings"}, timeout=5)
        assert response.status_code == 200
        assert controller.profiles.snapshot()[0]["display_name"] == "Jo"
        assert requests.post(url + "/api/profiles/alma", json=value, timeout=5).status_code == 403
        assert requests.post(url + "/api/profiles/alma", json={"invalid": True},
                             headers={"X-Aula-Request": "settings"}, timeout=5).status_code == 400
    finally:
        service.shutdown()
        service.server_close()


def test_reprocess_api_requires_settings_header_and_known_item(tmp_path):
    from test_controller import FakeHA, build_controller

    controller = build_controller(tmp_path, FakeHA())
    controller.provider = object()
    controller.store.set("feed", [{"id": "thread:42", "thread_complete": True}])
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}/api/feed/reprocess"
    try:
        for headers in ({}, {"X-Aula-Request": "refresh"}):
            assert requests.post(url, json={"id": "thread:42"}, headers=headers, timeout=5).status_code == 403
        headers = {"X-Aula-Request": "settings"}
        for value in ({}, {"id": 42}, {"id": "missing"}, {"id": "thread:42", "extra": True}):
            assert requests.post(url, json=value, headers=headers, timeout=5).status_code == 400
        response = requests.post(url, json={"id": "thread:42"}, headers=headers, timeout=5)
        assert response.status_code == 202 and response.json() == {"queued": True}
        assert controller._refresh.is_set()
        assert controller.feed._reprocess.get_nowait() == "thread:42"
    finally:
        service.shutdown()
        service.server_close()


def test_archive_api_requires_ingress_header_and_valid_payload(tmp_path):
    from test_controller import FakeHA, TWO_CHILDREN, build_controller
    from test_calendar_feed import ITEM, EVENT
    from aula_ui.calendar_feed import CalendarFeed, event_uid, validate_events

    controller = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir()
    controller.calendar = CalendarFeed(controller, str(directory))
    event = validate_events([EVENT], ITEM)[0]
    controller.calendar.record(ITEM, [event])
    controller.calendar.review(event_uid(event), "accept")
    controller.publish_feed()
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}"
    headers = {"X-Aula-Request": "settings"}
    value = {"uid": event_uid(event), "archived": True}
    try:
        assert requests.post(url + "/api/calendar/archive", json=value, timeout=5).status_code == 403
        for invalid in [{"uid": "unknown", "archived": True},
                        {"uid": event_uid(event), "archived": "true"}, {"other": True}]:
            assert requests.post(url + "/api/calendar/archive", json=invalid,
                                 headers=headers, timeout=5).status_code == 400
        assert requests.post(url + "/api/calendar/archive", json=value,
                             headers=headers, timeout=5).status_code == 200
        result = requests.get(url + "/api/profiles", timeout=5).json()
        assert not result["events"] and len(result["archived_events"]) == 1
        assert result["calendar_count"] == 0
        assert b"BEGIN:VEVENT" not in requests.get(url + "/calendar.ics", timeout=5).content
        assert requests.post(url + "/api/calendar/archive", json={**value, "archived": False},
                             headers=headers, timeout=5).status_code == 200
        assert requests.get(url + "/api/profiles", timeout=5).json()["calendar_count"] == 1
        categories = {"excluded": ["ai:post"]}
        assert requests.post(url + "/api/calendar/categories", json=categories, timeout=5).status_code == 403
        for invalid in [{"excluded": "ai:post"}, {"excluded": ["Bad"]}, {"other": []}]:
            assert requests.post(url + "/api/calendar/categories", json=invalid,
                                 headers=headers, timeout=5).status_code == 400
        saved = requests.post(url + "/api/calendar/categories", json=categories, headers=headers, timeout=5)
        assert saved.status_code == 200 and saved.json()["calendar_count"] == 0
        assert saved.json()["calendar_categories"] == [
            {"key": "ai:post", "label": "AI events from posts", "count": 1, "excluded": True}]
        assert requests.get(url + "/api/profiles", timeout=5).json()["calendar_categories"][0]["excluded"]
        assert b"BEGIN:VEVENT" not in requests.get(url + "/calendar.ics", timeout=5).content
    finally:
        service.shutdown()
        service.server_close()


def test_recommendation_review_api_validates_and_changes_ics(tmp_path):
    from test_controller import FakeHA, TWO_CHILDREN, build_controller
    from test_calendar_feed import ITEM, EVENT
    from aula_ui.calendar_feed import CalendarFeed

    controller = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir()
    controller.calendar = CalendarFeed(controller, str(directory))
    controller.calendar.recommend(ITEM, {**EVENT, "start": ""}, "Missing date")
    uid = controller.calendar.recommendations()[0]["uid"]
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}"
    headers = {"X-Aula-Request": "settings"}
    try:
        value = {"uid": uid, "action": "accept"}
        assert requests.post(url + "/api/calendar/review", json=value, timeout=5).status_code == 403
        assert requests.post(url + "/api/calendar/review", json=value,
                             headers=headers, timeout=5).status_code == 400
        value = {"uid": uid, "action": "edit", "changes": {
            "title": "Parent corrected", "start": "2026-10-09", "end": "",
            "all_day": True, "location": "School",
        }}
        assert requests.post(url + "/api/calendar/review", json=value,
                             headers=headers, timeout=5).status_code == 200
        result = requests.get(url + "/api/profiles", timeout=5).json()
        assert not result["recommendations"]
        assert result["events"][0]["title"] == "Parent corrected"
        assert uid.encode() in requests.get(url + "/calendar.ics", timeout=5).content
        assert requests.post(url + "/api/calendar/review", json={"uid": uid, "action": "bad"},
                             headers=headers, timeout=5).status_code == 400
    finally:
        service.shutdown()
        service.server_close()


def test_calendar_subscription_needs_only_secret_token(tmp_path):
    from test_controller import FakeHA, TWO_CHILDREN, build_controller

    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    controller.calendar.content = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    # Not ingress and no Access JWT: like a phone calendar app through a Cloudflare bypass.
    service = start_server(controller, port=0, allowed_ip="203.0.113.1")
    base = f"http://127.0.0.1:{service.server_port}"
    path = controller.profiles.calendar_path
    try:
        response = requests.get(base + path, timeout=5)
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/calendar")
        assert response.content.startswith(b"BEGIN:VCALENDAR")
        head = requests.head(base + path, timeout=5)
        assert head.status_code == 200 and head.content == b""
        wrong = path.replace(path[-10:-4], "AAAAAA" if path[-10:-4] != "AAAAAA" else "BBBBBB")
        for bad in (wrong, "/api/aula_calendar/x.ics", "/api/aula_calendar/%2e%2e%2fstate.json"):
            assert requests.get(base + bad, timeout=5).status_code == 404
        for private in ("/api/feed", "/api/profiles", "/calendar.ics"):
            assert requests.get(base + private, timeout=5).status_code == 403
        assert requests.head(base + "/api/feed", timeout=5).status_code == 405
        controller.profiles.rotate_calendar_token()
        assert requests.get(base + path, timeout=5).status_code == 404
    finally:
        service.shutdown()
        service.server_close()


def test_mark_read_api_marks_opened_unread_conversation(tmp_path, monkeypatch):
    from test_controller import FakeHA, build_controller

    controller = build_controller(tmp_path, FakeHA())
    controller.store.set("feed", [{"id": "thread:42", "content_type": "Message", "unread": True},
                                  {"id": "post:1", "content_type": "Post"}])
    calls = []
    monkeypatch.setattr("aula_ui.controller.mark_thread_read",
                        lambda client, thread_id: calls.append(thread_id) or True)
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}/api/feed/read"
    headers = {"X-Aula-Request": "settings"}
    try:
        assert requests.post(url, json={"id": "thread:42"}, timeout=5).status_code == 403
        for value in ({}, {"id": 42}, {"id": "post:1"}, {"id": "thread:x"}, {"id": "thread:42", "x": 1}):
            assert requests.post(url, json=value, headers=headers, timeout=5).status_code == 400
        assert requests.post(url, json={"id": "thread:7"}, headers=headers, timeout=5).status_code == 404
        response = requests.post(url, json={"id": "thread:42"}, headers=headers, timeout=5)
        assert response.status_code == 200 and response.json() == {"read": True, "changed": True}
        assert calls == ["42"]
        assert controller.store.get("feed")[0]["unread"] is False
        assert '"unread": false' in controller.feed_json if isinstance(controller.feed_json, str) \
            else b'"unread": false' in controller.feed_json
        response = requests.post(url, json={"id": "thread:42"}, headers=headers, timeout=5)
        assert response.json() == {"read": True, "changed": False} and calls == ["42"]
    finally:
        service.shutdown()
        service.server_close()
