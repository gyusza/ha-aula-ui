import time
import copy
from dataclasses import replace

import pytest

from aula_ui import notifications
from aula_ui.notifications import Notifier, handle, item_category, render
from test_controller import TWO_CHILDREN, FakeHA, build_controller


@pytest.fixture(autouse=True)
def fast_shutdown(monkeypatch):
    from aula_ui import web
    monkeypatch.setattr(web, "SERVE_POLL_INTERVAL", 0.01)


class NotifyHA(FakeHA):
    def __init__(self, fail=()):
        super().__init__(states={
            "notify.iphone": {"entity_id": "notify.iphone", "state": "unknown",
                              "attributes": {"friendly_name": "Lars's iPhone"}},
            "notify.old": {"entity_id": "notify.old", "state": "unavailable", "attributes": {}},
        })
        self.fail = set(fail)

    def list_services(self):
        return {"notify": ["mobile_app_lars_s_iphone", "notify", "send_message", "persistent_notification"]}

    def call_service(self, domain, service, data=None, return_response=False, timeout=None):
        self.service_calls.append((domain, service, data))
        if (data or {}).get("entity_id", f"notify.{service}") in self.fail:
            raise RuntimeError("Service not found")
        return []


def post(item_id, **extra):
    return {"id": item_id, "title": "Trip", "translated_title": "Trip to the forest",
            "content_type": "Post", "children": ["Noah"], "child_slugs": ["noah"],
            "summary": "**Bring** boots and a packed lunch.", "sender": "Teacher", **extra}


@pytest.fixture
def setup(tmp_path):
    client = NotifyHA()
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN)
    controller.config = replace(controller.config, public_url="https://aula.example.com")
    notifier = Notifier(controller)
    notifier._enqueue = lambda targets, message: notifier.deliver(targets, message)
    controller.notifier = notifier
    return controller, client, notifier


def sent(client):
    return [call for call in client.service_calls if call[0] == "notify"]


def test_child_focused_notification_uses_aula_names_only_and_leaves_feed_unchanged(setup):
    controller, _, notifier = setup
    controller.config = replace(controller.config, children=[
        replace(controller.config.children[0], name="Alex", aula_name="Alex Example")])
    item = post("post:focus", original_text=(
        "The class had a lovely day. Another pupil painted. Alex built a brilliant tower."),
        summary="The class enjoyed creative activities.", children=["Alex"])
    original = copy.deepcopy(item)
    prompts = []

    class Provider:
        def summarise(self, prompt):
            prompts.append(prompt)
            return "Alex built a brilliant tower.", True

    controller.provider = Provider()
    message = notifier._messages([(item, "post")], ".")[0]
    focused = notifier._focused(message)
    assert focused["message"] == "Alex built a brilliant tower."
    assert "Alex Example" in prompts[0]
    assert "Alma" not in prompts[0]
    assert not any(key.startswith("_focus") for key in focused)
    assert focused["url"] == "./post/post%3Afocus"
    assert item == original
    assert notifier._focused(message) == focused
    assert len(prompts) == 1  # Same preview reused for HA and every subscribed device.


def test_audience_metadata_and_substrings_do_not_trigger_focus(setup):
    _, _, notifier = setup
    item = post("post:general", original_text="Noahson enjoyed the trip. The whole class had fun.")
    message = notifier._messages([(item, "post")], ".")[0]
    assert "_focus_source" not in message
    assert notifier._focused(message)["message"] == render(item, "post")["message"]


def test_reply_focus_uses_only_latest_message_and_keeps_other_children_separate(setup):
    controller, _, notifier = setup
    controller.provider = None
    item = post("thread:focus", content_type="Message", thread_messages=[
        {"id": "new", "date": "2026-10-27T10:00:00Z",
         "original_text": "The class sang. Noah played drums. Alma danced."},
        {"id": "old", "date": "2026-10-26T10:00:00Z",
         "original_text": "Noah was absent yesterday."},
    ])
    message = notifier._messages([(item, "message")], ".")[0]
    assert "absent" not in message["_focus_source"]
    focused = notifier._focused(message)["message"]
    assert focused == "Noah played drums. Alma danced."
    item["thread_messages"][0]["original_text"] = "The whole class is welcome tomorrow."
    assert "_focus_source" not in notifier._messages([(item, "message")], ".")[0]


