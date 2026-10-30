"""Official Aula dates coexist with planning data and reviewed AI guesses."""

import json

import pytest
import requests

from aula_ui.aula_api import AulaApiError
from aula_ui.direct_source import DirectAulaClient
from aula_ui.calendar_feed import validate_events
from test_direct_account import CHILDREN, Session, controller, install, payload, source_handlers


NATIVE = {
    "id": 91, "title": "Parents' meeting",
    "startDateTime": "2026-11-09T18:00:00+01:00",
    "endDateTime": "2026-11-09T20:00:00+01:00",
    "primaryResource": {"name": "Main hall"},
    "description": {"html": "<p>Please bring your questions.</p>"},
    "creator": {"fullName": "Class teacher"},
}


class CalendarSession(Session):
    def __init__(self, *responses):
        super().__init__(*responses)
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("Csrfp-Token", "calendar-session-csrf", domain="www.aula.dk", path="/")

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_native_calendar_uses_child_institution_profiles_and_retains_source_fields(controller):
    install(controller)
    session = CalendarSession(payload([NATIVE]), payload([NATIVE]), payload([]))
    source = DirectAulaClient(controller.account.connection, session)
    source.context = {"institutionProfile": {"id": 2001}}
    events = source.native_events(CHILDREN[0], lookahead_days=15)
    assert len(session.calls) == 3 and len(events) == 1
    for _, request in session.calls:
        assert ("method", "calendar.getEventsByProfileIdsAndResourceIds") in request["params"]
        assert request["json"]["instProfileIds"] == [1001]
        assert request["json"]["resourceIds"] == []
        assert request["headers"]["csrfp-token"] == "calendar-session-csrf"
        assert request["headers"]["Content-Type"] == "application/json"
    event = events[0]
    assert event["timestamp"] == NATIVE["startDateTime"]
    assert event["end"] == NATIVE["endDateTime"]
    assert event["location"] == "Main hall"
    assert event["sender"] == "Class teacher"
    assert event["description"] == "Please bring your questions."
    assert event["calendar_source"] == "aula"


def test_native_calendar_handles_all_day_and_rejects_invalid_responses(controller):
    install(controller)
    native = DirectAulaClient._native_event({
        **NATIVE, "allDay": True, "startDateTime": "2026-11-09T00:00:00+01:00",
        "endDateTime": "2026-11-10T00:00:00+01:00", "location": {"name": "Classroom"},
    })
    assert native["timestamp"] == "2026-11-09"
    assert native["end"] == "2026-11-10"
    assert native["time"] is None and native["location"] == "Classroom"
    source = DirectAulaClient(controller.account.connection, CalendarSession(payload({"events": []})))
    source.context = {"institutionProfile": {"id": 2001}}
    with pytest.raises(AulaApiError, match="invalid native calendar response"):
        source.native_events(CHILDREN[0])
    with pytest.raises(AulaApiError, match="invalid date"):
        DirectAulaClient._native_event({**NATIVE, "startDateTime": "not a date"})


def test_lessons_use_structured_metadata_not_titles_or_classrooms():
    lesson = {**NATIVE, "title": "BHK", "primaryResource": {"name": "H021"}, "type": "lesson"}
    assert DirectAulaClient._native_event(lesson)["calendar_kind"] == "lesson"
    assert DirectAulaClient._native_event({**lesson, "type": None})["calendar_kind"] == ""
    assert DirectAulaClient._native_event({
        **lesson, "type": None, "lesson": {"lessonId": "source-lesson-id"},
    })["calendar_kind"] == "lesson"
    assert DirectAulaClient._native_event({
        **lesson, "title": "Parents' meeting", "type": "school_home_meeting",
    })["calendar_kind"] == "school_home_meeting"


