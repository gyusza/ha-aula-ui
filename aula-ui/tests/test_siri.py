import json
import pytest
import requests

from aula_ui.siri import SiriConnector
from aula_ui.web import start_server
from test_controller import FakeHA, TWO_CHILDREN, build_controller


@pytest.fixture(autouse=True)
def fast_shutdown(monkeypatch):
    from aula_ui import web
    monkeypatch.setattr(web, "SERVE_POLL_INTERVAL", 0.01)


@pytest.fixture
def controller(tmp_path):
    return build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)


class Sick:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"date": "2026-10-29", "valid_until": "2026-10-30T00:00:00+01:00", "notice": "SFO only.",
                "children": [{"slug": "alma", "name": "Alma", "sick": False, "can_report": True}]}

    def report(self, child, day):
        self.calls.append((child, day))
        if day != "2026-10-29":
            raise ValueError("The date has changed")
        return self.snapshot()


def test_key_is_stored_hashed_and_rotation_revocation_are_persistent(controller):
    profiles = controller.profiles
    assert not profiles.siri_enabled() and not profiles.verify_siri_key("")
    key = profiles.set_siri_key(True)
    assert len(key) == 43 and profiles.verify_siri_key(key)
    assert key not in profiles.path.read_text()
    assert profiles.siri_enabled()
    assert not profiles.verify_siri_key(key + "x")
    assert not profiles.verify_siri_key(123)
    second = profiles.set_siri_key(True)
    assert not profiles.verify_siri_key(key) and profiles.verify_siri_key(second)
    from aula_ui.profiles import Profiles
    restored = Profiles(controller.config)
    assert restored.verify_siri_key(second)
    restored.set_siri_key(False)
    assert not restored.verify_siri_key(second) and not restored.siri_enabled()


def test_failed_key_save_does_not_replace_working_key(controller, monkeypatch):
    key = controller.profiles.set_siri_key(True)

    def fail():
        raise OSError("Disk full")

    monkeypatch.setattr(controller.profiles, "save", fail)
    with pytest.raises(OSError):
        controller.profiles.set_siri_key(True)
    assert controller.profiles.verify_siri_key(key)


def test_recap_reads_latest_child_posts_only_without_aula_or_ai_calls(controller):
    items = [
        {"id": "post:old", "content_type": "Post", "child_slugs": ["noah"],
         "date": "2026-10-25", "title": "Gammel", "translated_title": "Older news", "summary": "**Bring boots**."},
        {"id": "thread:1", "content_type": "Message", "child_slugs": ["noah"],
         "date": "2026-10-30", "title": "Private conversation", "summary": "Do not include."},
        {"id": "post:other", "content_type": "Post", "child_slugs": ["alma"],
         "date": "2026-10-30", "title": "Other child", "summary": "Do not include."},
        {"id": "post:latest", "content_type": "Post", "child_slugs": ["noah"],
         "date": "2026-10-28", "title": "Latest news", "summary": "## Trip\n- Pack **lunch**.\n- Bring [boots](https://example.com)."},
        {"id": "album:1", "content_type": "Album", "child_slugs": ["noah"],
         "date": "2026-10-31", "title": "Album", "summary": "Do not include."},
    ]
    controller.feed_json = json.dumps({"items": items, "updated_at": "published", "errors": []}).encode()
    sick = Sick()
    api = SiriConnector(controller, sick)
    result = api.command({"action": "recap", "child": "Noah", "count": 2})
    assert [post["id"] for post in result["posts"]] == ["post:latest", "post:old"]
    assert "Pack lunch" in result["speech"] and "Bring boots" in result["speech"]
    assert "Do not include" not in result["speech"] and "https" not in result["speech"]
    assert result["cached"] and result["feed_published_at"] == "published"
    assert sick.calls == [] and controller.client.service_calls == []


def test_recap_surfaces_empty_original_and_stale_states(controller):
    api = SiriConnector(controller, Sick())
    controller.feed_json = b'{"items":[]}'
    assert "No cached" in api.command({"action": "recap", "child": "noah"})["speech"]
    controller.feed_json = json.dumps({"items": [{
        "id": "post:1", "content_type": "Post", "child_slugs": ["noah"],
        "title": "Danish title", "original_text": "Original content", "date": "2026-10-29",
    }], "errors": ["Rate limited"]}).encode()
    result = api.command({"action": "recap", "child": "noah"})
    assert "Summary not ready" in result["speech"] and "Original content" in result["speech"]
    assert "last saved" in result["speech"]