def test_focus_failure_logs_and_uses_named_passage(setup, caplog):
    controller, _, notifier = setup

    class Provider:
        def summarise(self, prompt):
            raise notifications.LLMError("AI unavailable")

    controller.provider = Provider()
    item = post("post:focus", original_text="General class news. Noah needs his boots tomorrow.")
    message = notifier._messages([(item, "post")], ".")[0]
    assert notifier._focused(message)["message"] == "Noah needs his boots tomorrow."
    assert "notifications.focus_failed" in caplog.text


def test_digest_prioritizes_named_content(setup):
    controller, _, notifier = setup
    controller.provider = None
    items = [(post(f"post:{index}", original_text="Class news."), "post") for index in range(6)]
    items[-1][0]["original_text"] = "Noah won a medal."
    message = notifier._focused(notifier._messages(items, ".")[0])
    assert message["title"] == "6 new items in Aula"
    assert message["message"] == "Noah won a medal."


def test_focus_delivery_runs_off_sync_thread_for_ha_and_push(setup, monkeypatch):
    controller, _, notifier = setup
    controller.provider = None
    jobs = []
    delivered = []
    monkeypatch.setattr(notifier, "_submit", lambda job, message: jobs.append(job))
    monkeypatch.setattr(notifier, "deliver", lambda targets, message: delivered.append(message))
    monkeypatch.setattr(notifier.push, "send", lambda device, message: delivered.append(message))
    message = notifier._messages([
        (post("post:1", original_text="School news. Alma painted a picture."), "post")], ".")[0]
    Notifier._enqueue(notifier, ["entity:notify.iphone"], message)
    notifier._enqueue_push({"id": "phone"}, message)
    assert delivered == []
    for job in jobs:
        job()
    assert [entry["message"] for entry in delivered] == ["Alma painted a picture."] * 2


def test_newest_first_replies_trigger_push_and_preview_latest(setup, monkeypatch):
    _, _, notifier = setup
    pushed = []
    monkeypatch.setattr(notifier.push, "all", lambda: [
        {"id": "phone", "categories": ["message"], "children": ["noah"]}])
    notifier._enqueue_push = lambda device, message: pushed.append(message)
    old = {"id": "old", "date": "2026-10-25T10:00:00Z", "original_text": "Older", "sender": "Teacher"}
    new = {"id": "new", "date": "2026-10-26T20:52:28Z", "translated_text": "Newest reply", "sender": "Parent"}
    thread = post("thread:1", content_type="Message", thread_messages=[old])
    assert notifier.observe([thread]) == 0
    thread["thread_messages"] = [new, old]
    assert notifier.observe([thread]) == 1
    assert pushed[-1]["message"] == "Parent: Newest reply"
    assert notifier.observe([thread]) == 0
    thread["thread_messages"] = [old, new]
    assert notifier.observe([thread]) == 0


def test_oldest_reply_signature_upgrade_does_not_flood(setup):
    controller, client, notifier = setup
    notifier.save({"targets": ["entity:notify.iphone"], "categories": ["message"], "children": []})
    controller.store.set(notifications.SEEN_KEY, {"thread:1": "old"})
    thread = post("thread:1", content_type="Message", thread_messages=[
        {"id": "current", "date": "2026-10-26T20:00:00Z"},
        {"id": "old", "date": "2026-10-01T10:00:00Z"}])
    assert notifier.observe([thread]) == 0
    assert not sent(client)
    thread["thread_messages"].insert(0, {"id": "next", "date": "2026-10-27T06:00:00Z"})
    assert notifier.observe([thread]) == 1