def test_lesson_classification_reaches_feed_and_survives_parent_corrections(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    monkeypatch.setattr(controller.account.source, "native_events", lambda child, days: [
        DirectAulaClient._native_event({**NATIVE, "title": "BHK", "type": "lesson"}),
    ])
    controller.poll_cycle()
    event = next(event for event in controller.calendar.snapshot_events() if event["title"] == "BHK")
    assert event["calendar_kind"] == "lesson"
    controller.calendar.review(event["uid"], "edit", {
        "title": event["title"], "start": event["start"], "end": event["end"],
        "all_day": event["all_day"], "location": event["location"],
    })
    controller.publish_feed()
    item = next(item for item in json.loads(controller.feed_json)["items"]
                if item.get("calendar_event", {}).get("uid") == event["uid"])
    assert item["calendar_event"]["calendar_kind"] == "lesson"
    assert item["content_type"] == "Calendar"


def test_only_exact_easyiq_lesson_counterparts_share_lesson_filter(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    child = controller.config.children[0]
    native = DirectAulaClient._native_event({**NATIVE, "title": "BHK", "type": "lesson"})
    monkeypatch.setattr(controller.account.source, "native_events", lambda *args: [native])
    controller.store.child(child.slug)["events"] = [
        {**native, "id": "planning", "origin": "Weekplan", "calendar_source": "easyiq",
         "timestamp": "2026-11-09T17:00:00Z"},
        {**native, "id": "different-time", "origin": "Weekplan", "calendar_source": "easyiq",
         "timestamp": "2026-11-09T18:00:00Z"},
        {**native, "id": "different-title", "title": "Other", "origin": "Weekplan",
         "calendar_source": "easyiq"},
    ]
    controller.process_events(child)
    events = {event["id"]: event for event in controller.store.child(child.slug)["events"]}
    assert events["planning"]["calendar_kind"] == "lesson"
    assert events["different-time"]["calendar_kind"] == ""
    assert events["different-title"]["calendar_kind"] == ""


def test_restored_tokens_bootstrap_guardian_context_before_calendar_post(controller):
    install(controller)
    session = CalendarSession(payload({"institutionProfile": {"id": 2001}}), payload([NATIVE]))
    source = DirectAulaClient(controller.account.connection, session)
    events = source.native_events(CHILDREN[0], lookahead_days=7)
    assert len(events) == 1
    assert ("method", "profiles.getProfileContext") in session.calls[0][1]["params"]
    assert ("portalrole", "guardian") in session.calls[0][1]["params"]
    assert "json" not in session.calls[0][1]
    assert session.calls[1][1]["headers"]["csrfp-token"] == "calendar-session-csrf"


@pytest.mark.parametrize(("start", "end", "expected"), [
    ("2026-11-09T00:00:00+01:00", "2026-11-09T00:00:00+01:00", "2026-11-10"),
    ("2026-11-09T00:00:00+01:00", "2026-11-09T23:59:59+01:00", "2026-11-10"),
    ("2026-11-09T00:00:00+01:00", "2026-11-11T23:59:59+01:00", "2026-11-12"),
    ("2026-11-09T00:00:00+01:00", "2026-11-12T00:00:00+01:00", "2026-11-12"),
    ("2026-11-09", "2026-11-09", "2026-11-10"),
])
def test_native_all_day_inclusive_dates_become_exclusive_without_extending_midnight_end(start, end, expected):
    native = DirectAulaClient._native_event({
        **NATIVE, "allDay": True, "startDateTime": start, "endDateTime": end,
    })
    assert native["end"] == expected
    item = {"id": "event:child:official", "title": native["title"], "date": native["timestamp"],
            "child_slugs": ["child"], "original_text": native["title"]}
    checked = validate_events([{
        "title": native["title"], "start": native["timestamp"], "end": native["end"],
        "location": native["location"], "all_day": True, "evidence": native["title"],
    }], item)[0]
    assert checked["end"] == expected and checked["origin"] == "aula"


def test_calendar_permission_failure_is_explicit_and_does_not_refresh_or_clear_cache(controller, monkeypatch):
    from test_direct_account import Response

    install(controller)
    session = CalendarSession(Response({}, 403))
    source = DirectAulaClient(controller.account.connection, session)
    source.context = {"institutionProfile": {"id": 2001}}
    original = controller.account.connection.access_token

    def no_refresh(force=False):
        assert not force, "Permission errors must not trigger token rotation"
        return original()

    monkeypatch.setattr(controller.account.connection, "access_token", no_refresh)
    with pytest.raises(AulaApiError, match="denied calendar access.*HTTP 403"):
        source.native_events(CHILDREN[0], lookahead_days=7)
    assert len(session.calls) == 1


def test_official_calendar_is_collected_for_daycare_and_needs_no_approval(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    for profile in controller.profiles.snapshot():
        controller.profiles.update(profile["slug"], {
            "display_name": profile["display_name"], "institution_type": "daycare",
            "calendar_enabled": True,
        })
    requested = []

    def native(child, lookahead_days):
        requested.append(child["profile_id"])
        return [DirectAulaClient._native_event(NATIVE)]

    monkeypatch.setattr(controller.account.source, "native_events", native)
    monkeypatch.setattr(controller.account.source, "easyiq_events",
                        lambda *args, **kwargs: pytest.fail("Daycare must not need EasyIQ"))
    controller.poll_cycle()
    assert requested == ["101", "102"]
    assert controller.calendar.count == 1
    assert not controller.calendar.recommendations()
    official = controller.calendar.snapshot_events()[0]
    assert official["origin"] == "aula" and not official["review_required"]
    assert official["end"] == "2026-11-09T19:00:00+00:00"
    assert official["location"] == "Main hall"
    assert official["source_author"] == "Class teacher"
    assert NATIVE["startDateTime"] in official["evidence"]
    assert "Please bring your questions." in official["source_excerpt"]
    items = json.loads(controller.feed_json)["items"]
    assert any(item.get("calendar_event", {}).get("origin") == "aula" for item in items)


def test_native_calendar_failure_retains_cached_official_events(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    monkeypatch.setattr(controller.account.source, "native_events",
                        lambda child, days: [DirectAulaClient._native_event(NATIVE)])
    controller.poll_cycle()
    uid = next(event["uid"] for event in controller.calendar.snapshot_events()
               if event["title"] == NATIVE["title"])

    def fail(child, days):
        raise AulaApiError("Native calendar temporarily unavailable.")

    monkeypatch.setattr(controller.account.source, "native_events", fail)
    controller.poll_cycle()
    assert uid in {event["uid"] for event in controller.calendar.snapshot_events()}
    assert any("Native calendar temporarily unavailable" in error
               for error in json.loads(controller.feed_json)["errors"])


def test_successful_native_sync_removes_cancelled_official_events(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    monkeypatch.setattr(controller.account.source, "native_events",
                        lambda child, days: [DirectAulaClient._native_event(NATIVE)])
    controller.poll_cycle()
    assert any(event["title"] == NATIVE["title"] for event in controller.calendar.snapshot_events())
    monkeypatch.setattr(controller.account.source, "native_events", lambda child, days: [])
    controller.poll_cycle()
    assert all(event["title"] != NATIVE["title"] for event in controller.calendar.snapshot_events())
