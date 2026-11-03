"""Aula author context stays separate from identity and cached AI content."""

import json
from pathlib import Path

import pytest

from aula_ui.aula_api import message_from_thread_summary
from aula_ui.direct_source import DirectAulaClient
from aula_ui.feed import content_fingerprint, fetch_posts
from aula_ui.messages import normalise_message, sender_details
from test_content_enrichment import THREAD, controller_with_thread
from test_feed import PostsClient, children, raw_post


@pytest.mark.parametrize("profile, expected", [
    ({"fullName": "Erik Jensen", "metadata": "Noah, Alma"},
     {"sender": "Erik Jensen", "sender_metadata": "Noah, Alma"}),
    ({"displayName": "Erik Jensen", "metadata": " Noah "},
     {"sender": "Erik Jensen", "sender_metadata": "Noah"}),
    ({"name": "Teacher", "metadata": None}, {"sender": "Teacher"}),
    ({"fullName": "Teacher", "metadata": {"invalid": True}}, {"sender": "Teacher"}),
    ("Teacher", {"sender": "Teacher"}),
    ({}, {"sender": "Unknown"}),
])
def test_sender_details(profile, expected):
    assert sender_details(profile) == expected


def test_normalising_preserves_metadata_without_changing_message_identity():
    raw = {"text": "Hello", "sender": "Erik Jensen"}
    original = normalise_message(raw)
    annotated = normalise_message({
        **raw, "sender": {"fullName": "Erik Jensen", "metadata": "Noah, Alma"},
    })
    assert annotated == {**original, "sender_metadata": "Noah, Alma"}
    assert normalise_message(annotated) == annotated


def test_thread_preview_uses_its_sender_not_recipients_or_creator():
    result = message_from_thread_summary({
        "subject": "Trip", "creator": {"fullName": "Teacher", "metadata": "School"},
        "regardingChildren": [{"displayName": "Unrelated child"}],
        "latestMessage": {"id": 7, "text": "Hello",
                          "sender": {"fullName": "Erik Jensen", "metadata": "Noah, Alma"}},
    })
    assert result["sender"] == "Erik Jensen"
    assert result["sender_metadata"] == "Noah, Alma"


def test_post_metadata_is_not_its_child_audience(tmp_path):
    post = raw_post(1)
    post["ownerProfile"] = {"fullName": "Erik Jensen", "metadata": "Noah, Alma"}
    client = PostsClient({("11", 0): ([post], False)})
    result = fetch_posts(client, children(tmp_path, client), 1)[0]
    assert result["sender_metadata"] == "Noah, Alma"
    assert result["children"] == ["Alma"]


def test_native_calendar_preserves_author_metadata():
    result = DirectAulaClient._native_event({
        "id": 1, "title": "Trip", "startDateTime": "2026-11-05T10:00:00Z",
        "creator": {"fullName": "Erik Jensen", "metadata": "Noah, Alma"},
    })
    assert result["sender"] == "Erik Jensen"
    assert result["sender_metadata"] == "Noah, Alma"


def test_legacy_thread_refresh_keeps_translations_and_summary(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    before = json.loads(controller.feed_json)["items"][0]
    analysis_calls = len(controller.provider.analyses)
    translation_calls = len(controller.provider.calls)
    client.messages[0] = {
        **client.messages[0], "sender": {"fullName": "Parent", "metadata": "Noah, Alma"},
    }
    cached = controller.store.get("feed_thread_content")["42"]
    cached.pop("sender_metadata_loaded")
    controller.feed.update()
    after = json.loads(controller.feed_json)["items"][0]
    assert client.message_calls == [0, 1, 0, 1]
    assert after["thread_messages"][0]["sender_metadata"] == "Noah, Alma"
    assert after["thread_messages"][0]["translated_text"] == before["thread_messages"][0]["translated_text"]
    assert after["summary"] == before["summary"]
    assert after["fingerprint"] == before["fingerprint"]
    assert len(controller.provider.analyses) == analysis_calls
    assert len(controller.provider.calls) == translation_calls
    controller.feed.update()
    assert client.message_calls == [0, 1, 0, 1]
    controller.store.save()
    assert json.loads(Path(controller.store.path).read_text())["feed"][0]["thread_messages"][0]["sender_metadata"] == "Noah, Alma"


def test_legacy_metadata_refresh_respects_stale_request_cap(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    controller.store.get("feed_thread_content")["42"].pop("sender_metadata_loaded")
    controller.feed._stale_refreshes = 3
    result = controller.feed._conversation(THREAD, {"id": "m2"})
    assert result["thread_complete"]
    assert client.message_calls == [0, 1]


def test_metadata_is_not_ai_source_content():
    item = {"title": "Trip", "original_text": "Hello", "date": "2026-11-01",
            "thread_messages": [{"id": "1", "sender": "Parent", "original_text": "Hello"}]}
    before = content_fingerprint(item, "Europe/Copenhagen")
    item["thread_messages"][0]["sender_metadata"] = "Noah, Alma"
    assert content_fingerprint(item, "Europe/Copenhagen") == before
