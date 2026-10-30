"""End-to-end controller tests against a fake Home Assistant."""

import json

import pytest

from aula_ui.config import build_config
from aula_ui.controller import AulaUIController

MESSAGES_SENSOR = "binary_sensor.easyiq_messages"
WEEKPLAN_SENSOR = "sensor.easyiq_alma_jensen_weekplan"
CALENDAR = "calendar.easyiq_alma_jensen_weekplan"


def make_options(**overrides):
    options = {
        "ha_url": "http://ha.local:8123/api",
        "ha_token": "token",
        "llm_provider": "none",
        "poll_interval": 60,
        "message_source": "sensor",
        "data_source": "integration",
        "children": [
            {
                "name": "Alma",
                "slug": "alma",
                "messages_sensor": MESSAGES_SENSOR,
                "weekplan_sensor": WEEKPLAN_SENSOR,
                "calendars": [CALENDAR],
            }
        ],
    }
    options.update(overrides)
    return options


class FakeHA:
    """Serves fixture states and records any (unexpected) state writes."""

    def __init__(self, states=None, calendar_events=None, service_responses=None):
        self.states = dict(states or {})
        self.calendar_events = calendar_events or {}
        self.service_responses = service_responses or {}
        self.service_calls = []
        self.written = {}

    def call_service(self, domain, service, data=None, return_response=False, timeout=None):
        self.service_calls.append((domain, service, data))
        key = f"{domain}.{service}"
        handler = self.service_responses.get(key)
        if handler is None:
            raise AssertionError(f"unexpected service call {key}")
        payload = handler(data) if callable(handler) else handler
        return {"service_response": payload}

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def list_states(self):
        return list(self.states.values())

    def set_state(self, entity_id, state, attributes=None):
        self.written[entity_id] = {"state": state, "attributes": attributes or {}}

    def get_calendar_events(self, entity_id, start=None, end=None, lookahead_days=21):
        return self.calendar_events.get(entity_id, [])


def message_state(subject, text, sender):
    return {
        "entity_id": MESSAGES_SENSOR,
        "state": "on",
        "last_changed": "2026-10-01T08:00:00+00:00",
        "attributes": {"subject": subject, "text": text, "sender": sender},
    }


def build_controller(tmp_path, client, **option_overrides):
    config = build_config(make_options(**option_overrides))
    object.__setattr__(config, "data_dir", str(tmp_path))
    return AulaUIController(config, client)


@pytest.fixture
def client():
    return FakeHA(
        states={
            MESSAGES_SENSOR: message_state(
                "OBS. Vigtig informationer",
                "<div>Her er lidt om Signe</div>",
                "Freja Holm Mikkelsen",
            ),
            WEEKPLAN_SENSOR: {"entity_id": WEEKPLAN_SENSOR, "state": "available", "attributes": {}},
        },
        calendar_events={
            CALENDAR: [
                {"summary": "Forældremøde", "start": {"dateTime": "2026-10-12T18:00:00+02:00"}},
                {"summary": "Sommerfest", "start": {"date": "2026-11-01"}},
            ]
        },
    )


def test_poll_cycle_writes_no_home_assistant_entities(tmp_path, client):
    client.states["input_boolean.aula_pin_thread_alma"] = {
        "entity_id": "input_boolean.aula_pin_thread_alma", "state": "on", "attributes": {},
    }
    controller = build_controller(tmp_path, client)
    controller.poll_cycle()

    assert client.written == {}
    assert controller._cycle_errors == []
    assert controller.store.child("alma")["last_message"]["title"] == "OBS. Vigtig informationer"


def test_last_message_is_html_stripped(tmp_path, client):
    controller = build_controller(tmp_path, client)
    controller.poll_cycle()

    latest = controller.store.child("alma")["last_message"]
    assert latest["original_text"] == "Her er lidt om Signe"
    assert latest["sender"] == "Freja Holm Mikkelsen"
    assert latest["title"] == "OBS. Vigtig informationer"


def test_events_are_classified(tmp_path, client):
    controller = build_controller(tmp_path, client)
    controller.poll_cycle()

    events = controller.store.child("alma")["events"]
    assert [e["type"] for e in events] == ["meeting", "party"]
    assert [e["id"] for e in events] == ["2026_10_12_foraeldremoede", "2026_11_01_sommerfest"]


def test_new_message_is_added_to_history(tmp_path, client):
    controller = build_controller(tmp_path, client)
    controller.poll_cycle()

    client.states[MESSAGES_SENSOR] = message_state(
        "Helt ny besked", "Noget helt andet sker nu", "Ny Afsender"
    )
    controller.poll_cycle()
    bucket = controller.store.child("alma")
    assert bucket["last_message"]["title"] == "Helt ny besked"
    assert [m["title"] for m in bucket["history"]] == ["Helt ny besked", "OBS. Vigtig informationer"]


def test_state_persists_across_restart(tmp_path, client):
    controller = build_controller(tmp_path, client)
    controller.poll_cycle()
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["children"]["alma"]

    fresh = FakeHA()
    restarted = build_controller(tmp_path, fresh)
    restarted.publish_feed()
    bucket = restarted.store.child("alma")
    assert bucket["last_message"]["title"] == "OBS. Vigtig informationer"
    assert len(bucket["events"]) == 2
    assert fresh.written == {}


def test_missing_messages_sensor_is_handled(tmp_path):
    empty = FakeHA()
    controller = build_controller(tmp_path, empty)
    controller.poll_cycle()
    assert controller.store.child("alma").get("last_message") is None
    assert controller._cycle_errors == []


