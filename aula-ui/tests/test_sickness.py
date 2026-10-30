import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import pytest
import requests

from aula_ui import sickness
from aula_ui.aula_api import AulaApiError
from aula_ui.calendar_feed import CalendarFeed
from aula_ui.sickness import SickDays
from aula_ui.web import start_server
from test_direct_account import create_controller, install


class Clock(datetime):
    value = datetime(2026, 10, 29, 10, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


class Source:
    def __init__(self):
        self.states = {1001: 0, 1002: 3}
        self.editable = {1001}
        self.writes = []
        self.reads = []
        self.accept = True
        self.persist = True
        self.fail_reads = False

    def get(self, uri):
        self.reads.append(uri)
        if self.fail_reads:
            raise AulaApiError("Aula is temporarily unavailable")
        query = parse_qs(uri.lstrip("?"))
        method = query["method"][0]
        if method == "presence.getPresenceStates":
            assert set(query["institutionProfileIds[]"]) == {"1001", "1002"}
            data = [{"uniStudentId": identifier, "uniStudent": {"id": identifier}, "state": state}
                    for identifier, state in self.states.items()]
        else:
            assert method == "presence.getPresenceConfigurationByChildIds"
            assert set(query["childIds[]"]) == {"1001", "1002"}
            data = [{"uniStudentId": identifier, "presenceConfiguration": {
                "dashboardModuleSettings": [{"presenceDashboardContext": "guardian_dashboard",
                                             "presenceModules": [{
                                                 "moduleType": "report_sick",
                                                 "permission": "editable" if identifier in self.editable else "readable",
                                             }]}]}} for identifier in self.states]
        return {"data": data}

    def post(self, uri, data):
        self.writes.append((uri, data))
        if self.accept and self.persist:
            for identifier in data["institutionProfileIds"]:
                self.states[identifier] = data["status"]
        return {"status": {"code": 0}, "data": self.accept}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    Clock.value = datetime(2026, 10, 29, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(sickness, "datetime", Clock)
    controller = create_controller(tmp_path)
    install(controller)
    source = Source()
    controller.source_client = source
    service = SickDays(controller)
    yield controller, source, service
    controller.close()


def test_read_only_status_checks_permissions_without_writes(setup):
    controller, source, service = setup
    result = service.snapshot()
    assert result["date"] == "2026-10-29"
    assert result["valid_until"] == "2026-10-30T00:00:00+01:00"
    assert [child["can_report"] for child in result["children"]] == [True, False]
    assert not any(child["sick"] for child in result["children"])
    assert "school teacher" in result["notice"]
    service.snapshot()
    assert len(source.reads) == 2 and source.writes == []


def test_explicit_report_writes_only_selected_child_and_adds_one_all_day_calendar_event(setup):
    controller, source, service = setup
    slug = controller.config.children[0].slug
    result = service.report(slug, "2026-10-29")
    assert result["children"][0]["sick"]
    assert source.writes == [("?method=presence.updateStatusByInstitutionProfileIds",
                              {"institutionProfileIds": [1001], "status": 1})]
    events = controller.calendar.snapshot_events()
    assert len(events) == 1
    assert events[0]["start"] == "2026-10-29" and events[0]["end"] == "2026-10-30"
    assert events[0]["all_day"] and events[0]["origin"] == "parent"
    assert events[0]["child_slugs"] == [slug] and not events[0]["review_required"]
    assert b"DTSTART;VALUE=DATE:20261029" in controller.calendar.content
    assert b"DTEND;VALUE=DATE:20261030" in controller.calendar.content
    assert b"Parent sickness report confirmed by Aula" in controller.calendar.content
    assert b"Recognized from Aula" not in controller.calendar.content
    assert any(item.get("calendar_event", {}).get("calendar_kind") == "sickness"
               for item in json.loads(controller.feed_json)["items"])
    service.report(slug, "2026-10-29")
    assert len(source.writes) == 1 and len(controller.calendar.snapshot_events()) == 1
    # Restart retains the dated history, not an undated 'sick' flag.
    restored = CalendarFeed(controller, controller.calendar.directory)
    assert restored.snapshot_events()[0]["start"] == "2026-10-29"


def test_midnight_expires_cached_status_without_resetting_any_aula_state(setup):
    controller, source, service = setup
    slug = controller.config.children[0].slug
    service.report(slug, "2026-10-29")
    source.states[1001] = 0  # Aula clears the status for the following day.
    Clock.value = datetime(2026, 10, 29, 23, tzinfo=timezone.utc)
    result = service.snapshot()
    assert result["date"] == "2026-10-30" and not result["children"][0]["sick"]
    with pytest.raises(ValueError, match="date has changed"):
        service.report(slug, "2026-10-29")
    assert len(source.writes) == 1
    assert controller.calendar.snapshot_events()[0]["end"] == "2026-10-30"


def test_clear_sickness_is_verified_and_removes_only_todays_sick_day(setup):
    controller, source, service = setup
    slug = controller.config.children[0].slug
    service._record(slug, "Maja", "2026-10-28")
    service.report(slug, "2026-10-29")
    result = service.report(slug, "2026-10-29", False)
    assert not result["children"][0]["sick"]
    assert source.writes[-1] == ("?method=presence.updateStatusByInstitutionProfileIds",
                                 {"institutionProfileIds": [1001], "status": 0})
    assert source.states[1002] == 3
    assert [event["start"] for event in controller.calendar.snapshot_events()] == ["2026-10-28"]
    assert b"DTSTART;VALUE=DATE:20261029" not in controller.calendar.content
    assert not any(item.get("calendar_event", {}).get("start") == "2026-10-29"
                   for item in json.loads(controller.feed_json)["items"])
    service.report(slug, "2026-10-29", False)
    assert len(source.writes) == 2
    restored = CalendarFeed(controller, controller.calendar.directory)
    assert [event["start"] for event in restored.snapshot_events()] == ["2026-10-28"]


@pytest.mark.parametrize("state", [0, 2, 3, 5, 8])
def test_clear_never_overwrites_a_non_sick_presence_state(setup, state):
    controller, source, service = setup
    source.states[1001] = state
    service.report(controller.config.children[0].slug, "2026-10-29", False)
    assert source.states[1001] == state and source.writes == []


def test_clear_only_sick_identity_when_child_attends_multiple_institutions(setup):
    controller, source, service = setup
    slug = controller.config.children[0].slug
    # Both identities belong to the same configured child for this scenario.
    service._identities = lambda: {slug: ["1001", "1002"], controller.config.children[1].slug: []}
    source.editable = {1001, 1002}
    source.states = {1001: 1, 1002: 3}
    assert service.snapshot()["children"][0]["sick"]
    service.report(slug, "2026-10-29", False)
    assert source.writes == [("?method=presence.updateStatusByInstitutionProfileIds",
                              {"institutionProfileIds": [1001], "status": 0})]
    assert source.states[1002] == 3


@pytest.mark.parametrize("accept,persist", [(False, False), (True, False)])
def test_failed_clear_preserves_calendar_entry(setup, accept, persist):
    controller, source, service = setup
    slug = controller.config.children[0].slug
    service.report(slug, "2026-10-29")
    source.accept, source.persist = accept, persist
    with pytest.raises(AulaApiError):
        service.report(slug, "2026-10-29", False)
    assert len(controller.calendar.snapshot_events()) == 1


def test_clear_obeys_permissions_current_day_and_boolean_validation(setup):
    controller, source, service = setup
    with pytest.raises(PermissionError):
        service.report(controller.config.children[1].slug, "2026-10-29", False)
    with pytest.raises(ValueError, match="date has changed"):
        service.report(controller.config.children[0].slug, "2026-10-28", False)
    with pytest.raises(ValueError, match="boolean"):
        service.report(controller.config.children[0].slug, "2026-10-29", "false")
    assert not source.writes


@pytest.mark.parametrize("instant,expiry", [
    (datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), "2026-03-30T00:00:00+02:00"),
    (datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), "2026-10-26T00:00:00+01:00"),
])
def test_local_midnight_handles_daylight_saving(setup, instant, expiry):
    assert setup[2].snapshot(now=instant)["valid_until"] == expiry


