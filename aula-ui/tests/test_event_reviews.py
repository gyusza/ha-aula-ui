import json

import pytest

from aula_ui.calendar_feed import CalendarFeed, event_uid, validate_events
from test_calendar_feed import EVENT, ITEM
from test_controller import TWO_CHILDREN, FakeHA, build_controller


def setup_calendar(tmp_path):
    controller = build_controller(tmp_path / "data", FakeHA(), children=TWO_CHILDREN)
    directory = tmp_path / "www" / "aula-feed"
    directory.parent.mkdir(exist_ok=True)
    controller.calendar = CalendarFeed(controller, str(directory))
    controller.store.set("feed", [{**ITEM, "content_type": "Post", "summary": "Source summary"}])
    return controller


def test_recognized_calendar_items_deduped_and_archive_removes_feed_and_ics(tmp_path):
    controller = setup_calendar(tmp_path)
    event = validate_events([EVENT], ITEM)[0]
    controller.calendar.record(ITEM, [event])
    controller.calendar.record({"id": "post:duplicate"}, [{**event, "source_id": "post:duplicate"}])
    controller.publish_feed()
    items = json.loads(controller.feed_json)["items"]
    calendars = [item for item in items if item["content_type"] == "Calendar"]
    assert len(calendars) == 1
    uid = calendars[0]["calendar_event"]["uid"]
    assert calendars[0]["date"] == "2026-10-09"
    assert calendars[0]["child_slugs"] == ["noah"]
    assert calendars[0]["calendar_event"]["review_required"]
    assert controller.calendar.count == 0
    controller.calendar.review(uid, "accept")
    controller.calendar.set_archived(uid, True)
    controller.publish_feed()
    assert not [item for item in json.loads(controller.feed_json)["items"] if item["content_type"] == "Calendar"]
    assert b"BEGIN:VEVENT" not in controller.calendar.content
    controller.calendar.set_archived(uid, False)
    controller.publish_feed()
    assert len([item for item in json.loads(controller.feed_json)["items"] if item["content_type"] == "Calendar"]) == 1


def test_recommendations_stay_out_of_ics_until_corrected_and_accepted(tmp_path):
    controller = setup_calendar(tmp_path)
    raw = {**EVENT, "start": "", "issue": "Date unclear",
           "recommendation": "Check the actual Friday with the school."}
    controller.calendar.recommend(ITEM, raw, "Date cannot be validated")
    controller.publish_feed()
    pending = controller.calendar.recommendations()[0]
    uid = pending["uid"]
    calendar = next(item for item in json.loads(controller.feed_json)["items"] if item["content_type"] == "Calendar")
    assert calendar["calendar_event"]["review_required"]
    assert calendar["date"] == ITEM["date"]
    assert "Check the actual Friday" in calendar["summary"]
    assert controller.calendar.count == 0
    with pytest.raises(ValueError):
        controller.calendar.review(uid, "accept")
    assert controller.calendar.recommendations()
    controller.calendar.review(uid, "edit", {
        "title": "Pyjama day confirmed", "start": "2026-10-09", "end": "",
        "all_day": True, "location": "School", "description": "Bring a teddy bear.",
    })
    controller.publish_feed()
    assert not controller.calendar.recommendations()
    assert controller.calendar.count == 1
    assert uid.encode() in controller.calendar.content
    assert b"Pyjama day confirmed" in controller.calendar.content
    assert b"Bring a teddy bear." in controller.calendar.content
    accepted = controller.calendar.snapshot_events()[0]
    assert accepted["manually_confirmed"] and accepted["end"] == "2026-10-10"
    assert event_uid(accepted) == uid
    controller.calendar.recommend(ITEM, raw, "Still unclear")
    controller.calendar.record(ITEM, validate_events([EVENT], ITEM))
    controller.publish_feed()
    assert controller.calendar.count == 1
    assert not controller.calendar.recommendations()
    controller.store.save()
    restored = setup_calendar(tmp_path)
    restored.publish_feed()
    assert restored.calendar.count == 1
    assert restored.calendar.snapshot_events()[0]["title"] == "Pyjama day confirmed"


