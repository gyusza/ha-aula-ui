import json
from types import SimpleNamespace

import pytest

from aula_ui.calendar_feed import CalendarFeed, build_ics, event_uid, validate_events
from aula_ui.llm import HomeAssistantAITaskProvider, LLMError
from aula_ui.profiles import Profiles
from test_controller import TWO_CHILDREN, FakeHA, build_controller


ITEM = {
    "id": "post:10", "title": "School news", "date": "2026-10-02T08:00:00Z",
    "original_text": "Friday 9 October is pyjama day. We meet at 10:00 in the school hall.",
    "child_slugs": ["noah"], "children": ["Noah"],
}
EVENT = {
    "title": "Pyjama day", "start": "2026-10-09", "end": "",
    "all_day": True, "location": "", "evidence": "Friday 9 October is pyjama day.",
}
PROFILES = [{
    "slug": "noah", "display_name": "Noah", "calendar_enabled": True,
}, {
    "slug": "alma", "display_name": "Alma", "calendar_enabled": True,
}]


def test_all_day_dates_and_grounded_evidence():
    result = validate_events([EVENT], ITEM)
    assert result[0]["end"] == "2026-10-10"
    assert result[0]["source_id"] == ITEM["id"]
    with pytest.raises(ValueError, match="quote"):
        validate_events([{**EVENT, "evidence": "Made-up source"}], ITEM)
    with pytest.raises(ValueError):
        validate_events([{**EVENT, "start": "2026-02-31"}], ITEM)
    with pytest.raises(ValueError, match="later"):
        validate_events([{**EVENT, "end": "2026-10-08"}], ITEM)
    with pytest.raises(ValueError):
        validate_events("[]", ITEM)


def test_local_times_become_utc_and_unknown_end_is_not_invented():
    raw = {**EVENT, "start": "2026-10-09T10:00:00", "all_day": False}
    event = validate_events([raw], ITEM)[0]
    assert event["start"] == "2026-10-09T08:00:00+00:00"
    assert event["end"] == ""
    content, count = build_ics([event], PROFILES)
    assert b"DTSTART:20261009T080000Z" in content
    assert b"DTEND" not in content
    assert count == 1


def test_ics_deduplication_child_exclusion_escaping_and_line_folding():
    first = validate_events([EVENT], ITEM)[0]
    second = {**first, "child_slugs": ["alma"], "source_title": "Another source"}
    content, count = build_ics([first, second], PROFILES)
    assert count == 1
    assert content.count(b"BEGIN:VEVENT") == 1
    assert b"Alma\\, Noah" in content
    assert b"DTSTART;VALUE=DATE:20261009" in content
    assert b"DTEND;VALUE=DATE:20261010" in content
    assert content.endswith(b"END:VCALENDAR\r\n")
    assert all(len(line) <= 75 for line in content.split(b"\r\n"))
    unusual = {**first, "title": "æ" * 100 + ",;\nInjected:bad"}
    content, _ = build_ics([unusual], PROFILES)
    assert all(len(line) <= 75 for line in content.split(b"\r\n"))
    unfolded = content.replace(b"\r\n ", b"").decode()
    assert "\\,\\;\\nInjected:bad" in unfolded
    none, count = build_ics([first], [{**PROFILES[0], "calendar_enabled": False}])
    assert count == 0 and b"BEGIN:VEVENT" not in none
    unassigned, count = build_ics([{**first, "child_slugs": []}], PROFILES)
    assert count == 1 and b"Family (child unassigned)" in unassigned


def test_profile_settings_persist_validate_and_do_not_change_aula_identity(tmp_path):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    profiles = controller.profiles
    original_name = profiles.snapshot()[0]["aula_name"]
    settings = {
        "display_name": "Jo", "institution_type": "daycare",
        "calendar_enabled": False,
    }
    profiles.update("alma", settings)
    restored = Profiles(controller.config)
    assert restored.snapshot()[0]["display_name"] == "Jo"
    assert restored.snapshot()[0]["aula_name"] == original_name
    assert restored.calendar_path == profiles.calendar_path
    for invalid in [{**settings, "timezone": "Europe/Copenhagen"}, {**settings, "calendar_enabled": "yes"},
                    {**settings, "display_name": ""}, {**settings, "institution_type": "other"}]:
        with pytest.raises(ValueError):
            profiles.update("alma", invalid)
    with pytest.raises(ValueError):
        profiles.update("unknown", settings)


