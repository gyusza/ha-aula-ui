import json

import pytest
import requests

from aula_ui.mcp import McpEndpoint
from aula_ui.web import start_server
from test_controller import FakeHA, TWO_CHILDREN, build_controller

KEY = "k" * 43
FEED = {
    "updated_at": "now", "errors": [],
    "children": [{"name": "Noah", "slug": "noah"}, {"name": "Alma", "slug": "alma"}],
    "presence": [{"slug": "noah", "status": "present"}],
    "items": [
        {"id": "post:1", "content_type": "Post", "date": "2026-10-20T08:00:00Z", "title": "Tur", "translated_title": "Trip",
         "summary": "Forest trip", "child_slugs": ["noah"], "children": ["Noah"], "attachments": [{"name": "a.pdf"}]},
        {"id": "thread:2", "content_type": "Message", "date": "2026-10-25T08:00:00Z", "title": "Sick",
         "child_slugs": ["alma"], "thread_messages": [{"id": "m1", "sender": "Teacher", "original_text": "Pack spare clothes"}]},
        {"id": "pres", "content_type": "Presence", "date": "2026-10-25T08:00:00Z", "child_slugs": ["noah"]},
    ],
}


class Sick:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"date": "2026-10-29", "children": []}

    def report(self, child, day, sick=True):
        self.calls.append((child, day, sick))
        return {"sick": sick}


@pytest.fixture
def controller(tmp_path):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    controller.feed_json = json.dumps(FEED).encode()
    return controller


def rpc(endpoint, method, params=None, id=1):
    return endpoint.handle({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def call(endpoint, name, **args):
    return rpc(endpoint, "tools/call", {"name": name, "arguments": args})["result"]


def test_initialize_list_and_notifications(controller):
    endpoint = McpEndpoint(controller, Sick(), KEY)
    assert rpc(endpoint, "initialize", {"protocolVersion": "2025-03-26"})["result"]["protocolVersion"] == "2025-03-26"
    names = [tool["name"] for tool in rpc(endpoint, "tools/list")["result"]["tools"]]
    assert "list_feed" in names and "report_sickness" not in names
    assert endpoint.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(endpoint, "nope")["error"]["code"] == -32601
    assert endpoint.handle("nonsense")["error"]["code"] == -32600


def test_writes_tool_only_when_enabled_and_reports_for_today(controller):
    sick = Sick()
    endpoint = McpEndpoint(controller, sick, KEY, allow_writes=True)
    assert "report_sickness" in endpoint.tools
    result = call(endpoint, "report_sickness", child="Alma", date="2026-10-29")
    assert not result.get("isError") and sick.calls == [("alma", "2026-10-29", True)]
    assert call(endpoint, "report_sickness", child="Zed", date="2026-10-29")["isError"]
    assert rpc(McpEndpoint(controller, sick, KEY), "tools/call", {"name": "report_sickness", "arguments": {}})["error"]


def test_feed_filtering_and_item_detail(controller):
    endpoint = McpEndpoint(controller, Sick(), KEY)
    listed = json.loads(call(endpoint, "list_feed", child="noah")["content"][0]["text"])
    assert [i["id"] for i in listed["items"]] == ["post:1"] and listed["items"][0]["title"] == "Trip"
    found = json.loads(call(endpoint, "list_feed", search="spare")["content"][0]["text"])
    assert [i["id"] for i in found["items"]] == ["thread:2"]
    ranged = json.loads(call(endpoint, "list_feed", since="2026-10-24")["content"][0]["text"])
    assert [i["id"] for i in ranged["items"]] == ["thread:2"]
    assert call(endpoint, "list_feed", child="nobody")["isError"]
    assert call(endpoint, "list_feed", limit=0)["isError"]
    item = json.loads(call(endpoint, "get_item", id="post:1")["content"][0]["text"])
    assert item["attachments"] == [{"index": 0, "name": "a.pdf", "type": None}]
    assert call(endpoint, "get_item", id="missing")["isError"]
    presence = json.loads(call(endpoint, "get_presence", child="Noah")["content"][0]["text"])
    assert presence[0]["status"] == "present"


def test_unknown_tool_and_bad_arguments(controller):
    endpoint = McpEndpoint(controller, Sick(), KEY)
    assert rpc(endpoint, "tools/call", {"name": "delete_everything"})["error"]["code"] == -32602
    assert rpc(endpoint, "tools/call", {"name": "list_feed", "arguments": []})["error"]["code"] == -32602


def test_http_endpoint_authorisation_and_protocol(controller):
    object.__setattr__(controller.config, "mcp_key", KEY)
    server = start_server(controller, port=0, allowed_ip="192.0.2.1")
    url = f"http://127.0.0.1:{server.server_port}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    try:
        assert requests.post(url + "/mcp/" + "x" * 43, json=body, timeout=5).status_code == 404
        assert requests.post(url + "/mcp/short", json=body, timeout=5).status_code == 404
        ok = requests.post(f"{url}/mcp/{KEY}", json=body, timeout=5)
        assert ok.status_code == 200 and ok.json()["result"]["tools"]
        assert ok.headers["Cache-Control"] == "no-store"
        assert requests.post(f"{url}/mcp/{KEY}", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                             timeout=5).status_code == 202
        assert requests.post(f"{url}/mcp/{KEY}", data="{", timeout=5).status_code == 400
        assert requests.post(f"{url}/mcp/{KEY}", data="x" * 70000, timeout=5).status_code == 400
        assert requests.get(f"{url}/mcp/{KEY}", timeout=5).status_code == 405
        # The key opens nothing else.
        assert requests.get(url + "/api/feed", headers={"Authorization": "Bearer " + KEY}, timeout=5).status_code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_disabled_without_key(controller):
    server = start_server(controller, port=0, allowed_ip="192.0.2.1")
    try:
        assert requests.post(f"http://127.0.0.1:{server.server_port}/mcp/{KEY}",
                             json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5).status_code == 404
    finally:
        server.shutdown()
        server.server_close()