def nap_presence(intervals, state="present", day="2026-10-26"):
    return [{"child_slug": "alma", "name": "Alma", "state": state,
             "updated_at": f"{day}T13:00:00+00:00", "sleep_intervals": intervals}]


def test_nap_start_end_duration_dedup_and_restart(setup):
    controller, client, notifier = setup
    notifier.save({"targets": ["service:notify.mobile_app_lars_s_iphone"],
                   "categories": ["nap"], "children": ["alma"]})
    assert notifier.observe_naps(nap_presence([])) == 0
    nap = {"start": "2026-10-26T11:40:00+01:00", "end": None}
    assert notifier.observe_naps(nap_presence([nap])) == 1
    assert sent(client)[-1][2]["title"] == "Alma started napping"
    assert sent(client)[-1][2]["message"] == "Asleep since 11:40."
    assert sent(client)[-1][2]["data"]["url"].endswith("/child/alma")
    assert notifier.observe_naps(nap_presence([nap])) == 0
    restored = Notifier(controller)
    restored._enqueue = lambda targets, message: restored.deliver(targets, message)
    assert restored.observe_naps(nap_presence([nap])) == 0
    ended = {**nap, "end": "2026-10-26T13:15:00+01:00"}
    assert restored.observe_naps(nap_presence([ended])) == 1
    assert sent(client)[-1][2]["title"] == "Alma woke up"
    assert sent(client)[-1][2]["message"] == "Slept 1h 35m (11:40–13:15)."
    assert restored.observe_naps(nap_presence([ended])) == 0
    assert restored.observe_naps(nap_presence([])) == 0
    assert restored.observe_naps(nap_presence([ended])) == 0


def test_nap_baseline_unknown_filters_and_push_delivery(setup, monkeypatch):
    controller, client, notifier = setup
    assert "nap" not in notifier.settings()["categories"]
    notifier.save({"targets": ["entity:notify.iphone"], "categories": ["nap"], "children": ["noah"]})
    start = {"start": "2026-10-26T11:40:00+01:00", "end": None}
    # First valid observation is a silent baseline, even after missing/failed source data.
    assert notifier.observe_naps(nap_presence([], "unknown")) == 0
    assert notifier.observe_naps(nap_presence([start])) == 0
    pushed = []
    monkeypatch.setattr(notifier.push, "all", lambda: [
        {"id": "phone", "categories": ["nap"], "children": ["alma"]},
        {"id": "other", "categories": ["post"], "children": []},
    ])
    notifier._enqueue_push = lambda device, message: pushed.append((device, message))
    ended = {**start, "end": "2026-10-26T12:05:00+01:00"}
    assert notifier.observe_naps(nap_presence([ended], "unknown")) == 0
    assert notifier.observe_naps(nap_presence([ended])) == 1
    assert not sent(client)
    assert len(pushed) == 1 and pushed[0][0]["id"] == "phone"
    assert pushed[0][1]["message"] == "Slept 25m (11:40–12:05)."
    assert pushed[0][1]["url"] == "./child/alma"
    assert notifier.observe_naps(nap_presence([ended])) == 0
    next_nap = {"start": "2026-10-27T12:00:00+01:00", "end": None}
    assert notifier.observe_naps(nap_presence([next_nap], day="2026-10-27")) == 1
    assert len(controller.store.get("notification_naps")["alma"]["intervals"]) == 1


def test_naps_observed_while_disabled_do_not_replay_on_enable(setup):
    _, client, notifier = setup
    notifier.observe_naps(nap_presence([]))
    nap = {"start": "2026-10-26T11:40:00+01:00", "end": None}
    assert notifier.observe_naps(nap_presence([nap])) == 0
    notifier.save({"targets": ["entity:notify.iphone"], "categories": ["nap"], "children": []})
    assert notifier.observe_naps(nap_presence([nap])) == 0
    assert not sent(client)


