"""Tests for per-child thread attribution via the Aula API."""

from __future__ import annotations

import pytest

from aula_ui.aula_api import (
    AulaApiError,
    AulaThreadSource,
    fetch_thread_messages,
    fetch_threads,
    message_from_thread_summary,
    thread_children,
    thread_matches_child,
    threads_for_child,
)


class Child:
    def __init__(self, slug, name, aula_name="", aula_child_id=""):
        self.slug = slug
        self.name = name
        self.aula_name = aula_name
        self.aula_child_id = aula_child_id


NOAH = Child("noah", "Noah", "Noah Emil Jensen")
ALMA = Child("alma", "Alma", "Alma Jensen")


def thread(tid, subject, children, read=True, date="2026-10-01T07:23:32+00:00"):
    """Build a thread dict shaped like the real messaging.getThreads payload."""
    return {
        "id": tid,
        "subject": subject,
        "read": read,
        "startedTime": date,
        "regardingChildren": [
            {"displayName": name, "profileId": pid} for name, pid in children
        ],
        "creator": {"fullName": "Freja Holm Mikkelsen"},
        "latestMessage": {
            "id": f"msg{tid}",
            "sendDateTime": date,
            "text": {"html": f"<div>Body of {subject}</div>"},
        },
    }


NOAH_THREAD = thread(1, "Ugebrev", [("Noah Emil Jensen", 900001)], read=False)
SHARED_THREAD = thread(
    2,
    "Afslutning for skolegruppen",
    [("Noah Emil Jensen", 900001), ("Alma Jensen", 111222)],
)
OTHER_THREAD = thread(3, "Noget andet", [("Someone Else", 999)])
THREADS = [NOAH_THREAD, SHARED_THREAD, OTHER_THREAD]


class FakeClient:
    """Stands in for HomeAssistantClient.call_service."""

    def __init__(self, pages=None, messages=None, error=None):
        self.pages = pages or {}
        self.messages = messages
        self.error = error
        self.calls = []

    def call_service(self, domain, service, data=None, return_response=False, timeout=None):
        uri = data["uri"]
        self.calls.append(uri)
        if self.error:
            raise self.error
        if "getMessagesForThread" in uri:
            return {"service_response": self.messages}
        page = int(uri.split("page=")[1])
        return {"service_response": self.pages.get(page, {"data": {"threads": []}})}


def threads_page(items, more=False):
    return {"status": {"code": 0}, "data": {"threads": items, "moreMessagesExist": more}}


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------


def test_thread_children_reads_display_names():
    assert thread_children(SHARED_THREAD) == [
        "Noah Emil Jensen",
        "Alma Jensen",
    ]


def test_thread_children_handles_missing_key():
    assert thread_children({"id": 1}) == []


def test_matches_child_by_aula_name():
    assert thread_matches_child(NOAH_THREAD, NOAH) is True
    assert thread_matches_child(NOAH_THREAD, ALMA) is False


def test_matches_child_falls_back_to_plain_name():
    # No aula_name configured: the child's own name must still match.
    assert thread_matches_child(NOAH_THREAD, Child("noah", "Noah")) is True


def test_matches_child_by_profile_id_takes_precedence():
    # Wrong name but right id -> still a match.
    child = Child("noah", "Nobody", "Nobody At All", "900001")
    assert thread_matches_child(NOAH_THREAD, child) is True


def test_profile_id_mismatch_is_not_a_match():
    child = Child("noah", "Noah", "Noah Emil Jensen", "123")
    assert thread_matches_child(NOAH_THREAD, child) is False


def test_shared_thread_matches_both_children():
    assert thread_matches_child(SHARED_THREAD, NOAH) is True
    assert thread_matches_child(SHARED_THREAD, ALMA) is True


def test_threads_for_child_filters_and_keeps_order():
    assert [t["id"] for t in threads_for_child(THREADS, NOAH)] == [1, 2]
    assert [t["id"] for t in threads_for_child(THREADS, ALMA)] == [2]


def test_unrelated_thread_is_excluded():
    assert OTHER_THREAD not in threads_for_child(THREADS, NOAH)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def test_fetch_threads_stops_when_no_more_pages():
    client = FakeClient({0: threads_page([NOAH_THREAD], more=False)})
    assert len(fetch_threads(client, pages=5)) == 1
    assert client.calls == ["?method=messaging.getThreads&sortOn=date&orderDirection=desc&page=0"]