def test_invalid_candidate_date_uses_source_date_without_losing_proposed_text(tmp_path):
    controller = setup_calendar(tmp_path)
    controller.calendar.recommend(ITEM, {**EVENT, "start": "next Friday"}, "Date needs checking")
    controller.publish_feed()
    calendar = next(item for item in json.loads(controller.feed_json)["items"] if item["content_type"] == "Calendar")
    assert calendar["date"] == ITEM["date"]
    assert calendar["calendar_event"]["start"] == "next Friday"


def test_decline_persists_hides_recommendation_and_allows_later_acceptance(tmp_path):
    controller = setup_calendar(tmp_path)
    controller.calendar.recommend(ITEM, EVENT, "Evidence needs review")
    uid = controller.calendar.recommendations()[0]["uid"]
    controller.calendar.review(uid, "decline")
    controller.publish_feed()
    assert not controller.calendar.recommendations()
    assert controller.calendar.recommendations(True)[0]["uid"] == uid
    assert not [item for item in json.loads(controller.feed_json)["items"] if item["content_type"] == "Calendar"]
    controller.calendar.recommend(ITEM, EVENT, "Again")
    assert not controller.calendar.recommendations()
    restored = setup_calendar(tmp_path)
    assert not restored.calendar.recommendations()
    assert restored.calendar.recommendations(True)
    restored.calendar.review(uid, "accept")
    assert restored.calendar.count == 1
    assert not restored.calendar.recommendations(True)


def test_declined_recommendation_does_not_reappear_when_ai_later_validates_it(tmp_path):
    controller = setup_calendar(tmp_path)
    controller.calendar.recommend(ITEM, EVENT, "Verify source")
    uid = controller.calendar.recommendations()[0]["uid"]
    controller.calendar.review(uid, "decline")
    controller.calendar.record(ITEM, validate_events([EVENT], ITEM))
    controller.publish_feed()
    assert controller.calendar.count == 0
    assert not controller.calendar.snapshot_events()
    assert controller.calendar.recommendations(True)


def test_edits_keep_ics_uid_and_survive_reanalysis_and_archiving(tmp_path):
    controller = setup_calendar(tmp_path)
    event = validate_events([EVENT], ITEM)[0]
    controller.calendar.record(ITEM, [event])
    uid = event_uid(event)
    controller.calendar.review(uid, "edit", {
        "title": "Changed title", "start": "2026-10-10T10:00:00", "end": "",
        "all_day": False, "location": "New hall",
    })
    controller.calendar.record(ITEM, [event])
    controller.publish_feed()
    effective = controller.calendar.snapshot_events()[0]
    assert effective["title"] == "Changed title" and effective["uid"] == uid
    assert effective["start"] == "2026-10-10T08:00:00+00:00"
    detail = next(item for item in json.loads(controller.feed_json)["items"] if item["id"] == ITEM["id"])
    assert detail["recognized_events"][0]["title"] == "Changed title"
    controller.calendar.set_archived(uid, True)
    assert controller.calendar.count == 0
    controller.calendar.set_archived(uid, False)
    assert controller.calendar.count == 1 and uid.encode() in controller.calendar.content
    with pytest.raises(ValueError):
        controller.calendar.review(uid, "edit", {
            "title": "Bad edit", "start": "2026-02-31", "end": "", "all_day": True, "location": "",
        })
    assert controller.calendar.snapshot_events()[0]["title"] == "Changed title"
    with pytest.raises(ValueError, match="Unknown"):
        controller.calendar.review("unknown", "accept")


def test_clear_ai_dates_require_approval_and_expose_the_original_source(tmp_path):
    controller = setup_calendar(tmp_path)
    source = {**ITEM, "sender": "Class teacher"}
    event = validate_events([{**EVENT, "date_rationale": "Friday 9 October; source published in 2026."}], source)[0]
    controller.calendar.record(source, [event])
    controller.publish_feed()
    candidate = controller.calendar.recommendations()[0]
    assert candidate["origin"] == "ai" and candidate["review_required"]
    assert candidate["source_title"] == ITEM["title"]
    assert candidate["source_id"] == ITEM["id"]
    assert candidate["source_date"] == ITEM["date"]
    assert candidate["source_author"] == "Class teacher"
    assert candidate["evidence"] == EVENT["evidence"]
    assert candidate["source_excerpt"] == ITEM["original_text"]
    assert candidate["date_rationale"].startswith("Friday")
    assert controller.calendar.count == 0
    controller.calendar.review(candidate["uid"], "accept")
    controller.publish_feed()
    accepted = controller.calendar.snapshot_events()[0]
    assert accepted["origin"] == "ai" and not accepted["review_required"]
    assert accepted["uid"] == candidate["uid"]
    assert accepted["source_excerpt"] == ITEM["original_text"]
    assert controller.calendar.count == 1