@pytest.mark.parametrize("value", [
    {}, [], {"action": "delete"}, {"action": "recap", "child": "unknown"},
    {"action": "recap", "child": "noah", "count": True},
    {"action": "recap", "child": "noah", "count": 6},
    {"action": "recap", "child": "noah", "url": "https://example.com"},
    {"action": "sick_today", "child": "alma", "confirmed": "true", "date": "2026-10-29"},
    {"action": "sick_today", "child": "alma", "date": "2026-10-29"},
])
def test_invalid_commands_never_write(controller, value):
    sick = Sick()
    with pytest.raises(ValueError):
        SiriConnector(controller, sick).command(value)
    assert sick.calls == []


def test_sick_command_requires_current_date_and_confirmation(controller):
    sick = Sick()
    api = SiriConnector(controller, sick)
    status = api.command({"action": "sick_status", "child": "Alma"})
    assert status["can_report"] and status["date"] == "2026-10-29"
    assert not sick.calls
    result = api.command({"action": "sick_today", "child": "alma", "date": status["date"], "confirmed": True})
    assert result["sick"] and sick.calls == [("alma", "2026-10-29")]
    assert "only" in result["speech"] and "school teacher" in result["speech"]
    with pytest.raises(ValueError, match="date has changed"):
        api.command({"action": "sick_today", "child": "alma", "date": "2026-10-28", "confirmed": True})


def test_http_connector_requires_own_key_even_for_ingress_and_cannot_access_settings(controller):
    controller.feed_json = b'{"items":[]}'
    key = controller.profiles.set_siri_key(True)
    # External sources are denied normal app APIs, even with a Siri key.
    server = start_server(controller, port=0, allowed_ip="192.0.2.1")
    url = f"http://127.0.0.1:{server.server_port}"
    command = {"action": "recap", "child": "noah"}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        assert requests.post(url + "/api/siri/command", json=command, timeout=5).status_code == 401
        assert requests.post(url + "/api/siri/command", json=command,
                             headers={"Authorization": "Bearer " + "x" * 43}, timeout=5).status_code == 401
        result = requests.post(url + "/api/siri/command", json=command, headers=headers, timeout=5)
        assert result.status_code == 200 and result.json()["speech"]
        assert result.headers["Cache-Control"] == "no-store"
        assert requests.get(url + "/api/feed", headers=headers, timeout=5).status_code == 403
        assert requests.get(url + "/api/siri/settings", headers=headers, timeout=5).status_code == 403
        assert requests.post(url + "/api/siri/settings", json={"action": "revoke"},
                             headers={**headers, "X-Aula-Request": "settings"}, timeout=5).status_code == 403
        assert requests.get(url + "/api/siri/command", headers=headers, timeout=5).status_code == 403
        assert requests.post(url + "/api/siri/command", data="x" * 1025,
                             headers=headers, timeout=5).status_code == 400
        controller.profiles.set_siri_key(False)
        assert requests.post(url + "/api/siri/command", json=command, headers=headers, timeout=5).status_code == 401
    finally:
        server.shutdown()
        server.server_close()


def test_settings_api_creates_once_and_requires_settings_header(controller):
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        for headers in ({}, {"X-Aula-Request": "refresh"}):
            assert requests.post(url + "/api/siri/settings", json={"action": "generate"},
                                 headers=headers, timeout=5).status_code == 403
        headers = {"X-Aula-Request": "settings"}
        response = requests.post(url + "/api/siri/settings", json={"action": "generate"}, headers=headers, timeout=5)
        assert response.status_code == 200 and response.json()["key"]
        key = response.json()["key"]
        settings = requests.get(url + "/api/siri/settings", timeout=5).json()
        assert settings["enabled"] and "key" not in settings
        assert key not in json.dumps(settings)
        response = requests.post(url + "/api/siri/settings", json={"action": "revoke"}, headers=headers, timeout=5)
        assert not response.json()["enabled"] and not controller.profiles.verify_siri_key(key)
    finally:
        server.shutdown()
        server.server_close()


def test_authenticated_command_budget(controller):
    api = SiriConnector(controller, Sick())
    assert all(api.admit() for _ in range(20))
    assert not api.admit()