def test_fetch_threads_follows_pagination():
    client = FakeClient(
        {
            0: threads_page([NOAH_THREAD], more=True),
            1: threads_page([SHARED_THREAD], more=True),
        }
    )
    assert [t["id"] for t in fetch_threads(client, pages=2)] == [1, 2]
    assert len(client.calls) == 2


def test_fetch_threads_raises_on_api_error_status():
    client = FakeClient({0: {"status": {"code": 403, "message": "Forbidden"}}})
    with pytest.raises(AulaApiError, match="403"):
        fetch_threads(client)


def test_fetch_threads_wraps_transport_errors():
    client = FakeClient(error=RuntimeError("boom"))
    with pytest.raises(AulaApiError, match="boom"):
        fetch_threads(client)


def test_fetch_thread_messages_normalises_and_strips_html():
    client = FakeClient(
        messages={
            "status": {"code": 0},
            "data": {
                "subject": "learning game from today",
                "messages": [
                    {
                        "id": "m2",
                        "messageType": "Message",
                        "sendDateTime": "2026-09-30T16:22:25+00:00",
                        "sender": {"fullName": "Freja Holm Mikkelsen"},
                        "text": {"html": "Hi Lars,<br /><br />It was a test."},
                    },
                    {
                        "id": "m1",
                        "messageType": "Message",
                        "sendDateTime": "2026-09-30T14:55:14+00:00",
                        "sender": {"fullName": "Lars Jensen"},
                        "text": {"html": "<div>Hey Freja</div>"},
                    },
                ],
            },
        }
    )
    messages = fetch_thread_messages(client, 100000001, limit=5)
    assert [m["id"] for m in messages] == ["m2", "m1"]
    assert messages[0]["sender"] == "Freja Holm Mikkelsen"
    assert messages[0]["title"] == "learning game from today"
    assert "<br" not in messages[0]["original_text"]
    assert messages[0]["original_text"].startswith("Hi Lars,")


def test_fetch_thread_messages_skips_non_message_types():
    client = FakeClient(
        messages={
            "data": {
                "subject": "s",
                "messages": [
                    {"messageType": "ThreadCreated", "text": {"html": "x"}},
                    {
                        "id": "m1",
                        "messageType": "Message",
                        "sendDateTime": "2026-09-30T14:55:14+00:00",
                        "sender": {"fullName": "A"},
                        "text": {"html": "real"},
                    },
                ],
            }
        }
    )
    messages = fetch_thread_messages(client, 1)
    assert len(messages) == 1
    assert messages[0]["original_text"] == "real"


def test_fetch_thread_messages_preserves_edited_message_among_recipient_changes():
    """Live Aula threads can contain only MessageEdited plus system entries."""
    edited = {
        "id": "edited-message", "messageType": "MessageEdited",
        "sendDateTime": "2026-10-20T14:55:14+00:00",
        "sender": {"fullName": "Teacher"},
        "text": {"html": "<p>Updated instructions. Bring a helmet at 9.</p>"},
    }
    client = FakeClient(messages={"data": {
        "subject": "Cykel tur",
        "messages": [
            {"id": "system3", "messageType": "RecipientsRemoved"},
            {"id": "system2", "messageType": "RecipientsAdded"},
            {"id": "system1", "messageType": "RecipientsRemoved"},
            edited,
        ],
        "firstMessage": edited, "totalMessageCount": 4, "moreMessagesExist": False,
    }})
    messages = fetch_thread_messages(client, 42)
    assert len(messages) == 1
    assert messages[0] == {
        "id": "edited-message", "title": "Cykel tur",
        "original_text": "Updated instructions. Bring a helmet at 9.",
        "sender": "Teacher", "date": "2026-10-20T14:55:14+00:00",
    }
    assert len(client.calls) == 1


def test_fetch_thread_messages_respects_limit():
    raw = [
        {
            "id": f"m{i}",
            "messageType": "Message",
            "sendDateTime": "2026-09-30T14:55:14+00:00",
            "sender": {"fullName": "A"},
            "text": {"html": f"body {i}"},
        }
        for i in range(10)
    ]
    client = FakeClient(messages={"data": {"subject": "s", "messages": raw}})
    assert len(fetch_thread_messages(client, 1, limit=3)) == 3


