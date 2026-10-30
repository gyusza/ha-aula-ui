"""Regression tests for the content/AI/calendar audit fixes."""

import json
import logging
import time
from types import SimpleNamespace

import pytest

from aula_ui import calendar_bridge, gallery
from aula_ui.aula_api import AulaRateLimited
from aula_ui.calendar_feed import CalendarFeed, validate_events
from aula_ui.direct_ai import AnthropicProvider
from aula_ui.feed import CACHE_RETENTION_SECONDS
from aula_ui.ha import HAError
from aula_ui.llm import HomeAssistantAITaskProvider, LLMError
from aula_ui.related_links import RelatedReading, generate
from test_content_enrichment import RAW_MESSAGES, THREAD, ConversationClient, controller_with_thread
from test_controller import TWO_CHILDREN, build_controller
from test_feed import PostsClient


def threads(count):
    return [{**THREAD, "id": 100 + index,
             "latestMessage": {**RAW_MESSAGES[0], "id": f"latest{index}"}} for index in range(count)]


def test_rate_limited_thread_fetch_stops_for_the_cycle_and_logs_once(tmp_path, monkeypatch, caplog):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.thread_source.threads = threads(3)

    def limited(domain, service, data=None, **kwargs):
        if "getMessagesForThread" in data["uri"]:
            client.message_calls.append(data["uri"])
            raise AulaRateLimited("Aula is limiting requests (HTTP 429).")
        return ConversationClient.call_service(client, domain, service, data, **kwargs)

    client.call_service = limited
    with caplog.at_level(logging.WARNING, logger="aula_ui.feed"):
        controller.feed.update()
    assert len(client.message_calls) == 1
    thread_logs = [r for r in caplog.records if "thread" in r.getMessage().casefold()]
    assert len(thread_logs) == 1 and thread_logs[0].levelno == logging.WARNING
    items = [item for item in json.loads(controller.feed_json)["items"] if item["content_type"] == "Message"]
    assert len(items) == 3
    assert all(not item["thread_complete"] and "HTTP 429" in item["thread_fetch_error"] for item in items)
    assert not any("aula.api_call" in error for error in json.loads(controller.feed_json)["errors"])


