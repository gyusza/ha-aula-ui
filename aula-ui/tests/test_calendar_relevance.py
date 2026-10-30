from datetime import date

import pytest

from aula_ui.calendar_feed import retrospective_reason, similar_calendar_event
from test_event_reviews import setup_calendar


@pytest.mark.parametrize("evidence", [
    "I dag var vi på tur til skoven den 28. oktober.",
    "Børnene har været på cykeltur den 28. oktober.",
    "We went on a lovely bike trip on 28 October.",
    "The children enjoyed the museum on 28 October.",
    "Tak for i dag den 28. oktober.",
])
def test_retrospective_reports_are_not_new_events(evidence):
    assert retrospective_reason({"evidence": evidence, "start": ""})


def test_past_dates_and_future_parts_of_mixed_reports():
    assert retrospective_reason({"start": "2026-10-25"}, today=date(2026, 10, 26))
    assert not retrospective_reason({
        "start": "2026-10-27", "evidence": "Tomorrow we will visit the museum.",
    }, today=date(2026, 10, 26))
    assert not retrospective_reason({
        "start": "2026-10-26", "evidence": "Join us today for the trip.",
    }, today=date(2026, 10, 26))


def test_recommendation_date_checks_use_central_timezone(tmp_path, monkeypatch):
    from dataclasses import replace

    controller = setup_calendar(tmp_path)
    controller.config = replace(controller.config, timezone="America/New_York")
    zones = []

    def today(zone):
        zones.append(zone)
        return date(2026, 10, 1)

    monkeypatch.setattr("aula_ui.calendar_feed.calendar_today", today)
    item = {"id": "post:central", "title": "Trip", "date": "2026-10-01", "child_slugs": ["noah"],
            "original_text": "We will take a bike trip on 5 October."}
    controller.calendar.recommend(item, {
        "title": "Bike trip", "start": "2026-10-05", "end": "2026-10-06",
        "all_day": True, "evidence": item["original_text"], "location": "",
    }, "Check date")
    controller.calendar.prune_weak_reviews()
    assert zones and set(zones) == {"America/New_York"}


@pytest.mark.parametrize("title", ["Bike trip to Bakkeskoven", "Bakkeskoven cykel tur", "Bakkeskoven bicycle trip reminder"])
def test_existing_translated_occurrence_matches(title):
    candidate = {"title": title, "start": "2026-11-05", "child_slugs": ["alma"]}
    existing = {"title": "Bakkeskoven bicycle trip", "start": "2026-11-05T09:00:00+01:00",
                "child_slugs": ["alma"]}
    assert similar_calendar_event(candidate, existing)
    assert not similar_calendar_event({**candidate, "start": "2026-11-06"}, existing)
    assert not similar_calendar_event({**candidate, "child_slugs": ["noah"]}, existing)
    assert not similar_calendar_event({**candidate, "title": "Parents meeting"}, existing)


def test_matching_occurrence_is_suppressed_and_pending_reviews_are_cleaned(tmp_path, monkeypatch):
    controller = setup_calendar(tmp_path)
    monkeypatch.setattr("aula_ui.calendar_feed.calendar_today", lambda *args: date(2026, 10, 26))
    item = {"id": "thread:trip", "title": "Trip", "date": "2026-10-26", "child_slugs": ["noah"],
            "original_text": "We will take a bike trip to Bakkeskoven on 5 November."}
    candidate = {"title": "Bike trip to Bakkeskoven", "start": "2026-11-05", "end": "2026-11-06",
                 "all_day": True, "evidence": item["original_text"], "location": ""}
    controller.calendar.recommend(item, candidate, "Check date")
    assert len(controller.calendar.recommendations()) == 1
    native = {**candidate, "title": "Bakkeskoven bicycle trip", "source_id": "event:official",
              "source_title": "Aula", "source_date": "2026-10-26", "child_slugs": ["noah"]}
    controller.calendar.record({"id": "event:official"}, [native])
    controller.calendar.prune_weak_reviews()
    assert not controller.calendar.recommendations()
    controller.calendar.recommend({**item, "id": "post:reminder"}, candidate, "Check date")
    assert not controller.calendar.recommendations()
    assert len(controller.calendar.snapshot_events()) == 1


def test_cleanup_keeps_approved_events_even_when_they_are_past(tmp_path, monkeypatch):
    controller = setup_calendar(tmp_path)
    item = {"id": "post:trip", "title": "Trip", "date": "2026-10-01", "child_slugs": ["noah"],
            "original_text": "We will take a bike trip on 5 October."}
    candidate = {"title": "Bike trip", "start": "2026-10-05", "end": "2026-10-06",
                 "all_day": True, "evidence": item["original_text"], "location": ""}
    controller.calendar.recommend(item, candidate, "Check date")
    uid = controller.calendar.recommendations()[0]["uid"]
    controller.calendar.review(uid, "accept")
    controller.calendar.recommend({**item, "id": "post:unapproved"}, {
        **candidate, "title": "Museum visit", "start": "2026-10-09",
    }, "Check date")
    monkeypatch.setattr("aula_ui.calendar_feed.calendar_today", lambda *args: date(2026, 10, 26))
    controller.calendar.prune_weak_reviews()
    assert not controller.calendar.recommendations()
    assert controller.calendar.snapshot_events()[0]["uid"] == uid