def test_message_from_thread_summary():
    msg = message_from_thread_summary(NOAH_THREAD)
    assert msg["title"] == "Ugebrev"
    assert msg["sender"] == "Freja Holm Mikkelsen"
    assert msg["original_text"] == "Body of Ugebrev"


# --------------------------------------------------------------------------
# AulaThreadSource
# --------------------------------------------------------------------------


def source_with(threads, messages=None):
    client = FakeClient({0: threads_page(threads, more=False)}, messages=messages)
    src = AulaThreadSource(client, pages=1, context_messages=5)
    src.refresh()
    return src, client


def test_source_serves_different_threads_per_child():
    """The whole point: Noah and Alma must not share one message."""
    messages = {"data": {"subject": "irrelevant", "messages": []}}
    src, _ = source_with(THREADS, messages)
    # Noah's newest thread is #1, Alma's only thread is #2.
    assert src.messages_for(NOAH)[0]["title"] == "Ugebrev"
    assert src.messages_for(ALMA)[0]["title"] == "Afslutning for skolegruppen"


def test_source_returns_empty_for_unknown_child():
    src, _ = source_with([OTHER_THREAD])
    assert src.messages_for(NOAH) == []


def test_source_falls_back_to_summary_when_thread_fetch_is_empty():
    src, _ = source_with([NOAH_THREAD], messages={"data": {"messages": []}})
    messages = src.messages_for(NOAH)
    assert len(messages) == 1
    assert messages[0]["original_text"] == "Body of Ugebrev"


def test_source_unavailable_when_api_fails():
    client = FakeClient(error=RuntimeError("down"))
    src = AulaThreadSource(client, pages=1)
    assert src.refresh() is False
    assert src.available is False


def test_empty_thread_list_is_success_not_unavailability():
    src, _ = source_with([])
    assert src.available is True


def test_missing_threads_list_is_an_error_not_an_empty_success():
    client = FakeClient({0: {"status": {"code": 0}, "data": {}}})
    with pytest.raises(AulaApiError, match="no threads list"):
        fetch_threads(client)


def test_unread_count_is_per_child():
    src, _ = source_with(THREADS)
    assert src.unread_count_for(NOAH) == 1
    assert src.unread_count_for(ALMA) == 0


class ReadClient:
    def __init__(self, data, post_status=0):
        self.data = data
        self.post_status = post_status
        self.posts = []

    def call_service(self, domain, service, data=None, return_response=False, timeout=None):
        assert "getMessagesForThread" in data["uri"]
        return {"service_response": {"status": {"code": 0}, "data": self.data}}

    def post(self, uri, body):
        self.posts.append((uri, body))
        return {"status": {"code": self.post_status}, "data": True}


def test_mark_thread_read_moves_marker_to_newest_message():
    from aula_ui.aula_api import mark_thread_read

    client = ReadClient({"mailBoxOwner": {"mailBoxOwnerType": "institutionProfile"},
                         "lastReadMessageId": "old", "messages": [
                             {"id": "event", "messageType": "RecipientAdded"},
                             {"id": "new", "messageType": "Message"}, {"id": "old"}]})
    assert mark_thread_read(client, "42") is True
    assert client.posts == [("?method=messaging.setLastReadMessage", {
        "threadId": 42, "messageId": "new", "commonInboxId": None, "otpInboxId": None})]


def test_mark_thread_read_skips_read_and_refuses_shared_inbox_or_rejection():
    from aula_ui.aula_api import mark_thread_read

    client = ReadClient({"lastReadMessageId": "new", "messages": [{"id": "new"}]})
    assert mark_thread_read(client, "42") is False and not client.posts
    client = ReadClient({"mailBoxOwner": {"mailBoxOwnerType": "commonInbox"}, "messages": [{"id": "new"}]})
    with pytest.raises(AulaApiError, match="shared inbox"):
        mark_thread_read(client, "42")
    assert not client.posts
    with pytest.raises(AulaApiError, match="did not accept"):
        mark_thread_read(ReadClient({"messages": [{"id": "new"}]}, post_status=1), "42")
    with pytest.raises(AulaApiError, match="own Aula login"):
        mark_thread_read(FakeClient(), "42")