def test_targets_list_entities_and_mobile_services(setup):
    _, _, notifier = setup
    targets = {t["id"]: t for t in notifier.targets()}
    assert set(targets) == {"entity:notify.iphone", "entity:notify.old",
                            "service:notify.mobile_app_lars_s_iphone", "service:notify.persistent_notification"}
    assert targets["entity:notify.iphone"]["name"] == "Lars's iPhone"
    assert not targets["entity:notify.old"]["available"]
    assert "tap opens Aula" in targets["service:notify.mobile_app_lars_s_iphone"]["name"]


def test_baseline_then_only_new_items_are_sent(setup):
    controller, client, notifier = setup
    assert notifier.observe([]) == 0
    assert notifier.observe([post("post:1")]) == 0  # baseline
    notifier.save({"targets": ["entity:notify.iphone", "service:notify.mobile_app_lars_s_iphone"],
                   "categories": ["post", "message"], "children": []})
    assert notifier.observe([post("post:1")]) == 0
    assert notifier.observe([post("post:1"), post("post:2")]) == 1
    calls = sent(client)
    assert calls[0] == ("notify", "send_message", {
        "entity_id": "notify.iphone", "title": "Noah · Trip to the forest",
        "message": "Teacher: Bring boots and a packed lunch."})
    assert calls[1][1] == "mobile_app_lars_s_iphone"
    assert calls[1][2]["data"]["url"] == "https://aula.example.com/post/post%3A2"
    assert notifier.observe([post("post:1"), post("post:2")]) == 0
    restored = Notifier(build_controller(controller.config.data_dir, client, children=TWO_CHILDREN))
    restored._enqueue = lambda *a: pytest.fail("restart must not resend")
    assert restored.observe([post("post:1"), post("post:2")]) == 0


def test_calendar_notification_uses_central_timezone(setup):
    from dataclasses import replace

    controller, _, notifier = setup
    controller.config = replace(controller.config, timezone="America/New_York")
    item = {"id": "event:central", "title": "Trip", "children": ["Noah"],
            "calendar_event": {"start": "2026-10-09T14:00:00+00:00"}}
    messages = notifier._messages([(item, "calendar")], "")
    assert messages[0]["message"] == "Fri 09 Oct 10:00"


def test_categories_children_translation_wait_and_replies(setup, monkeypatch):
    _, client, notifier = setup
    thread = {"id": "thread:9", "title": "Party", "content_type": "Message", "children": ["Alma"],
              "child_slugs": ["alma"], "thread_messages": [{"id": "m1", "sender": "Ann", "original_text": "Hej"}]}
    notifier.observe([thread])
    notifier.save({"targets": ["entity:notify.iphone"], "categories": ["message"], "children": ["alma"]})
    lesson = {"id": "event:x", "title": "Room 12", "content_type": "Calendar",
              "calendar_event": {"calendar_kind": "lesson", "start": "2999-01-01T08:00:00+00:00"}}
    assert item_category(lesson) == "lesson"
    assert notifier.observe([thread, lesson, post("post:5")]) == 0, "unselected categories are skipped"
    reply = {**thread, "analysis_pending": True, "thread_messages": thread["thread_messages"] + [
        {"id": "m2", "sender": "Bo", "original_text": "Kage?", "translated_text": "Cake?"}]}
    assert notifier.observe([reply]) == 0, "waits for translation"
    monkeypatch.setattr(notifications, "TRANSLATION_GRACE_SECONDS", -1)
    assert notifier.observe([reply]) == 1
    assert sent(client)[-1][2]["title"] == "Alma · New reply: Party"
    assert sent(client)[-1][2]["message"] == "Bo: Cake?"
    other_child = {**post("post:7"), "content_type": "Message", "child_slugs": ["noah"]}
    assert notifier.observe([reply, other_child]) == 0


def test_bulk_new_items_become_one_digest_and_past_events_skip(setup):
    _, client, notifier = setup
    notifier.observe([post("post:0")])
    notifier.save({"targets": ["entity:notify.iphone"], "categories": ["post", "calendar"], "children": []})
    past = {"id": "event:old", "title": "Old", "content_type": "Calendar",
            "calendar_event": {"start": "2000-01-01", "end": "2000-01-02"}}
    items = [post(f"post:{n}") for n in range(1, 8)] + [past]
    assert notifier.observe(items) == 1
    assert sent(client)[-1][2]["title"] == "7 new items in Aula"
    assert sent(client)[-1][2]["message"] == "7 posts"