def test_per_method_rate_limit_is_honoured(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    asked = []

    def rate_limited(method=None):
        asked.append(method)
        return method == "messaging.getMessagesForThread"

    client.rate_limited = rate_limited
    client._rate_limit_error = lambda method=None: AulaRateLimited("Paused (HTTP 429).")
    controller.feed.update()
    assert client.message_calls == []
    assert "messaging.getMessagesForThread" in asked
    assert "HTTP 429" in json.loads(controller.feed_json)["items"][0]["thread_fetch_error"]


def test_rate_limited_posts_are_a_warning_with_clear_text(tmp_path, caplog):
    class LimitedPosts(PostsClient):
        def call_service(self, domain, service, data=None, **kwargs):
            if "getAllPosts" in data["uri"]:
                raise AulaRateLimited("Aula is limiting requests (HTTP 429).")
            return super().call_service(domain, service, data, **kwargs)

    controller = build_controller(tmp_path, LimitedPosts(), children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[])
    with caplog.at_level(logging.WARNING, logger="aula_ui.feed"):
        controller.feed.update()
    errors = json.loads(controller.feed_json)["errors"]
    assert "Posts unavailable: Aula is limiting requests (HTTP 429)." in errors
    posts = [r for r in caplog.records if "Posts collection failed" in r.getMessage()]
    assert posts and all(r.levelno == logging.WARNING for r in posts)


@pytest.mark.parametrize("end, expected", [
    ("2026-10-05", "2026-10-06"),            # inclusive same-day end
    ("2026-10-06T23:59:00", "2026-10-07"),   # last included moment
    ("", "2026-10-06"),
    ("2026-10-08", "2026-10-08"),            # already exclusive
])
def test_source_all_day_ends_are_made_exclusive(tmp_path, end, expected):
    controller = build_controller(tmp_path, PostsClient(), children=TWO_CHILDREN)
    controller.store.child("noah")["events"] = [{
        "id": "allday", "title": "Skolefest", "description": "", "date": "2026-10-05",
        "time": None, "type": "event", "origin": "Calendar", "end": end,
    }]
    controller.feed.update()
    assert not any("Invalid calendar event" in e for e in json.loads(controller.feed_json)["errors"])
    events = controller.calendar.snapshot_events()
    assert len(events) == 1 and events[0]["end"] == expected


def test_zero_length_timed_source_event_is_kept_without_end(tmp_path):
    controller = build_controller(tmp_path, PostsClient(), children=TWO_CHILDREN)
    controller.store.child("noah")["events"] = [{
        "id": "timed", "title": "Møde", "description": "", "date": "2026-10-05",
        "time": "10:00", "type": "event", "origin": "Calendar", "end": "2026-10-05T10:00:00",
    }]
    controller.feed.update()
    events = controller.calendar.snapshot_events()
    assert len(events) == 1 and events[0]["end"] == ""


def test_persistently_failing_item_does_not_block_the_queue(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    newer = {**THREAD, "id": 43, "latestMessage": {**RAW_MESSAGES[0], "id": "newer",
                                                   "sendDateTime": "2026-10-09T10:00:00Z"}}
    controller.thread_source.threads = [THREAD, newer]
    attempted = []
    analyse = controller.provider.analyse

    def picky(item, child, zone):
        attempted.append(item["id"])
        if item["id"] == "thread:43":
            raise LLMError("Model refused this content")
        return analyse(item, child, zone)

    controller.provider.analyse = picky
    controller.feed.update()
    assert attempted == ["thread:43"]
    controller.feed._retry_at = 0
    controller.feed.enrich()
    assert attempted[1] == "thread:42"


def test_unreferenced_caches_are_pruned_after_retention(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    store = controller.store
    for key in ("feed_summaries", "feed_thread_content", "feed_translations"):
        cache = store.get(key)
        cache["gone-old"] = {"x": 1}
        cache["gone-new"] = {"x": 1}
        store.set(key, cache)
    seen = store.get("feed_cache_seen")
    old = time.time() - CACHE_RETENTION_SECONDS - 10
    for name in ("summaries", "threads", "translations"):
        seen[name]["gone-old"] = old
        seen[name]["gone-new"] = time.time() - 3600
    store.set("feed_cache_seen", seen)
    current = dict(store.get("feed_summaries"))
    controller.feed.update()
    for key in ("feed_summaries", "feed_thread_content", "feed_translations"):
        cache = store.get(key)
        assert "gone-old" not in cache and "gone-new" in cache
    assert "thread:42" in store.get("feed_summaries")
    assert store.get("feed_summaries")["thread:42"] == current["thread:42"]
    assert len(store.get("feed_translations")) >= 3


class FencedProvider(HomeAssistantAITaskProvider):
    def __init__(self, events_json):
        super().__init__(None)
        self.events_json = events_json

    def _generate(self, prompt, structure):
        return {"overview": "Overview.", "is_translated": True, "events_json": self.events_json}


@pytest.mark.parametrize("events_json", [
    '```json\n[{"title": "Tur", "start": "2026-10-05", "end": null, "location": null,'
    ' "evidence": "Tur 5/10", "all_day": true, "issue": null}]\n```',
    [{"title": "Tur", "start": "2026-10-05", "end": "", "location": "",
      "evidence": "Tur 5/10", "all_day": True}],
])
def test_analyse_accepts_fenced_or_native_events_with_nulls(events_json):
    item = {"id": "post:1", "title": "Tur", "original_text": "Tur 5/10", "date": "2026-10-01",
            "children": ["Noah"], "child_slugs": ["noah"], "content_type": "Post"}
    summary, _, events = FencedProvider(events_json).analyse(item, "Noah", "Europe/Copenhagen")
    assert summary and events[0]["end"] == "" and events[0]["location"] == ""
    assert "issue" not in events[0]


@pytest.mark.parametrize("events_json", ["not json", '{"a": 1}', "[1]"])
def test_analyse_rejects_malformed_events_as_llm_error(events_json):
    item = {"id": "post:1", "title": "Tur", "original_text": "Tur", "date": "2026-10-01",
            "children": [], "child_slugs": [], "content_type": "Post"}
    with pytest.raises(LLMError):
        FencedProvider(events_json).analyse(item, "Family", "Europe/Copenhagen")


@pytest.mark.parametrize("payload", [[], {"content": [{"type": "text", "text": None}]}, {"content": None}])
def test_direct_provider_unexpected_shape_is_llm_error(monkeypatch, payload):
    provider = AnthropicProvider("key", "model")
    monkeypatch.setattr(provider, "_post", lambda *args: payload)
    with pytest.raises(LLMError, match="unexpected response|no text"):
        provider._generate("prompt", {"overview": {"selector": {"text": {}}, "required": True}})


def test_related_links_accept_fenced_json_and_ignore_unhashable_urls():
    provider = SimpleNamespace(_generate=lambda prompt, structure: {
        "links_json": '```json\n[{"url": ["x"]}, {"url": "https://a.dk/x"}]\n```'})
    assert generate(provider, "p") == [{"url": ["x"]}, {"url": "https://a.dk/x"}]


def test_related_reading_ai_failure_is_warning_and_cached(tmp_path, caplog):
    controller = build_controller(tmp_path, PostsClient(), children=TWO_CHILDREN)

    class Failing:
        def _generate(self, prompt, structure):
            raise LLMError("quota exhausted")

    controller.provider = Failing()
    reading = RelatedReading(controller)
    item = {"id": "post:9", "title": "Svømning", "original_text": "Vi skal svømme", "date": "2026-10-01"}
    with caplog.at_level(logging.WARNING, logger="aula_ui.related_links"):
        reading.request(item)
        state = reading.wait(item, timeout=10)
    assert state["status"] == "error" and "AI" in state["error"]
    failed = [r for r in caplog.records if "related_reading.failed" in r.getMessage()]
    assert failed and failed[0].levelno == logging.WARNING and "quota exhausted" in failed[0].getMessage()


def test_unchanged_calendar_is_not_rewritten(tmp_path):
    controller = build_controller(tmp_path / "data", PostsClient(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir()
    controller.calendar = CalendarFeed(controller, str(directory))
    item = {"id": "post:1", "title": "Tur", "date": "2026-10-01", "original_text": "Tur 5/10",
            "child_slugs": ["noah"], "content_type": "Post"}
    controller.calendar.record(item, validate_events([{
        "title": "Tur", "start": "2026-10-05", "end": "", "all_day": True, "location": "",
        "evidence": "Tur 5/10", "origin": "aula"}], item))
    controller.calendar.publish()
    path = directory / controller.profiles.calendar_path.rsplit("/", 1)[-1]
    first, mtime = controller.calendar.content, path.stat().st_mtime_ns
    time.sleep(1.1)
    controller.calendar.publish()
    assert controller.calendar.content == first and path.stat().st_mtime_ns == mtime
    path.unlink()
    controller.calendar.publish()
    assert path.read_bytes() == first
    controller.calendar.record(item, [])
    controller.calendar.publish()
    assert b"BEGIN:VEVENT" not in path.read_bytes()
    assert not list(directory.glob("tmp*"))


@pytest.mark.parametrize("response", [ValueError("not json"), {"entries": []}, [{"no_domain": 1}]])
def test_calendar_bridge_tolerates_unexpected_responses(monkeypatch, response):
    monkeypatch.setattr(calendar_bridge.Path, "is_dir", lambda self: True)

    class Response:
        def json(self):
            if isinstance(response, Exception):
                raise response
            return response

    class Client:
        def _request(self, method, path, **kwargs):
            if method == "POST":
                raise HAError("POST request returned 400")
            return Response()

    assert isinstance(calendar_bridge.ensure_bridge(Client()), str)


def test_gallery_cache_evicts_expired_entries(monkeypatch):
    monkeypatch.setattr(gallery, "_cache", {("media", "old"): (time.monotonic() - gallery.CACHE_SECONDS - 1, {})})
    assert gallery._cached(("media", "new"), lambda: {"ok": True}) == {"ok": True}
    assert list(gallery._cache) == [("media", "new")]


def test_feed_fixture_cycle_logs_no_errors(tmp_path, caplog):
    controller = build_controller(tmp_path, PostsClient(), children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[])
    gallery._cache.pop("albums", None)
    with caplog.at_level(logging.WARNING, logger="aula_ui"):
        controller.feed.update()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_sources_without_events_leave_no_recognized_group(tmp_path):
    controller = build_controller(tmp_path, PostsClient(), children=TWO_CHILDREN)
    controller.store.child("noah")["events"] = [{
        "id": "native1", "title": "Skolefest", "description": "", "date": "2026-10-05",
        "time": None, "type": "event", "origin": "Calendar",
    }]
    controller.feed.update()
    assert "event:noah:native1" in controller.calendar.events
    controller.calendar.record({"id": "event:noah:native1", "title": "Skolefest"}, [])
    assert "event:noah:native1" not in controller.calendar.events
    assert "event:noah:native1" not in controller.store.get("recognized_events")
    controller.store.set("recognized_events", {"post:empty": [], **controller.store.get("recognized_events")})
    assert "post:empty" not in CalendarFeed(controller, str(tmp_path / "missing" / "feed")).events