def test_model_cannot_claim_official_origin_or_parent_approval(tmp_path):
    controller = setup_calendar(tmp_path)
    forged = {**EVENT, "source_id": "event:forged", "origin": "aula",
              "manually_confirmed": True, "review_required": False,
              "source_excerpt": "Invented excerpt"}
    event = validate_events([forged], ITEM)[0]
    controller.calendar.record(ITEM, [event])
    assert event["source_id"] == ITEM["id"] and event["origin"] == "ai"
    assert event["source_excerpt"] == ITEM["original_text"]
    assert event["review_required"]
    assert not controller.calendar.snapshot_events()


def test_evidence_preserves_verbatim_source_case_and_spacing(tmp_path):
    source = {**ITEM, "original_text": "Friday 9 October\nis Pyjama day."}
    event = validate_events([{**EVENT, "evidence": "friday 9 october is pyjama day."}], source)[0]
    assert event["evidence"] == source["original_text"]
    controller = setup_calendar(tmp_path)
    controller.calendar.recommend(ITEM, {**EVENT, "evidence": "Made up quote"}, "Not grounded")
    assert controller.calendar.recommendations()[0]["evidence"] == ""


def test_native_records_cannot_be_suppressed_by_identical_declined_ai(tmp_path):
    controller = setup_calendar(tmp_path)
    controller.calendar.record(ITEM, validate_events([EVENT], ITEM))
    ai = controller.calendar.recommendations()[0]
    controller.calendar.review(ai["uid"], "decline")
    source = {**ITEM, "id": "event:noah:native", "content_type": "Calendar"}
    official = validate_events([EVENT], source)[0]
    controller.calendar.record(source, [official])
    controller.store.set("feed", [source])
    controller.publish_feed()
    native = controller.calendar.snapshot_events()[0]
    assert native["origin"] == "aula" and not native["review_required"]
    assert native["uid"] != ai["uid"]
    assert controller.calendar.count == 1
    assert b"Official Aula calendar event" in controller.calendar.content
    controller.calendar.review(ai["uid"], "accept")
    controller.publish_feed()
    assert {event["origin"] for event in controller.calendar.snapshot_events()} == {"aula", "ai"}
    assert controller.calendar.count == 2


def test_persisted_unapproved_ai_migrates_but_approvals_keep_uid_and_survive_source_removal(tmp_path):
    controller = setup_calendar(tmp_path)
    event = validate_events([EVENT], ITEM)[0]
    legacy = {key: value for key, value in event.items()
              if key not in ("origin", "review_required", "source_author", "source_excerpt", "date_rationale")}
    controller.store.set("recognized_events", {ITEM["id"]: [legacy]})
    controller.store.save()
    restored = setup_calendar(tmp_path)
    restored.publish_feed()
    assert restored.calendar.count == 0
    uid = restored.calendar.recommendations()[0]["uid"]
    assert uid == event_uid(legacy)
    restored.calendar.review(uid, "accept")
    restored.calendar.record(ITEM, [])
    restored.store.save()
    again = setup_calendar(tmp_path)
    again.publish_feed()
    assert again.calendar.count == 1
    assert again.calendar.snapshot_events()[0]["uid"] == uid


def test_legacy_native_uid_survives_refresh_without_becoming_a_suggestion(tmp_path):
    controller = setup_calendar(tmp_path)
    source = {**ITEM, "id": "event:noah:legacy"}
    event = validate_events([EVENT], source)[0]
    legacy = {key: value for key, value in event.items() if key not in ("origin", "review_required")}
    old_uid = event_uid({**legacy, "source_id": ""})
    controller.store.set("recognized_events", {source["id"]: [legacy]})
    controller.store.save()
    restored = setup_calendar(tmp_path)
    restored.calendar.record(source, [event])
    restored.publish_feed()
    assert restored.calendar.snapshot_events()[0]["uid"] == old_uid
    assert not restored.calendar.recommendations()
    assert restored.calendar.count == 1