def test_render_calendar_and_suggestion():
    item = {"id": "calendar:x", "title": "Pyjama day", "children": ["Noah"],
            "calendar_event": {"start": "2026-10-09", "location": "Hall", "review_required": True}}
    assert item_category(item) == "suggestion"
    assert render(item, "suggestion") == {"title": "Noah · Calendar suggestion: Pyjama day",
                                          "message": "Fri 09 Oct · Hall · Needs your approval."}


def test_failed_target_reported_without_blocking_others(setup):
    controller, client, notifier = setup
    client.fail = {"notify.iphone"}
    status, result = handle(controller, "POST", "/api/notifications/test",
                            {"targets": ["entity:notify.iphone", "service:notify.persistent_notification"]})
    assert status == 502 and result["sent"] == 1
    assert "Service not found" in result["errors"][0]
    snapshot = handle(controller, "GET", "/api/notifications")[1]
    assert "notify.iphone" in snapshot["status"]["last_error"] and snapshot["status"]["last_sent"]


@pytest.mark.parametrize("value", [
    {"targets": ["light.kitchen"], "categories": [], "children": []},
    {"targets": [], "categories": ["spam"], "children": []},
    {"targets": [], "categories": [], "children": ["nobody"]},
    {"targets": "notify.iphone", "categories": [], "children": []},
    {"targets": []},
])
def test_invalid_settings_rejected(setup, value):
    controller, _, _ = setup
    assert handle(controller, "POST", "/api/notifications", value)[0] == 400


def test_test_requires_targets(setup):
    controller, _, _ = setup
    assert handle(controller, "POST", "/api/notifications/test", {})[0] == 400


def test_http_endpoints_require_header_and_round_trip(setup):
    import requests
    from aula_ui.web import start_server

    controller, client, _ = setup
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}"
    headers = {"X-Aula-Request": "settings"}
    value = {"targets": ["entity:notify.iphone"], "categories": ["post"], "children": ["noah"]}
    try:
        assert requests.get(url + "/notifications.js", timeout=5).status_code == 200
        data = requests.get(url + "/api/notifications", timeout=5).json()
        assert data["targets"] == [] and data["categories"] == notifications.DEFAULT_CATEGORIES
        assert requests.post(url + "/api/notifications", json=value, timeout=5).status_code == 403
        assert requests.post(url + "/api/notifications", data="x", headers=headers, timeout=5).status_code == 400
        saved = requests.post(url + "/api/notifications", json=value, headers=headers, timeout=5)
        assert saved.status_code == 200 and saved.json()["targets"] == value["targets"]
        assert requests.get(url + "/api/notifications", timeout=5).json()["children"] == ["noah"]
        tested = requests.post(url + "/api/notifications/test", json={}, headers=headers, timeout=5)
        assert tested.status_code == 200 and tested.json() == {"sent": 1, "errors": []}
        assert sent(client)[-1][2]["title"] == "Aula test notification"
    finally:
        service.shutdown()
        service.server_close()


def test_public_url_deep_links(setup):
    from dataclasses import replace
    controller, client, notifier = setup
    controller.config = replace(controller.config, public_url="https://aula-ui.example/")
    notifier.observe([post("post:0")])
    notifier.save({"targets": ["service:notify.mobile_app_lars_s_iphone"], "categories": ["post", "message"],
                   "children": []})
    thread = post("message:7", content_type="Message", thread_messages=[{"id": "m1"}])
    assert notifier.observe([post("post:0"), post("post:1/x"), thread]) == 2
    urls = [c[2]["data"] for c in sent(client)]
    assert urls[0]["url"] == urls[0]["clickAction"] == "https://aula-ui.example/post/post%3A1%2Fx"
    assert urls[1]["url"] == "https://aula-ui.example/thread/message%3A7"
    assert notifications.deep_link("", {"id": "a"}) == ""
    assert notifications.deep_link("https://x/") == "https://x/"


