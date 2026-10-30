"""Tests for calendar event collection and classification."""

from aula_ui.events import (
    classify_event_type,
    collect_events,
    dedupe_and_sort,
    make_event_id,
    normalise_calendar_event,
    normalise_weekplan_event,
    slugify,
)


class FakeClient:
    def __init__(self, events_by_entity):
        self._events = events_by_entity
        self.calls = []

    def get_calendar_events(self, entity_id, start=None, end=None, lookahead_days=21):
        self.calls.append(entity_id)
        return self._events.get(entity_id, [])


def test_classify_event_type_danish_and_english():
    assert classify_event_type("Forældremøde 4.B") == "meeting"
    assert classify_event_type("Parent-teacher night") == "meeting"
    assert classify_event_type("Sommerfest") == "party"
    assert classify_event_type("Skole-hjem-samtale") == "talk"
    assert classify_event_type("Terminsprøve i matematik") == "exam"
    assert classify_event_type("Math exam") == "exam"
    assert classify_event_type("Lektier til fredag") == "homework"
    assert classify_event_type("Idræt") == "other"
    assert classify_event_type("") == "other"


def test_classify_event_type_uses_description():
    assert classify_event_type("Besked", "Husk aflevering på fredag") == "homework"


def test_slugify_transliterates_nordic_characters():
    assert slugify("Forældremøde") == "foraeldremoede"
    assert slugify("Skole-hjem-samtale") == "skole_hjem_samtale"
    assert slugify("  Ø  ") == "oe"
    assert slugify("") == ""


def test_make_event_id_is_slug_safe():
    event_id = make_event_id("Forældremøde 4.B", "2026-10-12")
    assert event_id == "2026_10_12_foraeldremoede_4_b"
    assert make_event_id("", "") == "nodate_event"


def test_normalise_calendar_event_timed():
    event = normalise_calendar_event(
        {
            "summary": "Forældremøde",
            "start": {"dateTime": "2026-10-12T18:00:00+02:00"},
            "end": {"dateTime": "2026-10-12T20:00:00+02:00"},
        }
    )
    assert event == {
        "id": "2026_10_12_foraeldremoede",
        "title": "Forældremøde",
        "date": "2026-10-12",
        "time": "18:00",
        "type": "meeting",
        "description": "",
        "origin": "Calendar",
        "timestamp": "2026-10-12T18:00:00+02:00",
        "end": "2026-10-12T20:00:00+02:00",
        "location": "",
        "sender": "Aula calendar",
    }


def test_normalise_calendar_event_all_day_has_no_time():
    event = normalise_calendar_event({"summary": "Skolefest", "start": {"date": "2026-11-01"}})
    assert event["date"] == "2026-11-01"
    assert event["time"] is None
    assert event["type"] == "party"


def test_normalise_calendar_event_rejects_empty():
    assert normalise_calendar_event({}) is None
    assert normalise_calendar_event("nope") is None


def test_normalise_weekplan_event_uses_easyiq_fields():
    event = normalise_weekplan_event(
        {
            "courses": "Matematik",
            "activities": "Aflevering af opgave 3",
            "start": "2026-10-02T08:15:00",
        }
    )
    assert event["title"] == "Matematik"
    assert event["date"] == "2026-10-02"
    assert event["time"] == "08:15"
    assert event["type"] == "homework"


def test_dedupe_and_sort_orders_chronologically():
    events = [
        {"id": "b", "date": "2026-10-12", "time": "18:00", "title": "B"},
        {"id": "a", "date": "2026-10-01", "time": None, "title": "A"},
        {"id": "b", "date": "2026-10-12", "time": "18:00", "title": "B dup"},
        {"id": "c", "date": "2026-10-12", "time": "09:00", "title": "C"},
    ]
    assert [e["id"] for e in dedupe_and_sort(events)] == ["a", "c", "b"]


def test_collect_events_merges_calendars():
    client = FakeClient(
        {
            "calendar.weekplan": [
                {"summary": "Matematik", "start": {"dateTime": "2026-10-02T08:15:00+02:00"}}
            ],
            "calendar.homework": [
                {"summary": "Aflevering dansk", "start": {"date": "2026-10-03"}}
            ],
        }
    )
    events = collect_events(client, ["calendar.weekplan", "calendar.homework"], None)
    assert [e["title"] for e in events] == ["Matematik", "Aflevering dansk"]
    assert client.calls == ["calendar.weekplan", "calendar.homework"]


def test_collect_events_falls_back_to_weekplan_sensor():
    client = FakeClient({})
    weekplan_state = {
        "attributes": {
            "weekplan_summary": {
                "events": [
                    {"courses": "Idræt", "start": "2026-10-05T10:00:00"},
                    {"courses": "Forældremøde", "start": "2026-10-12T18:00:00"},
                ]
            }
        }
    }
    events = collect_events(client, ["calendar.empty"], weekplan_state)
    assert [e["type"] for e in events] == ["other", "meeting"]


def test_collect_events_returns_empty_when_nothing_available():
    assert collect_events(FakeClient({}), [], None) == []
    assert collect_events(FakeClient({}), [], {"attributes": {}}) == []