def test_central_timezone_migrates_all_saved_children_and_preserves_profile_data(tmp_path):
    from dataclasses import replace

    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    profiles = controller.profiles
    token = profiles.calendar_path
    profiles.data["children"] = {
        "alma": {"display_name": "Jo", "institution_type": "daycare",
                    "calendar_enabled": False, "timezone": "America/New_York"},
        "noah": {"timezone": "Invalid/Legacy"},
        "not_currently_discovered": {"display_name": "Saved", "timezone": "Asia/Tokyo"},
    }
    profiles.set_event_archived("existing-uid", True)
    profiles.save()
    # A restored backup and remembered/discovered children use the admin value,
    # not their historical per-child override (including an invalid old zone).
    config = replace(controller.config, children=(), timezone="Pacific/Auckland")
    restored = Profiles(config)
    assert len(restored.children) == 2
    assert {profile["timezone"] for profile in restored.snapshot()} == {"Pacific/Auckland"}
    assert restored.snapshot()[0]["display_name"] == "Jo"
    assert not restored.snapshot()[0]["calendar_enabled"]
    assert restored.archived_events() == {"existing-uid"}
    assert restored.calendar_path == token
    saved = json.loads(restored.path.read_text())
    assert all("timezone" not in value for value in saved["children"].values())
    restored.update("alma", {
        "display_name": "Updated", "institution_type": "school", "calendar_enabled": True,
    })
    restored.children = (*restored.children, replace(restored.children[0], slug="new_child"))
    assert {profile["timezone"] for profile in restored.snapshot()} == {"Pacific/Auckland"}
    assert {profile["timezone"] for profile in Profiles(config).snapshot()} == {"Pacific/Auckland"}


def test_profile_timezone_override_is_explicitly_rejected(tmp_path):
    profiles = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN).profiles
    previous = profiles.snapshot()
    with pytest.raises(ValueError, match="Home Assistant add-on configuration"):
        profiles.update("alma", {
            "display_name": "Jo", "institution_type": "school",
            "calendar_enabled": True, "timezone": "America/New_York",
        })
    assert profiles.snapshot() == previous


def test_native_event_local_times_use_central_timezone_for_every_child(tmp_path):
    controller = build_controller(
        tmp_path, FakeHA(), children=TWO_CHILDREN, timezone="America/New_York",
    )
    try:
        for child in controller.config.children:
            controller.store.child(child.slug)["events"] = [{
                "id": child.slug, "title": child.name + " meeting",
                "description": "", "date": "2026-10-09", "time": "10:00",
                "type": "meeting", "origin": "Calendar",
            }]
        controller.feed.update()
        events = controller.calendar.snapshot_events()
        assert len(events) == 2
        assert {event["start"] for event in events} == {"2026-10-09T14:00:00+00:00"}
    finally:
        controller.close()


def test_atomic_publication_stable_uids_and_link_revocation(tmp_path):
    controller = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir()
    calendar = CalendarFeed(controller, str(directory))
    native = {**ITEM, "id": "event:noah:native"}
    event = validate_events([EVENT], native)
    calendar.record(native, event)
    calendar.publish()
    assert not calendar.error
    first_path = directory / controller.profiles.calendar_path.rsplit("/", 1)[-1]
    content = first_path.read_bytes()
    uid = next(line for line in content.splitlines() if line.startswith(b"UID:"))
    calendar.publish()
    assert uid in first_path.read_bytes()
    calendar.rotate()
    assert not first_path.exists()
    next_path = directory / controller.profiles.calendar_path.rsplit("/", 1)[-1]
    assert next_path.exists() and uid in next_path.read_bytes()
    controller.store.save()
    restored = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    assert restored.calendar.snapshot_events() == [
        {**entry, "uid": event_uid(entry), "source_ids": [entry["source_id"]]} for entry in event
    ]
    calendar.record(native, [])
    calendar.publish()
    assert calendar.count == 0


def test_default_ai_extracts_in_same_request_and_uses_publication_date():
    client = FakeHA(service_responses={"ai_task.generate_data": {
        "data": {"overview": "Pyjama day is Friday.", "is_translated": False,
                 "events_json": json.dumps([EVENT])}
    }})
    provider = HomeAssistantAITaskProvider(client)
    summary, translated, events = provider.analyse(ITEM, "Noah", "Europe/Copenhagen")
    assert summary == "Pyjama day is Friday."
    assert events == [EVENT] and not translated
    assert len(client.service_calls) == 1
    request = client.service_calls[0][2]
    assert "entity_id" not in request
    assert "2026-10-02T08:00:00Z" in request["instructions"]
    assert "events_json" in request["structure"]
    bad = FakeHA(service_responses={"ai_task.generate_data": {
        "data": {"overview": "Summary.", "events_json": "not json"}
    }})
    with pytest.raises(LLMError, match="events_json"):
        HomeAssistantAITaskProvider(bad).analyse(ITEM, "Noah", "Europe/Copenhagen")