@pytest.mark.parametrize("native_first", [False, True])
def test_legacy_native_and_ai_uid_collision_never_hides_official_record(tmp_path, native_first):
    controller = setup_calendar(tmp_path)
    ai = validate_events([EVENT], ITEM)[0]
    native_item = {**ITEM, "id": "event:noah:legacy"}
    native = validate_events([EVENT], native_item)[0]
    native.pop("origin")
    native.pop("review_required")
    pairs = [(native_item["id"], [native]), (ITEM["id"], [ai])]
    controller.store.set("recognized_events", dict(pairs if native_first else reversed(pairs)))
    controller.store.save()
    restored = setup_calendar(tmp_path)
    restored.publish_feed()
    official = restored.calendar.snapshot_events()[0]
    assert official["origin"] == "aula"
    assert not restored.calendar.recommendations()
    assert official["uid"] != event_uid(ai)
    restored.publish_feed()
    assert restored.calendar.count == 1


THREAD = {
    "id": "thread:900", "title": "Fælles fødselsdag", "date": "2026-10-25T10:59:00Z",
    "child_slugs": ["noah"], "children": ["Noah"],
    "original_text": ("Festen er lørdag d. 31. oktober kl. 11-13.30 i musiklokalet.\n"
                      "Saturday\nJeg tager kage med.\nVi mødes lørdag og pynter op."),
}


def test_gate_rejects_weak_thread_suggestions_and_same_day_duplicates():
    from aula_ui.calendar_feed import gate_suggestions
    party = {"title": "Birthday party", "start": "2026-10-31T11:00:00+01:00",
             "evidence": "Festen er lørdag d. 31. oktober kl. 11-13.30 i musiklokalet."}
    suggestions = [
        ({"title": "Gathering preparation", "start": "2026-10-31", "evidence": "Saturday"}, "unclear"),
        ({"title": "Decorating", "start": "", "evidence": "Vi mødes lørdag og pynter op."}, "unclear"),
        ({"title": "Setup", "start": "2026-10-31", "evidence": "Festen er lørdag d. 31. oktober kl. 11-13.30"}, "x"),
    ]
    confirmed, kept, dropped = gate_suggestions(THREAD, [party], suggestions)
    assert confirmed == [party] and kept == []
    text = "\n".join(dropped)
    assert "too short (1 words)" in text and "no explicit date" in text and "already on 2026-10-31" in text


def test_gate_keeps_well_evidenced_post_suggestion_and_caps_threads():
    from aula_ui.calendar_feed import gate_suggestions
    post = {"id": "post:1"}
    good = ({"title": "Trip", "start": "", "evidence": "Vi skal på tur fredag"}, "date unclear")
    assert gate_suggestions(post, [], [good])[1] == [good]
    many = [({"title": f"E{n}", "start": f"2026-11-0{n}",
              "evidence": f"Vi holder arrangement nummer {n} den {n}. november"}, "x") for n in range(1, 6)]
    chat = {**THREAD, "original_text": "\n".join(raw["evidence"] for raw, _ in many)}
    _, kept, dropped = gate_suggestions(chat, [], many)
    assert len(kept) == 3 and len(dropped) == 2


def test_existing_weak_pending_suggestions_are_retired_on_publish(tmp_path):
    controller = setup_calendar(tmp_path)
    weak = {"title": "Gathering preparation", "start": "2026-10-31", "end": "", "all_day": True,
            "location": "", "evidence": "Saturday", "issue": "Date ambiguous"}
    strong = {**weak, "title": "Birthday party", "start": "2026-11-07",
              "evidence": "Festen er lørdag d. 31. oktober kl. 11-13.30 i musiklokalet."}
    controller.calendar.recommend(THREAD, weak, "Date ambiguous")
    controller.calendar.recommend(THREAD, strong, "Date ambiguous")
    controller.publish_feed()
    titles = [event["title"] for event in controller.calendar.recommendations()]
    assert titles == ["Birthday party"]
    retired = [review for review in controller.profiles.event_reviews().values()
               if review["status"] == "superseded"]
    assert len(retired) == 1 and "too short" in retired[0]["dismissed_reason"]
