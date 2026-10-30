"""Presence is current, expiring status; never an AI-enriched feed post."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from aula_ui.aula_api import AulaApiError
from aula_ui.presence import snapshot
from test_direct_account import create_controller, install, patch_presence, source_handlers
from aula_ui.feed import Feed

@pytest.fixture
def controller(tmp_path):
    value = create_controller(tmp_path / "data")
    yield value
    value.close()


def test_current_presence_with_no_arrival_timestamp_is_valid(controller):
    install(controller)
    now = datetime(2026, 10, 26, 8, tzinfo=timezone.utc)
    controller.store.set("direct_presence:child_101", {"status": "Not arrived"})
    controller.store.set("presence_sync:child_101", {"checked_at": now.isoformat()})
    rows = snapshot(controller, now)
    assert rows[0]["status"] == "Not arrived"
    assert rows[0]["state"] == "away"
    assert rows[0]["check_in_time"] == ""
    assert rows[1]["status"] == "Unknown"


def test_all_children_presence_expire_at_central_timezone_midnight(tmp_path):
    from test_controller import TWO_CHILDREN, FakeHA, build_controller

    controller = build_controller(
        tmp_path, FakeHA(), children=TWO_CHILDREN,
        timezone="America/New_York", poll_interval=86400,
    )
    try:
        now = datetime(2026, 10, 27, 3, 50, tzinfo=timezone.utc)
        controller.source_states = [
            {"child_slug": child.slug, "state": "on", "attributes": {"status_description": "Present"},
             "last_updated": now.isoformat()}
            for child in controller.config.children
        ]
        rows = snapshot(controller, now)
        assert len(rows) == 2
        assert {row["timezone"] for row in rows} == {"America/New_York"}
        assert {row["valid_until"] for row in rows} == {"2026-10-27T00:00:00-04:00"}
        assert {row["status"] for row in snapshot(controller, now + timedelta(minutes=10))} == {"Unknown"}
    finally:
        controller.close()


@pytest.mark.parametrize("case", ["missing", "old", "yesterday", "failed", "unknown", "source_yesterday", "future"])
def test_unreliable_presence_is_unknown_and_omits_old_times(controller, case):
    install(controller)
    now = datetime(2026, 10, 26, 8, tzinfo=timezone.utc)
    data = {"status": "Present", "check_in_time": "07:30"}
    sync = {"checked_at": now.isoformat()}
    if case == "missing":
        sync = {}
    elif case == "old":
        sync["checked_at"] = (now - timedelta(hours=2)).isoformat()
    elif case == "yesterday":
        sync["checked_at"] = (now - timedelta(days=1)).isoformat()
    elif case == "failed":
        sync["failed"] = True
    elif case == "unknown":
        data["status"] = "Unknown presence status (99)"
    elif case == "source_yesterday":
        data["date"] = (now - timedelta(days=1)).isoformat()
    elif case == "future":
        sync["checked_at"] = (now + timedelta(hours=1)).isoformat()
    controller.store.set("direct_presence:child_101", data)
    controller.store.set("presence_sync:child_101", sync)
    row = snapshot(controller, now)[0]
    assert row["status"] == "Unknown" and row["state"] == "unknown"
    assert row["check_in_time"] == "" and row["valid_until"] is None


def test_midnight_invalidates_previous_day_even_with_long_poll_interval(controller):
    install(controller)
    now = datetime(2026, 10, 26, 22, 50, tzinfo=timezone.utc)
    controller.store.set("direct_presence:child_101", {"status": "Present"})
    controller.store.set("presence_sync:child_101", {"checked_at": now.isoformat()})
    assert snapshot(controller, now)[0]["valid_until"] == "2026-10-27T00:00:00+01:00"
    assert snapshot(controller, now + timedelta(minutes=10))[0]["status"] == "Unknown"


def test_presence_failures_invalidate_cache_and_recovery_restores_it(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    patch_presence(monkeypatch, controller.account.source, lambda child: {
        "status": "Present", "exit_with": "", "date": "", "present": True,
        "check_in_time": "08:00",
    })
    controller.account.collect()
    assert snapshot(controller)[0]["status"] == "Present"
    def fail(child):
        raise AulaApiError("Unavailable")
    patch_presence(monkeypatch, controller.account.source, fail)
    assert controller.account.collect()
    assert snapshot(controller)[0]["status"] == "Unknown"
    assert snapshot(controller)[0]["reason"] == "Refresh failed"
    patch_presence(monkeypatch, controller.account.source, lambda child: None)
    controller.account.collect()
    assert snapshot(controller)[0]["status"] == "Unknown"
    assert snapshot(controller)[0]["reason"] == "No presence reported"
    assert not controller.store.get("presence_sync:child_101")["failed"]


def test_cached_presence_posts_are_not_published(controller):
    install(controller)
    controller.store.set("feed", [{
        "id": "presence:child_101", "content_type": "Presence", "child_slugs": ["child_101"],
    }])
    controller.publish_feed()
    payload = json.loads(controller.feed_json)
    assert payload["items"] == []
    assert len(payload["presence"]) == 2
    assert not Feed._needs_enrichment({
        "content_type": "Presence", "source": "original", "title": "Present",
    })
    assert controller.feed.ai_progress["total"] == 0


def test_recorded_naps_use_child_timezone_and_persist_after_waking(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    now = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)
    naps = [{"start": "09:00:00", "end": "09:30:00"}, {"start": "12:05:00", "end": ""}]
    patch_presence(monkeypatch, controller.account.source, lambda child: {
        "status": "Napping", "present": True, "exit_with": "", "date": "",
        "sleep_intervals": naps,
    })
    controller.account.collect()
    controller.store.set("presence_sync:child_101", {"checked_at": now.isoformat()})
    row = snapshot(controller, now)[0]
    assert row["sleep_intervals"][-1]["end"] is None
    assert row["sleep_intervals"][-1]["start"].endswith("T12:05:00+01:00")
    naps[-1]["end"] = "13:25:00"
    patch_presence(monkeypatch, controller.account.source, lambda child: {
        "status": "Present", "present": True, "exit_with": "", "date": "",
        "sleep_intervals": naps,
    })
    controller.account.collect()
    controller.store.set("presence_sync:child_101", {"checked_at": now.isoformat()})
    controller.store.save()
    controller.store.load()
    row = snapshot(controller, now)[0]
    nap = row["sleep_intervals"][-1]
    assert row["status"] == "Present"
    assert (datetime.fromisoformat(nap["end"]) - datetime.fromisoformat(nap["start"])).total_seconds() == 4800
    assert snapshot(controller, now + timedelta(days=1))[0]["sleep_intervals"] == []


@pytest.mark.parametrize("nap,malformed", [
    ({"start": "invalid", "end": ""}, True),
    ({"start": "12:30", "end": "12:00"}, True),
    ({"start": "15:00", "end": ""}, False),
    ({"start": "2026-10-25T10:00:00Z", "end": ""}, False),
    ({"start": "11:00", "end": "invalid"}, True),
])
def test_invalid_nap_times_are_not_invented(controller, nap, malformed, caplog):
    import aula_ui.presence as presence
    presence._reported_invalid.clear()
    install(controller)
    now = datetime(2026, 10, 26, 13, tzinfo=timezone.utc)
    controller.store.set("direct_presence:child_101", {"status": "Napping", "sleep_intervals": [nap]})
    controller.store.set("presence_sync:child_101", {"checked_at": now.isoformat()})
    caplog.set_level("DEBUG", logger="aula_ui.presence")
    assert snapshot(controller, now)[0]["sleep_intervals"] == []
    warnings = [record for record in caplog.records if record.levelname == "WARNING"
                and "presence.nap_invalid" in record.getMessage()]
    # Malformed values are reported once; earlier days' naps are an expected, quiet state.
    assert len(warnings) == (1 if malformed else 0)
    assert malformed or "presence.nap_skipped" in caplog.text
    caplog.clear()
    for _ in range(5):
        snapshot(controller, now)
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


def test_previous_days_naps_after_midnight_are_hidden_without_warnings(controller, caplog):
    install(controller)
    # 01:19 Copenhagen: Aula still lists yesterday's naps as bare times of day.
    now = datetime(2026, 10, 30, 0, 19, tzinfo=timezone.utc)
    controller.store.set("direct_presence:child_101", {"status": "Picked up", "sleep_intervals": [
        {"start": "12:00:00", "end": "13:30:00"}, {"start": "15:00:00", "end": ""}]})
    controller.store.set("presence_sync:child_101", {"checked_at": now.isoformat()})
    for _ in range(50):
        assert snapshot(controller, now)[0]["sleep_intervals"] == []
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


def test_nap_started_a_few_seconds_ahead_of_our_clock_is_shown(controller):
    install(controller)
    now = datetime(2026, 10, 26, 12, 4, 30, tzinfo=timezone.utc)  # 13:04:30 local
    controller.store.set("direct_presence:child_101", {"status": "Napping", "sleep_intervals": [
        {"start": "13:05:00", "end": ""}]})
    controller.store.set("presence_sync:child_101", {"checked_at": now.isoformat()})
    assert snapshot(controller, now)[0]["sleep_intervals"][0]["start"].endswith("T13:05:00+01:00")
