import json

import pytest

from aula_ui.sync_settings import DEFAULTS, SyncSettings

REAL_REFRESH_SECONDS = SyncSettings.refresh_seconds


def test_defaults_match_the_previous_fixed_cadence(tmp_path, monkeypatch):
    monkeypatch.setattr(SyncSettings, "refresh_seconds", REAL_REFRESH_SECONDS)
    settings = SyncSettings(str(tmp_path))
    assert settings.full_sync_seconds == 900 and settings.quick_seconds == 60
    assert [settings.refresh_seconds(kind) for kind in ("posts", "calendar", "planning", "albums")] == [
        900, 1800, 1800, 1800]
    assert settings.values() == DEFAULTS and not (tmp_path / "sync_settings.json").exists()


def test_legacy_poll_interval_seeds_full_sync_once(tmp_path):
    assert SyncSettings(str(tmp_path), 300)["full_sync_minutes"] == 5
    assert SyncSettings(str(tmp_path), 60)["full_sync_minutes"] == 5  # clamped to the safe minimum
    settings = SyncSettings(str(tmp_path), 1800)
    assert settings["full_sync_minutes"] == 30
    settings.update({"posts_minutes": 20})
    assert SyncSettings(str(tmp_path), 300)["full_sync_minutes"] == 30  # saved file wins


def test_update_persists_only_changes_and_validates(tmp_path):
    settings = SyncSettings(str(tmp_path))
    values = settings.update({"quick_check_seconds": 120, "quick_presence": False, "posts_minutes": 15})
    assert values["quick_check_seconds"] == 120 and values["quick_presence"] is False
    assert json.loads((tmp_path / "sync_settings.json").read_text()) == {
        "quick_check_seconds": 120, "quick_presence": False}
    assert SyncSettings(str(tmp_path)).values() == values
    for bad in ({"quick_check_seconds": 10}, {"full_sync_minutes": "15"}, {"posts_minutes": True},
                {"quick_messages": 1}, {"poll_interval": 900}, {"messages_from_hour": 23},
                {"presence_to_hour": 6}):
        with pytest.raises(ValueError):
            settings.update(bad)
    with pytest.raises(ValueError):
        settings.update([])
    assert SyncSettings(str(tmp_path)).values() == values


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "sync_settings.json").write_text("{nope")
    assert SyncSettings(str(tmp_path)).values() == DEFAULTS
    (tmp_path / "sync_settings.json").write_text(json.dumps({"quick_check_seconds": 1}))
    assert SyncSettings(str(tmp_path)).values() == DEFAULTS


def test_snapshot_reports_limits_and_a_request_estimate(tmp_path):
    settings = SyncSettings(str(tmp_path))
    snapshot = settings.snapshot()
    assert snapshot["limits"]["quick_check_seconds"] == {"min": 30, "max": 900}
    assert snapshot["estimate"] < snapshot["budget"]
    faster = settings.update({"quick_check_seconds": 30})
    assert SyncSettings.estimate(faster) > snapshot["estimate"]
    off = settings.update({"quick_messages": False, "quick_presence": False})
    assert SyncSettings.estimate(off) < snapshot["estimate"]


def test_controller_saves_settings_and_wakes_the_loop(tmp_path):
    from test_controller import FakeHA, build_controller

    controller = build_controller(tmp_path, FakeHA())
    try:
        controller._refresh.clear()
        snapshot = controller.set_sync_settings({"full_sync_minutes": 30})
        assert snapshot["values"]["full_sync_minutes"] == 30 and controller._refresh.is_set()
        assert controller.sync_settings.full_sync_seconds == 1800
    finally:
        controller.stop()