def test_errors_are_reported_in_the_cycle(tmp_path, client):
    controller = build_controller(tmp_path, client)

    def boom(_entity_id):
        raise RuntimeError("kaboom")

    client.get_state = boom
    controller.poll_cycle()
    assert any("kaboom" in err for err in controller._cycle_errors)


# --------------------------------------------------------------------------
# Per-child attribution: the Aula API tells us which child a thread is for,
# so two children must never end up sharing one message.
# --------------------------------------------------------------------------

TWO_CHILDREN = [
    {
        "name": "Alma",
        "slug": "alma",
        "aula_name": "Alma Jensen",
        "messages_sensor": MESSAGES_SENSOR,
    },
    {
        "name": "Noah",
        "slug": "noah",
        "aula_name": "Noah Emil Jensen",
        "messages_sensor": MESSAGES_SENSOR,
    },
]


def _thread(tid, subject, names, read=True):
    return {
        "id": tid,
        "subject": subject,
        "read": read,
        "startedTime": "2026-10-01T07:00:00+00:00",
        "regardingChildren": [{"displayName": n, "profileId": i} for i, n in enumerate(names)],
        "creator": {"fullName": "Freja Holm Mikkelsen"},
        "latestMessage": {
            "id": f"m{tid}",
            "sendDateTime": "2026-10-01T07:00:00+00:00",
            "text": {"html": f"<div>{subject} body</div>"},
        },
    }


# Guardian/child institution profiles for TWO_CHILDREN, so posts and albums
# can be requested; both are empty in these message-focused fixtures.
_PROFILES = [{
    "institutionProfiles": [{"id": 1, "institutionCode": "X"}],
    "children": [
        {"id": 11, "profileId": 101, "name": "Noah Emil Jensen", "institutionCode": "X"},
        {"id": 12, "profileId": 102, "name": "Alma Jensen", "institutionCode": "X"},
    ],
}]


def _content_response(uri):
    """Empty posts/albums for the feed's non-message requests, else None."""
    if "profiles.getProfilesByLogin" in uri:
        return {"status": {"code": 0}, "data": {"profiles": _PROFILES}}
    if "posts.getAllPosts" in uri:
        return {"status": {"code": 0}, "data": {"posts": []}}
    if "gallery.getAlbums" in uri:
        return {"status": {"code": 0}, "data": []}
    return None


def _messages_unavailable(data):
    """Aula messaging is down; the rest of the feed still answers (empty)."""
    response = _content_response(data["uri"])
    if response is None:
        raise RuntimeError("Aula messaging unavailable")
    return response


def _aula_api_handler(data):
    uri = data["uri"]
    content = _content_response(uri)
    if content is not None:
        return content
    if "getThreads" in uri:
        if "page=0" in uri:
            return {
                "status": {"code": 0},
                "data": {
                    "moreMessagesExist": False,
                    "threads": [
                        _thread(1, "Ugebrev uge 40", ["Noah Emil Jensen"], read=False),
                        _thread(2, "Lukkedag i vuggestuen", ["Alma Jensen"]),
                    ],
                },
            }
        return {"status": {"code": 0}, "data": {"threads": []}}
    thread_id = int(uri.split("threadId=")[1].split("&")[0])
    subject = "Ugebrev uge 40" if thread_id == 1 else "Lukkedag i vuggestuen"
    return {
        "status": {"code": 0},
        "data": {
            "subject": subject,
            "messages": [
                {
                    "id": f"m{thread_id}",
                    "messageType": "Message",
                    "sendDateTime": "2026-10-01T07:00:00+00:00",
                    "sender": {"fullName": "Freja Holm Mikkelsen"},
                    "text": {"html": f"<div>{subject} body</div>"},
                }
            ],
        },
    }


@pytest.fixture
def thread_client():
    return FakeHA(
        states={
            MESSAGES_SENSOR: message_state(
                "Global latest message",
                "<div>Same for everyone</div>",
                "Freja Holm Mikkelsen",
            )
        },
        service_responses={"aula.api_call": _aula_api_handler},
    )


def test_children_get_their_own_threads(tmp_path, thread_client):
    controller = build_controller(tmp_path, thread_client, children=TWO_CHILDREN, message_source="auto")
    controller.poll_cycle()

    noah = controller.store.child("noah")["last_message"]
    alma = controller.store.child("alma")["last_message"]

    assert noah["title"] == "Ugebrev uge 40"
    assert alma["title"] == "Lukkedag i vuggestuen"
    assert noah["id"] != alma["id"]
    assert thread_client.written == {}


def test_falls_back_to_sensor_when_aula_api_unavailable(tmp_path):
    client = FakeHA(
        states={
            MESSAGES_SENSOR: message_state(
                "Global latest message",
                "<div>Same for everyone</div>",
                "Freja Holm Mikkelsen",
            )
        },
        service_responses={"aula.api_call": _messages_unavailable},
    )
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="auto")
    controller.poll_cycle()

    assert controller.store.child("noah")["last_message"]["title"] == "Global latest message"


def test_aula_api_only_mode_does_not_touch_the_sensor(tmp_path):
    client = FakeHA(
        states={
            MESSAGES_SENSOR: message_state(
                "Global latest message",
                "<div>Same for everyone</div>",
                "Freja Holm Mikkelsen",
            )
        },
        service_responses={"aula.api_call": _messages_unavailable},
    )
    controller = build_controller(
        tmp_path, client, children=TWO_CHILDREN, message_source="aula_api"
    )
    controller.poll_cycle()

    assert controller.store.child("noah").get("last_message") is None