def test_unpermitted_child_unknown_child_and_old_date_do_not_write(setup):
    controller, source, service = setup
    with pytest.raises(PermissionError):
        service.report(controller.config.children[1].slug, "2026-10-29")
    with pytest.raises(LookupError):
        service.report("other", "2026-10-29")
    with pytest.raises(ValueError):
        service.report(controller.config.children[0].slug, "2026-10-28")
    assert not source.writes and not controller.calendar.snapshot_events()


def test_midnight_during_permission_checks_does_not_send_yesterday_report(setup, monkeypatch):
    controller, source, service = setup
    original_get = source.get

    def crosses_midnight(uri):
        result = original_get(uri)
        if "Configuration" in uri:
            Clock.value = datetime(2026, 10, 29, 23, tzinfo=timezone.utc)
        return result

    monkeypatch.setattr(source, "get", crosses_midnight)
    with pytest.raises(ValueError, match="date has changed"):
        service.report(controller.config.children[0].slug, "2026-10-29")
    assert source.writes == [] and not controller.calendar.snapshot_events()


@pytest.mark.parametrize("accept,persist", [(False, False), (True, False)])
def test_unconfirmed_write_never_creates_a_sick_day(setup, accept, persist):
    controller, source, service = setup
    source.accept, source.persist = accept, persist
    with pytest.raises(AulaApiError):
        service.report(controller.config.children[0].slug, "2026-10-29")
    assert not controller.calendar.snapshot_events()


def test_api_is_explicit_and_protected(setup):
    controller, source, _ = setup
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/api/sickness"
    value = {"child": controller.config.children[0].slug, "date": "2026-10-29"}
    headers = {"X-Aula-Request": "settings"}
    try:
        assert requests.get(url, timeout=5).json()["children"][0]["can_report"]
        for header in ({}, {"X-Aula-Request": "refresh"}):
            assert requests.post(url, json=value, headers=header, timeout=5).status_code == 403
        for invalid in ({}, {**value, "date": "2026-10-28"}, {**value, "child": 1},
                        {**value, "extra": 1}, {**value, "sick": "false"}, {**value, "sick": 0}):
            assert requests.post(url, json=invalid, headers=headers, timeout=5).status_code == 400
        assert source.writes == []
        assert requests.post(url, json={**value, "child": "other"}, headers=headers, timeout=5).status_code == 404
        assert requests.post(url, json={**value, "child": controller.config.children[1].slug},
                             headers=headers, timeout=5).status_code == 403
        result = requests.post(url, json=value, headers=headers, timeout=5)
        assert result.status_code == 200 and result.json()["children"][0]["sick"]
        result = requests.post(url, json={**value, "sick": False}, headers=headers, timeout=5)
        assert result.status_code == 200 and not result.json()["children"][0]["sick"]
        assert not controller.calendar.snapshot_events()
    finally:
        server.shutdown()
        server.server_close()


def test_api_reports_upstream_errors(setup):
    controller, source, _ = setup
    source.fail_reads = True
    server = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/api/sickness"
    try:
        response = requests.get(url, timeout=5)
        assert response.status_code == 502 and response.json()["error"]
        assert source.writes == []
    finally:
        server.shutdown()
        server.server_close()