def test_feed_analysis_records_events_and_replaces_on_changed_content(tmp_path, monkeypatch):
    from test_feed import PostsClient, raw_post

    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    client = PostsClient({("21", 0): ([raw_post(1, date=ITEM["date"])], False)})
    client.pages[("21", 0)][0][0]["content"]["html"] = ITEM["original_text"]
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[])

    class Provider:
        name = "ha_ai_task"
        calls = 0

        def analyse(self, item, child_name, zone):
            self.calls += 1
            return "Entire summary.", False, [EVENT] if self.calls == 1 else []

        def generate(self, prompt):
            return json.dumps({"translated_text": "School news", "source_language": "en"})

    controller.provider = Provider()
    controller.feed.update()
    assert controller.calendar.recommendations()[0]["start"] == "2026-10-09"
    assert not controller.calendar.snapshot_events()
    assert controller.calendar.count == 0
    controller.feed.update()
    assert controller.provider.calls == 1
    client.pages[("21", 0)][0][0]["content"]["html"] = "Updated source with no event"
    controller.feed.update()
    assert controller.provider.calls == 2
    assert not controller.calendar.snapshot_events()
    assert not controller.calendar.recommendations()


def test_bad_event_candidate_does_not_discard_summary_or_block_other_posts(tmp_path, monkeypatch):
    from test_feed import PostsClient, raw_post

    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    client = PostsClient({("21", 0): ([raw_post(1), raw_post(2)], False)})
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[])

    class Provider:
        name = "ha_ai_task"
        calls = 0

        def analyse(self, item, child_name, zone):
            self.calls += 1
            return "Full useful summary.", True, [EVENT]

        def generate(self, prompt):
            return json.dumps({"translated_text": "School update", "source_language": "en"})

    controller.provider = Provider()
    controller.feed.update()
    if controller.feed.pending:
        controller.feed.enrich()
    assert controller.provider.calls == 2
    items = [item for item in json.loads(controller.feed_json)["items"] if item["content_type"] == "Post"]
    assert all(item["summary"] == "Full useful summary." for item in items)
    for item in items:
        # A candidate whose evidence is absent from the source is dropped, not suggested.
        assert not item["recognition_warning"]
        assert not item.get("recognized_events")
        assert not controller.calendar.recommendations()
    assert not controller.feed.retry_delay
    assert not controller.calendar.snapshot_events()


def test_existing_summary_remains_visible_during_event_backfill_failure(tmp_path, monkeypatch):
    import hashlib
    from test_feed import PostsClient, raw_post

    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    client = PostsClient({("21", 0): ([raw_post(1)], False)})
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[])
    controller.store.set("feed_summaries", {"post:1": {
        "fingerprint": hashlib.sha256(b"A postSchool update").hexdigest(),
        "summary": "The entire existing summary.", "is_translated": True,
        "source": "ha_ai_task",
    }})

    class Provider:
        name = "ha_ai_task"

        def analyse(self, item, child_name, zone):
            raise LLMError("Temporary provider error")

    controller.provider = Provider()
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert item["summary"] == "The entire existing summary."
    assert item["analysis_pending"]
    assert controller.feed.retry_delay > 0


def test_native_calendar_dates_join_ics(tmp_path):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    controller.store.child("noah")["events"] = [{
        "id": "native1", "title": "Homework", "description": "Read chapter 2",
        "date": "2026-10-05", "time": None, "type": "homework", "origin": "Calendar",
    }]
    controller.feed.update()
    events = controller.calendar.snapshot_events()
    assert len(events) == 1 and events[0]["start"] == "2026-10-05"


def test_archive_excludes_duplicates_persists_and_restores(tmp_path):
    controller = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir()
    controller.calendar = CalendarFeed(controller, str(directory))
    event = validate_events([EVENT], ITEM)[0]
    duplicate = {**event, "source_id": "post:other", "child_slugs": ["alma"]}
    controller.calendar.record(ITEM, [event])
    controller.calendar.record({"id": "post:other"}, [duplicate])
    controller.calendar.review(event_uid(event), "accept")
    controller.store.set("feed", [{**ITEM, "recognized_events": [event]}])
    controller.publish_feed()
    assert controller.calendar.count == 1
    uid = event_uid(event)
    controller.calendar.set_archived(uid, True)
    controller.publish_feed()
    assert controller.calendar.count == 0
    assert b"BEGIN:VEVENT" not in controller.calendar.content
    assert not controller.calendar.snapshot_events()
    assert len(controller.calendar.snapshot_events(archived=True)) == 1
    feed = json.loads(controller.feed_json)
    assert feed["items"][0]["title"] == ITEM["title"]
    assert feed["items"][0]["recognized_events"] == []
    # Reanalysis and a repeated click cannot bring an archived event back.
    controller.calendar.record(ITEM, [event])
    controller.calendar.set_archived(uid, True)
    controller.store.save()
    restored = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    restored.calendar = CalendarFeed(restored, str(directory))
    restored.publish_feed()
    assert restored.calendar.count == 0
    assert uid in restored.profiles.archived_events()
    restored.calendar.set_archived(uid, False)
    restored.publish_feed()
    assert restored.calendar.count == 1
    assert len(restored.calendar.snapshot_events()) == 1
    source = next(item for item in json.loads(restored.feed_json)["items"] if item["id"] == ITEM["id"])
    assert source["recognized_events"][0]["uid"] == uid
    with pytest.raises(ValueError, match="Unknown"):
        restored.calendar.set_archived("unknown", True)


