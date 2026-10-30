"""Bounded retention of calendar archive flags and event reviews in profiles.json."""

import json
from datetime import date

from aula_ui.calendar_feed import validate_events
from test_calendar_feed import EVENT, ITEM
from test_event_reviews import setup_calendar

TODAY = date(2026, 10, 30)


def review(status, source, start="", **extra):
    event = {"source_id": source, "title": "Trip", "start": start, "end": ""}
    return {"status": status, "event": event, "candidate": event, **extra}


def test_only_long_past_unrelated_calendar_state_is_pruned(tmp_path):
    profiles = setup_calendar(tmp_path).profiles
    profiles.data["event_reviews"] = {
        "old-declined": review("declined", "post:gone", "2025-01-10", is_recommendation=True),
        "old-superseded": review("superseded", "thread:gone", "2025-03-01T10:00:00+01:00"),
        "old-accepted": review("accepted", "post:gone", "2024-12-24"),
        "old-active-source": review("declined", "post:live", "2025-01-10"),
        "recent-declined": review("declined", "post:gone", "2026-06-01"),
        "future-pending": review("pending", "post:gone", "2027-02-01"),
        "known-uid": review("declined", "post:gone", "2025-01-10"),
        "dateless": review("superseded", "post:gone"),
    }
    profiles.data["archived_events"] = ["known-uid", "old-accepted", "orphan"]
    profiles.save()

    removed = profiles.prune_calendar_state({"known-uid"}, {"post:live"}, TODAY)

    assert removed == (0, 2)
    assert set(profiles.event_reviews()) == {
        "old-accepted", "old-active-source", "recent-declined", "future-pending", "known-uid", "dateless"}
    assert profiles.archived_events() == {"known-uid", "old-accepted", "orphan"}
    # Date-less entries start ageing now and go only once the retention has passed since.
    assert profiles.data["calendar_state_seen"] == {"dateless": "2026-10-30", "orphan": "2026-10-30"}
    assert profiles.prune_calendar_state({"known-uid"}, {"post:live"}, date(2027, 4, 1)) == (0, 1)
    assert profiles.prune_calendar_state({"known-uid"}, {"post:live"}, date(2027, 5, 1)) == (1, 1)
    assert profiles.archived_events() == {"known-uid", "old-accepted"}
    assert "dateless" not in profiles.event_reviews()
    assert profiles.data["calendar_state_seen"] == {}
    on_disk = json.loads(profiles.path.read_text())
    assert on_disk["archived_events"] == ["known-uid", "old-accepted"]
    assert set(on_disk["event_reviews"]) == set(profiles.event_reviews())


def test_daily_prune_never_changes_feed_or_ics(tmp_path, monkeypatch):
    controller = setup_calendar(tmp_path)
    event = validate_events([EVENT], ITEM)[0]
    controller.calendar.record(ITEM, [event])
    controller.publish_feed()
    uid = controller.calendar.recommendations() and controller.calendar.recommendations()[0]["uid"]
    uid = uid or controller.calendar.snapshot_events()[0]["uid"]
    controller.calendar.review(uid, "accept")
    controller.calendar.set_archived(uid, True)
    profiles = controller.profiles
    # Long-past leftovers from sources that no longer exist, plus one still-relevant decision.
    profiles.save_event_review("stale", review("declined", "post:gone", "2025-02-01", is_recommendation=True))
    profiles.save_event_review("live", review("declined", ITEM["id"], "2025-02-01", is_recommendation=True))
    profiles.set_event_archived("stale-archived", True)
    profiles.data["calendar_state_seen"] = {"stale-archived": "2025-01-01"}
    controller.publish_feed()
    before = (controller.calendar.content, controller.calendar.snapshot_events(),
              controller.calendar.snapshot_events(True), controller.feed_json)

    controller.prune_calendar_state()

    assert "stale" not in profiles.event_reviews() and "live" in profiles.event_reviews()
    assert profiles.archived_events() == {uid}
    controller.publish_feed()
    assert (controller.calendar.content, controller.calendar.snapshot_events(),
            controller.calendar.snapshot_events(True), controller.feed_json) == before
    # Runs at most once per day.
    profiles.save_event_review("stale-again", review("declined", "post:gone", "2025-02-01"))
    controller.prune_calendar_state()
    assert "stale-again" in profiles.event_reviews()


def test_prune_is_skipped_until_events_are_recognized(tmp_path):
    controller = setup_calendar(tmp_path)
    controller.profiles.save_event_review("stale", review("declined", "post:gone", "2025-02-01"))
    controller.prune_calendar_state()
    assert "stale" in controller.profiles.event_reviews()
