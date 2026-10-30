"""Posts attribution, real offset pagination, feed ordering and persistence."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from aula_ui.aula_api import AulaApiError
from aula_ui.feed import date_key, fetch_posts
from test_controller import TWO_CHILDREN, FakeHA, build_controller


PROFILES = [{
    "institutionProfiles": [
        {"id": 10, "institutionCode": "daycare"},
        {"id": 20, "institutionCode": "school"},
    ],
    "children": [
        {"id": 11, "profileId": 101, "name": "Alma Jensen", "institutionCode": "daycare"},
        {"id": 21, "profileId": 201, "name": "Noah Emil Jensen", "institutionCode": "school"},
    ],
}]


def raw_post(pid, title="A post", date="2026-10-02T10:00:00+00:00"):
    return {
        "id": pid, "title": title, "content": {"html": "<p>School update</p>"},
        "timestamp": date, "ownerProfile": {"fullName": "Teacher"},
        "commentCount": 3,
    }


class PostsClient(FakeHA):
    def __init__(self, pages=None):
        super().__init__()
        self.pages = pages or {}
        self.queries = []

    def call_service(self, domain, service, data=None, **kwargs):
        query = parse_qs(data["uri"].lstrip("?"))
        self.queries.append(query)
        if query["method"] == ["profiles.getProfilesByLogin"]:
            payload = {"data": {"profiles": PROFILES}}
        elif query["method"] in (["gallery.getAlbums"], ["gallery.getMedia"]):
            payload = {"data": {"albums": [], "results": []}}
        elif query["method"] == ["messaging.getMessagesForThread"]:
            payload = {"data": {"subject": "Message", "messages": [{
                "id": 99, "text": {"html": "Børn skal på tur med skole"},
                "sender": {"fullName": "Teacher"},
                "sendDateTime": "2026-10-03T09:00:00Z",
            }], "moreMessagesExist": False}}
        else:
            ids = query["institutionProfileIds[]"]
            assert ids in (["10", "11"], ["20", "21"])
            offset = int(query["index"][0])
            rows, more = self.pages.get((ids[1], offset), ([], False))
            payload = {"data": {"posts": rows, "hasMorePosts": more}}
        return {"service_response": {"status": {"code": 0}, **payload}}


def children(tmp_path, client):
    return build_controller(tmp_path, client, children=TWO_CHILDREN,
                            message_source="aula_api").config.children


def test_posts_are_child_filtered_and_shared_posts_deduped(tmp_path):
    client = PostsClient({
        ("11", 0): ([raw_post(1, "Daycare"), raw_post(3, "Shared")], False),
        ("21", 0): ([raw_post(2, "School"), raw_post(3, "Shared")], False),
    })
    result = {post["id"]: post for post in fetch_posts(client, children(tmp_path, client), 2)}
    assert result["post:1"]["children"] == ["Alma"]
    assert result["post:2"]["children"] == ["Noah"]
    assert result["post:3"]["children"] == ["Alma", "Noah"]
    assert result["post:1"]["original_text"] == "School update"
    assert result["post:1"]["sender"] == "Teacher"


def test_post_index_is_offset_not_page_number(tmp_path):
    client = PostsClient({
        ("11", 0): ([raw_post(i) for i in range(20)], True),
        ("11", 20): ([raw_post(20)], False),
    })
    posts = fetch_posts(client, children(tmp_path, client), 3)
    assert len(posts) == 21
    indices = [q["index"] for q in client.queries if "index" in q]
    assert indices == [["0"], ["20"], ["0"]]


def test_missing_profiles_is_explicit_error(tmp_path):
    client = FakeHA(service_responses={"aula.api_call": {"data": {}}})
    with pytest.raises(AulaApiError, match="no profiles"):
        fetch_posts(client, children(tmp_path, client), 2)


def test_unmatched_child_is_not_assigned_someone_elses_posts(tmp_path):
    client = PostsClient()
    child = SimpleNamespace(name="Other", slug="other", aula_name="", aula_child_id="")
    with pytest.raises(AulaApiError, match="No guardian/child"):
        fetch_posts(client, [child], 2)


def test_child_profile_id_matching(tmp_path):
    client = PostsClient({("11", 0): ([raw_post(1)], False)})
    child = SimpleNamespace(name="Wrong name", slug="j", aula_name="", aula_child_id="101")
    posts = fetch_posts(client, [child], 1)
    assert posts[0]["child_slugs"] == ["j"]


def test_dates_sort_by_instant_not_offset_string():
    assert date_key({"date": "2026-10-02T10:00:00+02:00"}) < date_key(
        {"date": "2026-10-02T09:00:00Z"})
    assert date_key({"date": "invalid"}) == float("-inf")


def test_mixed_feed_is_sorted_and_translation_fallback_is_not_claimed(tmp_path):
    client = PostsClient({("11", 0): ([raw_post(1)], False)})
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[{
        "id": 40, "subject": "Message", "regardingChildren": [
            {"displayName": "Noah Emil Jensen"}
        ], "latestMessage": {"id": 99, "sendDateTime": "2026-10-03T09:00:00Z",
                             "text": {"html": "Børn skal på tur med skole"}},
    }])
    controller.store.child("noah")["events"] = [{
        "id": "event1", "title": "Homework", "date": "2026-10-04",
        "time": None, "type": "homework", "description": "Read chapter 2",
    }]
    controller.feed.update()
    payload = json.loads(controller.feed_json)
    assert [q["content_type"] for q in payload["items"]] == ["Homework", "Message", "Post"]
    assert payload["items"][0]["original_text"] == "Read chapter 2"
    assert not payload["items"][1]["is_translated"]
    assert payload["items"][1]["source"] == "fallback"
    controller.store.save()
    restored = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    restored.publish_feed()
    assert json.loads(restored.feed_json)["items"] == payload["items"]


def test_posts_failure_preserves_cache_and_surfaces_error(tmp_path):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN, message_source="aula_api")
    controller.store.set("feed_posts", [{
        "id": "post:1", "title": "Cached", "date": "2026-10-01", "original_text": "",
        "content_type": "Post", "children": ["Alma"], "child_slugs": ["alma"],
    }])
    controller.feed.update()
    payload = json.loads(controller.feed_json)
    assert payload["items"][0]["title"] == "Cached"
    assert "Posts unavailable" in payload["errors"][0]


def test_successful_ai_summary_is_cached_by_content_and_budgeted(tmp_path, monkeypatch):
    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    client = PostsClient({("11", 0): ([raw_post(i) for i in range(10)], False)})
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")

    class Provider:
        name = "ha_ai_task"
        calls = 0

        def summarise(self, prompt):
            self.calls += 1
            return "English summary.", True

        def generate(self, prompt):
            return json.dumps({"translated_text": "A post", "source_language": "en"})

    controller.provider = Provider()
    controller.thread_source = SimpleNamespace(available=True, threads=[])
    controller.feed.update()
    assert controller.provider.calls == 6
    assert controller.feed.pending
    assert sum(item["is_translated"] for item in json.loads(controller.feed_json)["items"]) == 6
    query_count = len(client.queries)
    controller.feed.enrich()
    assert controller.provider.calls == 10
    assert len(client.queries) == query_count
    assert not controller.feed.pending
    controller.feed.update()
    assert controller.provider.calls == 10


def test_ai_failure_is_visible_and_backoff_prevents_request_storm(tmp_path, monkeypatch):
    from aula_ui.llm import LLMError

    clock = [100.0]
    monkeypatch.setattr("aula_ui.feed.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    client = PostsClient({("11", 0): ([raw_post(i) for i in range(10)], False)})
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")

    class Provider:
        name = "ha_ai_task"
        calls = 0

        def summarise(self, prompt):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("Quota exhausted")
            return "English summary.", True

        def generate(self, prompt):
            return json.dumps({"translated_text": "A post", "source_language": "en"})

    controller.provider = Provider()
    controller.thread_source = SimpleNamespace(available=True, threads=[])
    controller.feed.update()
    assert controller.provider.calls == 1
    assert controller.feed.retry_delay == 60
    payload = json.loads(controller.feed_json)
    assert "AI is temporarily unavailable" in payload["errors"][0]
    assert payload["items"][0]["source"] == "original"
    assert not payload["items"][0]["is_translated"]
    controller.feed.enrich()
    assert controller.provider.calls == 1
    clock[0] += 61
    controller.feed.enrich()
    assert controller.provider.calls == 7
    assert not json.loads(controller.feed_json)["errors"]