def test_archiving_native_calendar_item_removes_it_from_ui_feed(tmp_path):
    controller = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir()
    controller.calendar = CalendarFeed(controller, str(directory))
    item = {**ITEM, "id": "event:noah:native", "content_type": "Calendar"}
    event = validate_events([EVENT], item)[0]
    controller.calendar.record(item, [event])
    controller.store.set("feed", [item])
    controller.calendar.set_archived(event_uid(event), True)
    controller.publish_feed()
    assert json.loads(controller.feed_json)["items"] == []
    controller.calendar.set_archived(event_uid(event), False)
    controller.publish_feed()
    assert len(json.loads(controller.feed_json)["items"]) == 1


def test_archive_persistence_failure_rolls_back(tmp_path, monkeypatch):
    controller = build_controller(tmp_path, FakeHA(), children=TWO_CHILDREN)
    event = validate_events([EVENT], ITEM)[0]
    controller.calendar.record(ITEM, [event])
    controller.profiles.save_event_review(event_uid(event), {
        "status": "accepted", "event": {**event, "manually_confirmed": True},
    })

    def fail():
        raise OSError("Disk full")

    monkeypatch.setattr(controller.profiles, "save", fail)
    with pytest.raises(OSError, match="Disk full"):
        controller.calendar.set_archived(event_uid(event), True)
    assert not controller.profiles.archived_events()
    assert len(controller.calendar.snapshot_events()) == 1


def test_subscription_excludes_selected_categories_and_persists(tmp_path):
    from aula_ui.calendar_feed import category_label, event_category

    controller = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir()
    controller.calendar = CalendarFeed(controller, str(directory))
    lesson_item = {**ITEM, "id": "event:noah:lesson", "content_type": "Calendar", "calendar_kind": "lesson"}
    meeting_item = {**ITEM, "id": "event:noah:meeting", "content_type": "Calendar",
                    "calendar_kind": "school_home_meeting"}
    lesson = validate_events([{**EVENT, "title": "Room 12 booking"}], lesson_item)[0]
    meeting = validate_events([{**EVENT, "title": "Parent talk"}], meeting_item)[0]
    post_event = validate_events([EVENT], ITEM)[0]
    controller.calendar.record(lesson_item, [lesson])
    controller.calendar.record(meeting_item, [meeting])
    controller.calendar.record(ITEM, [post_event])
    controller.calendar.review(event_uid(post_event), "accept")
    assert [event_category(e) for e in (lesson, meeting, post_event)] == [
        "aula:lesson", "aula:school_home_meeting", "ai:post"]
    assert event_category({"source_id": "thread:5"}) == "ai:message"
    assert category_label("aula:lesson") == "Lessons / classroom bookings"
    assert category_label("aula:field_trip") == "Aula: Field trip"
    assert controller.calendar.count == 3

    controller.calendar.set_excluded_categories(["aula:lesson", "ai:post"])
    assert controller.calendar.count == 1
    assert b"Parent talk" in controller.calendar.content
    assert b"Room 12 booking" not in controller.calendar.content
    assert len(controller.calendar.snapshot_events()) == 3, "Calendar tab still shows excluded categories"
    categories = {c["key"]: c for c in controller.calendar.categories()}
    assert categories["aula:lesson"] == {"key": "aula:lesson", "label": "Lessons / classroom bookings",
                                         "count": 1, "excluded": True}
    assert not categories["aula:school_home_meeting"]["excluded"]

    restored = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    assert restored.profiles.calendar_excluded_categories() == {"aula:lesson", "ai:post"}
    for invalid in ["aula:lesson", ["bad key"], ["x" * 60], [1]]:
        with pytest.raises(ValueError):
            controller.calendar.set_excluded_categories(invalid)
    controller.calendar.set_excluded_categories([])
    assert controller.calendar.count == 3
