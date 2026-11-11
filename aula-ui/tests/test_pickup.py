import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest

from aula_ui.aula_api import AulaApiError
from aula_ui.pickup import PickupPlans


@pytest.fixture
def plans():
    day = (datetime.now(ZoneInfo("Europe/Copenhagen")).date() + timedelta(days=1)).isoformat()

    class Client:
        editable = True
        persist = True
        ambiguous = False
        status = 0

        def __init__(self):
            self.writes = []
            self.template = {"byDate": day, "entryTime": "08:00:00", "exitTime": "16:00:00",
                             "exitWith": "Alex", "activityType": 0, "comment": "Bring boots",
                             "spareTimeActivity": {"startTime": "10:00:00", "endTime": "11:00:00", "comment": "Library"}}

        def guardian_profiles(self):
            return [{"children": [{"id": n, "profileId": 2} for n in (20, 21)]}]

        def get(self, uri):
            query = parse_qs(uri.lstrip("?"))
            method = query["method"][0]
            if method == "presence.getPresenceConfigurationByChildIds":
                assert query["childIds[]"] == ["20", "21"]
                return {"data": [{"uniStudentId": n, "presenceConfiguration": {
                    "pickup": True, "dashboardModuleSettings": [{"presenceDashboardContext": "guardian_dashboard",
                        "presenceModules": [{"moduleType": module,
                                            "permission": "editable" if self.editable and (n == 20 or self.ambiguous) else "readable"}
                                           for module in ("pickup_times", "pickup_type")]}]}} for n in (20, 21)]}
            if method == "presence.getPresenceTemplates":
                assert query["fromDate"] == query["toDate"] == [day]
                return {"data": {"presenceWeekTemplates": [{"institutionProfile": {"id": n},
                    "dayTemplates": [copy.deepcopy(self.template)]} for n in (20, 21)]}}
            assert method == "presence.getPickupResponsibles"
            return {"data": [{"uniStudentId": 20, "pickupSuggestions": [{"pickupName": "Alex"}]}]}

        def post(self, uri, body):
            assert uri == "?method=presence.updatePresenceTemplate"
            self.writes.append(body)
            if self.persist:
                self.template.update(entryTime=body["presenceActivity"]["pickup"]["entryTime"] + ":00",
                                     exitTime=body["presenceActivity"]["pickup"]["exitTime"] + ":00",
                                     exitWith=body["presenceActivity"]["pickup"]["exitWith"], activityType=0)
            return {"status": {"code": self.status}, "data": None}

    client = Client()
    controller = SimpleNamespace(
        source_client=client, config=SimpleNamespace(timezone="Europe/Copenhagen",
            children=[SimpleNamespace(slug="alma", name="Alma", aula_child_id="2")]),
        request_refresh=lambda: None)
    return PickupPlans(controller), client, day


def test_reads_permissions_names_and_saves_only_one_date_preserving_other_fields(plans):
    service, client, day = plans
    result = service.get("alma", day)
    assert result["plans"][0]["pickup_time"] == "16:00"
    assert result["plans"][0]["can_edit"] and not result["plans"][1]["can_edit"]
    assert result["pickup_people"][0]["pickupSuggestions"][0]["pickupName"] == "Alex"
    result = service.set("alma", day, "13:45", "Sam", confirmed=True)
    assert result["confirmed"] and result["changed"]
    assert client.writes == [{
        "institutionProfileId": 20, "byDate": day,
        "presenceActivity": {"activityType": 0, "pickup": {"entryTime": "08:00", "exitTime": "13:45", "exitWith": "Sam"}},
        "comment": "Bring boots", "repeatPattern": "never", "expiresAt": None,
        "spareTimeActivity": {"startDate": "10:00", "endDate": "11:00", "comment": "Library"},
    }]
    assert not service.set("alma", day, "13:45", "Sam", confirmed=True)["changed"]
    assert len(client.writes) == 1


@pytest.mark.parametrize("changes", [
    {"confirmed": False}, {"confirmed": 1}, {"pickup_time": "25:00"},
    {"pickup_time": "7:45"}, {"pickup_time": "07:45"}, {"person": "I"}, {"person": "me"},
    {"person": "Sam\nSomeone"}, {"day": "20200101"}, {"day": "2020-01-01"},
    {"institution": "999"}, {"slug": "unknown"},
])
def test_rejects_unsafe_requests_without_writes(plans, changes):
    service, client, day = plans
    args = {"slug": "alma", "day": day, "pickup_time": "13:45", "person": "Sam", "confirmed": True}
    args.update(changes)
    with pytest.raises((ValueError, LookupError, AulaApiError)):
        service.set(**args)
    assert not client.writes


def test_permissions_and_ambiguous_institutions_are_not_guessed(plans):
    service, client, day = plans
    client.editable = False
    with pytest.raises(PermissionError):
        service.set("alma", day, "13:45", "Sam", confirmed=True)
    client.editable = True
    client.ambiguous = True
    with pytest.raises(ValueError, match="Multiple institutions"):
        service.set("alma", day, "13:45", "Sam", confirmed=True)
    assert not client.writes
    assert service.set("alma", day, "13:45", "Sam", confirmed=True, institution="20")["confirmed"]


@pytest.mark.parametrize("cause", ["rejected", "unconfirmed", "vacation", "bad_spare"])
def test_failures_never_return_a_successful_plan(plans, cause):
    service, client, day = plans
    if cause == "rejected":
        client.status = 1
    elif cause == "unconfirmed":
        client.persist = False
    elif cause == "vacation":
        client.template["isOnVacation"] = True
    else:
        client.template["spareTimeActivity"] = {"unknown": True}
    with pytest.raises((ValueError, AulaApiError)):
        service.set("alma", day, "13:45", "Sam", confirmed=True)
    assert len(client.writes) == (1 if cause in {"rejected", "unconfirmed"} else 0)


def test_today_past_pickup_is_rejected_after_fresh_reads(plans, monkeypatch):
    from aula_ui import pickup
    service, client, day = plans

    class Late(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(day + "T18:00:00").replace(tzinfo=tz)

    monkeypatch.setattr(pickup, "datetime", Late)
    with pytest.raises(ValueError, match="future"):
        service.set("alma", day, "13:45", "Sam", confirmed=True)
    assert not client.writes
