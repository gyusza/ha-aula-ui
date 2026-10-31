import json

import pytest
import requests

from aula_ui.mcp import AulaMCP
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
        return {"date": "2026-10-29", "children": [
            {"slug": "alma", "name": "Alma", "can_report": True},
        ]}

    def report(self, child, day, sick=True):
        self.calls.append((child, day, sick))
        return self.snapshot()


def feed(controller):
    controller.feed_json = json.dumps({
        "updated_at": "2026-10-30T12:00:00+00:00",
        "children": [{"name": "Alma", "slug": "alma"}, {"name": "Noah", "slug": "noah"}],
        "presence": [{"slug": "alma", "status": "in"}, {"slug": "noah", "status": "out"}],
        "errors": ["Rate limited"],
        "items": [
            {"id": "post:1", "content_type": "Post", "child_slugs": ["alma"], "children": ["Alma"],
             "date": "2026-10-30", "title": "Cykel tur", "translated_title": "Bike trip",
             "original_text": "Remember the helmet", "summary": "Bring a helmet.",
             "attachments": [{"name": "photo.jpg", "kind": "image", "url": "https://example.invalid/a"}]},
            {"id": "thread:1", "content_type": "Message", "child_slugs": ["noah"], "children": ["Noah"],
             "date": "2026-10-29", "title": "Thread", "thread_messages": [
                 {"id": "m1", "sender": "Teacher", "original_text": "Bring boots",
                  "attachments": [{"name": "doc.pdf", "kind": "file", "url": "https://example.invalid/b"}]},
             ], "attachments": []},
            {"id": "calendar:1", "content_type": "Calendar", "child_slugs": ["alma"], "children": ["Alma"],
             "date": "2026-10-31", "title": "School trip", "summary": "2026-10-31"},
        ],
    }).encode()


def test_initialize_tool_inventory_and_notifications(controller):
    api = AulaMCP(controller, Sick())
    response, status = api.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-03-26"}}, "guardian@example.test")
    assert status == 200 and response["result"]["protocolVersion"] == "2025-03-26"
    assert response["result"]["serverInfo"]["name"] == "aula-ui"
    response, _ = api.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "HA")
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {"aula_status", "list_feed", "get_item", "upcoming_events", "get_presence",
                     "get_attachment", "get_album", "get_album_photo", "sickness_status", "refresh",
                     "report_sickness"}
    response, status = api.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, "HA")
    assert response is None and status == 202


def test_feed_tools_filter_and_keep_thread_attachments_private(controller):
    feed(controller)
    api = AulaMCP(controller, Sick())
    result = api.call_tool("list_feed", {"child": "Alma", "search": "bike", "limit": 1}, "HA")
    body = json.loads(result["content"][0]["text"])
    assert body["count"] == 1 and body["items"][0]["title"] == "Bike trip"
    full = json.loads(api.call_tool("get_item", {"id": "thread:1"}, "HA")["content"][0]["text"])
    assert full["thread_messages"][0]["original_text"] == "Bring boots"
    assert full["thread_messages"][0]["attachments"] == [{"index": 0, "name": "doc.pdf", "type": "file"}]
    assert "url" not in json.dumps(full)
    assert "Rate limited" in json.loads(api.call_tool("aula_status", {}, "guardian")["content"][0]["text"])["errors"]


def test_invalid_filters_and_unknown_items_are_tool_errors(controller):
    api = AulaMCP(controller, Sick())
    for name, args in (
        ("list_feed", {"limit": True}),
        ("list_feed", {"child": "unknown"}),
        ("get_item", {"id": "post:missing"}),
        ("get_album", {"album": "../1"}),
        ("not_a_tool", {}),
    ):
        with pytest.raises((ValueError, LookupError)):
            api.call_tool(name, args, "HA")


def test_sickness_write_requires_explicit_confirmation_and_checks_arguments(controller):
    sick = Sick()
    api = AulaMCP(controller, sick)
    args = {"child": "Alma", "date": "2026-10-29"}
    with pytest.raises(ValueError, match="confirm"):
        api.call_tool("report_sickness", {**args, "confirmed": False}, "HA")
    assert sick.calls == []
    api.call_tool("report_sickness", {**args, "confirmed": True, "sick": False}, "HA")
    assert sick.calls == [("alma", "2026-10-29", False)]
    with pytest.raises(ValueError, match="valid YYYY-MM-DD"):
        api.call_tool("report_sickness", {**args, "date": "2026-02-31", "confirmed": True}, "HA")
    assert len(sick.calls) == 1


def test_refresh_is_queued_without_direct_aula_or_home_assistant_writes(controller):
    api = AulaMCP(controller, Sick())
    result = api.call_tool("refresh", {}, "HA")
    assert json.loads(result["content"][0]["text"]) == {"queued": True}
    assert controller.client.service_calls == []


def test_profiles_migration_removes_retired_siri_key_hash(controller):
    from aula_ui.profiles import Profiles

    controller.profiles.data["siri_key_hash"] = "retired-hash"
    controller.profiles.save()
    restored = Profiles(controller.config)
    assert "siri_key_hash" not in restored.data
    assert "siri_key_hash" not in json.loads(restored.path.read_text())


def test_http_mcp_requires_existing_auth_checks_origin_and_supports_streamable_post(controller):
    feed(controller)
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}"
    headers = {"Content-Type": "application/json", "Origin": url}
    try:
        initialized = requests.post(url + "/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }, timeout=5)
        assert initialized.status_code == 200
        assert initialized.headers["MCP-Protocol-Version"] == "2025-11-25"
        assert initialized.json()["result"]["capabilities"]["tools"]["listChanged"] is False
        tools = requests.post(url + "/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_feed", "arguments": {"child": "alma"}},
        }, timeout=5)
        assert tools.status_code == 200
        assert json.loads(tools.json()["result"]["content"][0]["text"])["count"] == 2
        assert requests.get(url + "/mcp", timeout=5).status_code == 405
        assert requests.delete(url + "/mcp", timeout=5).status_code == 405
        assert requests.post(url + "/mcp", headers={**headers, "Origin": "https://attacker.example"},
                             json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
                             timeout=5).status_code == 403
        assert requests.post(url + "/mcp", headers={"Origin": url, "Content-Type": "text/plain"},
                             data=json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}),
                             timeout=5).status_code == 415
    finally:
        server.shutdown()
        server.server_close()


def test_http_mcp_accepts_valid_cloudflare_identity_but_not_unauthenticated_requests(controller):
    class Access:
        def verify(self, token):
            return {"email": "guardian@example.test"} if token == "valid" else None

    server = start_server(controller, port=0, allowed_ip="192.0.2.1", access=Access())
    url = f"http://127.0.0.1:{server.server_port}"
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "aula_status", "arguments": {}}}
    try:
        assert requests.post(url + "/mcp", json=request, timeout=5).status_code == 403
        response = requests.post(url + "/mcp", json=request,
                                 headers={"Cf-Access-Jwt-Assertion": "valid"}, timeout=5)
        assert response.status_code == 200
        assert json.loads(response.json()["result"]["content"][0]["text"])["identity"] == "guardian@example.test"
    finally:
        server.shutdown()
        server.server_close()