def test_push_devices_get_their_own_topics_and_relative_links(setup, tmp_path):
    from test_webpush import browser_keys
    controller, client, notifier = setup
    pushed = []
    notifier._enqueue_push = lambda device, message: pushed.append((device["name"], message))
    notifier.observe([post("old")])
    _, p256dh, auth = browser_keys()
    status, result = handle(controller, "POST", "/api/push/subscribe", {
        "subscription": {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": p256dh, "auth": auth}},
        "name": "iPhone", "categories": ["message"], "children": []})
    assert status == 200 and result["device"]["name"] == "iPhone" and result["public_key"]
    device_id = result["device"]["id"]
    message = {"id": "m1", "title": "Trip", "content_type": "Message", "child_slugs": ["noah"],
               "children": ["Noah"], "thread_messages": [{"id": "x", "original_text": "Hi", "sender": "T"}]}
    # No HA targets: only the device is notified, and only about messages.
    assert notifier.observe([post("old"), post("p1"), message]) == 1
    assert not sent(client)
    assert pushed == [("iPhone", {**render(message, "message"), "tag": "aula-m1", "url": "./thread/m1"})]
    assert handle(controller, "GET", "/api/notifications")[1]["push"]["devices"][0]["id"] == device_id
    assert handle(controller, "POST", "/api/push/device", {"id": device_id, "categories": ["post"],
                                                           "children": []})[0] == 200
    assert notifier.observe([post("old"), post("p1"), message, post("p2")]) == 1
    assert pushed[-1][1]["url"] == "./post/p2"
    assert handle(controller, "POST", "/api/push/device", {"id": "0" * 16, "categories": [],
                                                           "children": []})[0] == 404
    assert handle(controller, "POST", "/api/push/test", {"id": "0" * 16})[0] == 400
    assert handle(controller, "POST", "/api/push/remove", {"id": device_id})[1]["devices"] == []
    assert handle(controller, "POST", "/api/push/remove", {})[0] == 400


def test_history_derives_read_state_and_naps_are_read(setup):
    controller, client, notifier = setup
    notifier.save({"targets": ["service:notify.mobile_app_lars_s_iphone"],
                   "categories": ["post", "message", "nap"], "children": []})
    notifier.observe([post("post:0")])
    message = {"id": "thread:9", "title": "Hej", "content_type": "Message", "unread": True,
               "child_slugs": [], "children": [], "summary": "Hello", "sender": "Teacher"}
    controller.store.set("feed", [post("post:1"), message])
    notifier.observe([post("post:0"), post("post:1"), message])
    nap = {"start": "2026-10-26T11:40:00+01:00", "end": None}
    notifier.observe_naps(nap_presence([]))
    notifier.observe_naps(nap_presence([nap]))
    history = notifier.history()
    by_id = {entry["item_id"]: entry for entry in history["items"]}
    assert by_id["post:1"]["read"] is False and by_id["thread:9"]["read"] is False
    assert next(e for e in history["items"] if e["category"] == "nap")["read"] is True
    assert history["unread"] == 2 and history["items"][0]["category"] == "nap"
    message["unread"] = False
    controller.store.set("feed", [post("post:1"), message])
    assert notifier.history()["unread"] == 1
    status, result = handle(controller, "POST", "/api/notifications/read", {"id": "post:1"})
    assert status == 200 and result["unread"] == 0
    assert handle(controller, "POST", "/api/notifications/read", {"id": 5})[0] == 400


def test_panel_path_follows_supervisor_hostname():
    from aula_ui.notifications import panel_path

    assert panel_path("local-aula-ui") == "/local_aula-ui"
    assert panel_path("a1b2c3d4-aula-ui") == "/a1b2c3d4_aula-ui"
    assert panel_path("devbox.lan") == "/"
