"""The add-on must sync with no Aula/EasyIQ integrations installed."""

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest
import requests

from aula_ui.account_auth import (
    AuthenticationError, BoundedSession, Connection, InteractiveLoginClient, LoginFlow,
)
from aula_ui.aula_api import AulaApiError
from aula_ui.calendar_feed import CalendarFeed
from aula_ui.config import build_config
from aula_ui.controller import AulaUIController
from aula_ui.direct_source import DirectAulaClient
from aula_ui.sync_settings import SyncSettings
from aula_ui.web import start_server
from test_controller import FakeHA, make_options
from vendor.easyiq.aula_login_client.exceptions import OAuthError

# Captured before conftest disables slow-data cadence for ordinary feed tests.
REAL_REFRESH_SECONDS = SyncSettings.refresh_seconds

CHILDREN = [
    {"profile_id": "101", "name": "Maja Example", "relations": [
        {"profileId": 101, "id": 1001, "userId": 9001, "institutionCode": "S1", "name": "Maja Example"},
    ]},
    {"profile_id": "102", "name": "Emil Example", "relations": [
        {"profileId": 102, "id": 1002, "userId": 9002, "institutionCode": "S1", "name": "Emil Example"},
    ]},
]
TOKENS = {"access_token": "private-access-value", "refresh_token": "private-refresh-value", "expires_at": time.time() + 3600}
PROFILES = [{"institutionProfiles": [{"id": 2001, "institutionCode": "S1"}],
             "children": [child["relations"][0] for child in CHILDREN]}]
WIDGETS = {"pageConfiguration": {"widgetConfigurations": [{"widget": {"widgetId": "0128"}}]}}


def create_controller(path, children=None, client=None):
    config = build_config(make_options(data_source="direct", children=children or []))
    config = replace(config, data_dir=str(path))
    controller = AulaUIController(config, client or FakeHA())
    directory = path / "calendar"
    directory.parent.mkdir(parents=True, exist_ok=True)
    controller.calendar = CalendarFeed(controller, str(directory))
    return controller


@pytest.fixture
def controller(tmp_path):
    value = create_controller(tmp_path / "data")
    yield value
    value.close()


def install(controller, username="example-guardian"):
    mapped = controller.account._mapped_children(CHILDREN)
    controller.account.connection.install(username, TOKENS, mapped)
    controller.account._apply_children(mapped)


def patch_presence(monkeypatch, source, read):
    """Serve the single combined presence request from a per-child function."""
    monkeypatch.setattr(source, "presence_many",
                        lambda children: {child["profile_id"]: read(child) for child in children})


def source_handlers(controller, monkeypatch):
    source = controller.account.source
    source.context = WIDGETS
    monkeypatch.setattr(source, "discover", lambda: CHILDREN)
    monkeypatch.setattr(source, "native_events", lambda child, lookahead_days: [])
    patch_presence(monkeypatch, source, lambda child: {
        "status": "Present", "exit_with": "", "date": "2026-10-04T08:00:00Z",
        "present": True, "check_in_time": "08:00", "entry_time": "06:30", "exit_time": "17:00",
    })
    monkeypatch.setattr(source, "easyiq_events", lambda child, weeks: [
        DirectAulaClient._event({
            "id": 11, "title": "Family lesson", "itemType": 9,
            "start": "2026-11-09T09:00:00+01:00", "end": "2026-11-09T10:30:00+01:00",
            "location": "Classroom",
        }),
    ])

    def get(uri):
        method = uri.split("method=")[1].split("&")[0]
        values = {
            "presence.getPresenceStates": [
                {"uniStudentId": identifier, "state": 0} for identifier in (1001, 1002)
            ],
            "presence.getPresenceConfigurationByChildIds": [
                {"uniStudentId": identifier, "presenceConfiguration": {
                    "dashboardModuleSettings": [{
                        "presenceDashboardContext": "guardian_dashboard",
                        "presenceModules": [{"moduleType": "report_sick", "permission": "editable"}],
                    }],
                }} for identifier in (1001, 1002)
            ],
            "profiles.getProfilesByLogin": {"profiles": PROFILES},
            "messaging.getThreads": {"threads": [{
                "id": 81, "subject": "Maja message", "regardingChildren": [{"profileId": 101}],
                "latestMessage": {"id": 55, "text": {"html": "Bring lunch"}, "sendDateTime": "2026-10-03T08:00:00Z"},
                "read": False,
            }]},
            "messaging.getMessagesForThread": {"subject": "Maja message", "messages": [{
                "id": 55, "messageType": "Message", "text": {"html": "Bring lunch"},
                "sendDateTime": "2026-10-03T08:00:00Z", "sender": {"fullName": "Teacher"},
            }]},
            "posts.getAllPosts": {"posts": [{
                "id": 22, "title": "Shared announcement", "content": {"html": "Please bring lunch."},
                "publishAt": "2026-10-03T10:00:00Z",
            }]},
            "gallery.getAlbums": [],
        }
        return {"status": {"code": 0}, "data": values[method]}

    monkeypatch.setattr(source, "get", get)


def test_direct_is_default_and_allows_discovery_without_sensors():
    config = build_config({"ha_token": "token", "llm_provider": "none", "children": []})
    assert config.data_source == "direct"
    assert config.children == ()
    config = build_config({"ha_token": "token", "children": [{"name": "Maja", "slug": "maja"}]})
    assert config.children[0].messages_sensor == ""


def test_direct_sync_does_not_call_integration_services_or_read_their_entities(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    assert controller.config.children[0].slug == "child_101"

    def no_sensor_read(entity):
        pytest.fail(f"Direct mode tried to read an integration entity: {entity}")

    monkeypatch.setattr(controller.client, "get_state", no_sensor_read)
    controller.poll_cycle()
    assert controller.client.service_calls == []
    assert controller.account.last_sync
    data = json.loads(controller.feed_json)
    assert not data["errors"]
    post = next(item for item in data["items"] if item["content_type"] == "Post")
    assert post["child_slugs"] == ["child_101", "child_102"]
    message = next(item for item in data["items"] if item["content_type"] == "Message")
    assert message["child_slugs"] == ["child_101"]
    events = [item for item in data["items"] if item["content_type"] == "Weekplan"]
    assert events[0]["calendar_event"]["end"] == "2026-11-09T09:30:00+00:00"
    assert not any(item["content_type"] == "Presence" for item in data["items"])
    assert len(data["presence"]) == 2
    assert controller.client.written == {}
    assert controller.calendar.count == 1
    presence = controller.store.get("direct_presence:child_101")
    assert presence["status"] == "Present"
    assert presence["entry_time"] == "06:30"
    assert presence["exit_time"] == "17:00"


def test_daycare_profile_skips_easyiq_but_school_keeps_syncing(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    controller.profiles.update("child_102", {
        "display_name": "Daycare child", "institution_type": "daycare",
        "calendar_enabled": True,
    })
    controller.account.connection.value["children"][1]["institution_type"] = "school"
    original = controller.account.source.easyiq_events
    requested = []

    def school_only(child, weeks):
        assert child["profile_id"] == "101", "Daycare must not request EasyIQ"
        requested.append(child["profile_id"])
        return original(child, weeks)

    monkeypatch.setattr(controller.account.source, "easyiq_events", school_only)
    controller.poll_cycle()
    assert requested == ["101"]
    assert controller.account.last_sync
    data = json.loads(controller.feed_json)
    assert data["errors"] == []
    assert not any(item["content_type"] == "Presence" for item in data["items"])
    assert len(data["presence"]) == 2
    assert any("child_102" in item["child_slugs"] for item in data["items"] if item["content_type"] == "Post")
    assert controller.store.child("child_101")["events"]
    assert not controller.store.child("child_102").get("events")

    controller.profiles.update("child_102", {
        "display_name": "School child", "institution_type": "school",
        "calendar_enabled": True,
    })
    monkeypatch.setattr(controller.account.source, "easyiq_events",
                        lambda child, weeks: requested.append(child["profile_id"]) or original(child, weeks))
    controller.poll_cycle()
    assert requested == ["101", "101", "102"]
    assert controller.store.child("child_102")["events"]


def test_daycare_keeps_cached_dates_and_does_not_hide_school_errors(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    controller.poll_cycle()
    previous = list(controller.store.child("child_102")["events"])
    controller.profiles.update("child_102", {
        "display_name": "Daycare child", "institution_type": "daycare",
        "calendar_enabled": True,
    })

    def fail_school(child, weeks):
        assert child["profile_id"] == "101"
        raise AulaApiError("Aula request failed (HTTP 404).")

    monkeypatch.setattr(controller.account.source, "easyiq_events", fail_school)
    controller.poll_cycle()
    assert controller.account.errors == ["Maja Example / EasyIQ: Aula request failed (HTTP 404)."]
    assert controller.store.child("child_102")["events"] == previous
    assert not any("Emil" in error for error in json.loads(controller.feed_json)["errors"])


def test_processing_error_never_marks_direct_sync_as_successful(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)

    def fail(child):
        raise RuntimeError("Could not process child message")

    monkeypatch.setattr(controller, "process_messages", fail)
    controller.poll_cycle()
    assert not controller.account.last_sync
    assert any("Could not process child message" in error for error in controller.account.errors)


def test_configured_slugs_profiles_archives_and_ics_secret_survive_own_login(tmp_path):
    children = [
        {"name": "Maja", "slug": "existing_slug", "aula_name": "Maja Example"},
        {"name": "Emil", "slug": "emil", "aula_child_id": "102", "institution_type": "daycare"},
    ]
    controller = create_controller(tmp_path, children)
    try:
        token = controller.profiles.calendar_path
        controller.profiles.update("existing_slug", {"display_name": "Family name", "institution_type": "school",
                                   "calendar_enabled": True})
        controller.profiles.set_event_archived("existing-event-uid", True)
        install(controller)
        assert [child.slug for child in controller.config.children] == ["existing_slug", "emil"]
        assert controller.profiles.snapshot()[0]["display_name"] == "Family name"
        assert controller.profiles.snapshot()[1]["institution_type"] == "daycare"
        assert controller.profiles.archived_events() == {"existing-event-uid"}
        assert controller.profiles.calendar_path == token
    finally:
        controller.close()
    restored = create_controller(tmp_path, children)
    try:
        assert restored.account.connection.value
        assert restored.config.children[0].aula_child_id == "101"
        assert restored.profiles.calendar_path == token
        assert restored.profiles.snapshot()[0]["display_name"] == "Family name"
        assert b"private-access-value" not in restored.account.vault.path.read_bytes()
        assert restored.account.vault.path.stat().st_mode & 0o777 == 0o600
    finally:
        restored.close()


def test_wrong_guardian_or_changed_child_access_cannot_replace_cached_family(controller):
    install(controller)
    with pytest.raises(AuthenticationError, match="access changed"):
        controller.account._mapped_children(CHILDREN[:1])
    with pytest.raises(ValueError, match="same guardian"):
        controller.account.login.start("different-guardian")


def test_unmatched_configured_child_never_guesses_identity(tmp_path):
    controller = create_controller(tmp_path, [{"name": "Other child", "slug": "other"}])
    try:
        with pytest.raises(AuthenticationError, match="every configured child"):
            controller.account._mapped_children(CHILDREN)
    finally:
        controller.close()


def test_legacy_profile_pins_institution_type_without_guessing_from_slug(tmp_path):
    children = [{"name": "Alma", "slug": "alma"}]
    fresh = create_controller(tmp_path / "fresh", children)
    try:
        assert fresh.profiles.snapshot()[0]["institution_type"] == "school"
    finally:
        fresh.close()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "profiles.json").write_text(json.dumps({"children": {}, "calendar_token": "existing-subscription"}))
    migrated = create_controller(legacy, children)
    try:
        assert migrated.profiles.snapshot()[0]["institution_type"] == "school"
        assert migrated.profiles.data["profile_version"] == 2
        assert migrated.profiles.calendar_path.endswith("existing-subscription.ics")
    finally:
        migrated.close()


def test_not_logged_in_keeps_feed_and_calendar_history(controller):
    controller.store.set("feed", [{"id": "post:cached", "title": "Cached", "child_slugs": [], "date": ""}])
    controller.poll_cycle()
    data = json.loads(controller.feed_json)
    assert data["items"][0]["title"] == "Cached"
    assert "Account login" in data["errors"][0]
    assert not controller.account.last_sync
    assert not controller.client.service_calls


def test_failed_source_retains_last_successful_events(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    controller.poll_cycle()
    previous = controller.store.child("child_101")["events"]

    def failed(*args, **kwargs):
        raise AulaApiError("EasyIQ is unavailable")

    monkeypatch.setattr(controller.account.source, "easyiq_events", failed)
    controller.poll_cycle()
    assert controller.store.child("child_101")["events"] == previous
    assert controller.account.errors
    assert any("EasyIQ is unavailable" in error for error in json.loads(controller.feed_json)["errors"])


def test_ingress_account_endpoints_do_not_expose_tokens(controller):
    install(controller)
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    url = f"http://127.0.0.1:{service.server_port}"
    try:
        result = requests.get(url + "/api/account", timeout=5)
        assert result.status_code == 200
        assert "private-access-value" not in result.text and "private-refresh-value" not in result.text
        assert result.json()["connected"] is True
        html = requests.get(url + "/account", timeout=5).text
        assert 'src="account.js"' in html and 'href="style.css"' in html
        assert "img-src 'self' data:" in result.headers["Content-Security-Policy"]
        assert requests.post(url + "/api/auth/start", json={"username": "example"}, timeout=5).status_code == 403
        for value in ({}, {"username": 42}, {"username": "example", "password": "not allowed"}):
            assert requests.post(url + "/api/auth/start", json=value,
                                 headers={"X-Aula-Request": "settings"}, timeout=5).status_code == 400
        assert requests.post(url + "/api/auth/identity", json={"index": True},
                             headers={"X-Aula-Request": "settings"}, timeout=5).status_code == 400
        assert requests.post(url + "/api/auth/cancel", json={},
                             headers={"X-Aula-Request": "settings"}, timeout=5).status_code == 202
        for path in ("/aula_auth/connection.enc", "/aula_auth/vault.key", "/account_auth.py"):
            assert requests.get(url + path, timeout=5).status_code == 404
    finally:
        service.shutdown()
        service.server_close()


def test_own_login_approval_is_interactive_and_failures_are_explicit(controller, monkeypatch):
    approved = threading.Event()
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.session = requests.Session()

        def authenticate(self):
            approved.wait(3)
            return {"success": True, "tokens": TOKENS}

        def get_mitid_client(self):
            return None

    monkeypatch.setattr(controller.account, "connect", lambda username, tokens, cancelled: calls.append(username))
    controller.account.login = LoginFlow(controller.account, Client)
    controller.account.login.start("example")
    assert controller.account.login.snapshot()["status"] == "running"
    assert not calls
    with pytest.raises(ValueError, match="already running"):
        controller.account.login.start("parallel")
    approved.set()
    controller.account.login.worker.join(3)
    assert calls == ["example"]
    assert controller.account.login.snapshot()["status"] == "complete"


def test_token_refresh_persists_own_rotated_tokens(controller):
    install(controller)

    class Response:
        status_code = 200

        def json(self):
            return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    class Session:
        def post(self, url, **kwargs):
            assert kwargs["data"]["grant_type"] == "refresh_token"
            return Response()

    connection = Connection(controller.account.vault, Session())
    assert connection.access_token(force=True) == "new-access"
    assert controller.account.vault.read()["tokens"]["refresh_token"] == "new-refresh"


def test_login_callback_state_and_cancel_deadlines():
    client = InteractiveLoginClient(mitid_username="example", auth_method="APP")
    client.state = "expected"
    try:
        for url in ("https://app-private.aula.dk?code=x",
                    "https://evil.invalid?" + urlencode({"code": "x", "state": "expected"}),
                    "https://app-private.aula.dk?code=x&state=wrong"):
            with pytest.raises(OAuthError):
                client.step8_exchange_oauth_code(url)
    finally:
        client.session.close()
    cancelled = threading.Event()
    cancelled.set()
    with BoundedSession(cancelled) as session:
        with pytest.raises(AuthenticationError, match="cancelled"):
            session.get("https://www.aula.dk")
    with BoundedSession(deadline=time.monotonic() - 1) as session:
        with pytest.raises(AuthenticationError, match="timed out"):
            session.get("https://www.aula.dk")


class Response:
    def __init__(self, value, status=200, headers=None):
        self.status_code = status
        self.value = value
        self.headers = headers or {}

    def json(self):
        return self.value


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def close(self):
        pass


def payload(value):
    return Response({"status": {"code": 0}, "data": value})


def test_direct_discovery_upgrades_aula_api_and_keeps_child_identifiers(controller):
    install(controller)
    session = Session(Response({}, 410), payload({"profiles": PROFILES}), payload({
        "userId": 5001, "institutionProfile": {"id": 2001},
    }))
    source = DirectAulaClient(controller.account.connection, session)
    assert source.discover() == CHILDREN
    assert source.version == 23
    assert source.context["userId"] == 5001
    assert session.calls[0][0].endswith("/v22")
    assert session.calls[1][0].endswith("/v23")
    assert ("access_token", TOKENS["access_token"]) in session.calls[1][1]["params"]
    source.session = Session(Response({"invalid": True}))
    with pytest.raises(AulaApiError, match="invalid response"):
        source.get("?method=anything")


def test_direct_presence_and_easyiq_contract_reuses_valid_variant_for_empty_weeks(controller):
    install(controller)
    session = Session(payload([{
        "institutionProfile": {"id": 1001}, "status": 3, "checkInTime": "08:00",
        "entryTime": "06:30", "exitTime": "17:00", "exitWith": "Guardian",
    }]), payload("widget-token"), Response([
        {"id": 81, "title": "Homework", "itemType": 4,
         "start": "2026-10-09T09:00:00+02:00", "end": "2026-10-09T10:00:00+02:00"},
    ]), Response([]))
    source = DirectAulaClient(controller.account.connection, session)
    source.context = {**WIDGETS, "userId": 5001, "institutionProfile": {"id": 2001}}
    presence = source.presence(CHILDREN[0])
    assert presence["status"] == "Present" and presence["present"] is True
    assert presence["check_in_time"] == "08:00" and presence["exit_time"] == "17:00"
    events = source.easyiq_events(CHILDREN[0], weeks=2)
    assert events[0]["type"] == "homework"
    assert events[0]["end"] == "2026-10-09T10:00:00+02:00"
    assert len(session.calls) == 4
    assert ("method", "aulaToken.getAulaToken") in session.calls[1][1]["params"]
    assert not any(("method", "aulaToken.getWidgets") in kwargs.get("params", []) for _, kwargs in session.calls)
    assert session.calls[2][1]["headers"]["Authorization"] == "Bearer widget-token"
    assert session.calls[2][1]["headers"]["x-childfilter"] == "9001"
    assert session.calls[2][1]["params"]["loginId"] == "1001"
    assert source.calendar_variants["1001"] == ("example-guardian", "1001", "9001")
    assert DirectAulaClient._event({"title": "No end", "start": "2026-10-09T09:00:00Z"})["end"] == ""
    with pytest.raises(AulaApiError, match="valid start"):
        DirectAulaClient._event({"title": "Missing date"})


def test_direct_presence_preserves_recorded_naps(controller):
    install(controller)
    source = DirectAulaClient(controller.account.connection, Session(payload([{
        "institutionProfile": {"id": 1001}, "status": 5,
        "sleepIntervals": [
            {"id": 1, "startTime": "09:00:00", "endTime": "09:45:00"},
            {"id": 2, "startTime": "12:10:00", "endTime": None},
        ],
    }])))
    presence = source.presence(CHILDREN[0])
    assert presence["status"] == "Napping"
    assert presence["sleep_intervals"] == [
        {"start": "09:00:00", "end": "09:45:00"},
        {"start": "12:10:00", "end": ""},
    ]


@pytest.mark.parametrize("intervals", [{}, "invalid", [None], [{"startTime": 12}]])
def test_direct_presence_rejects_invalid_nap_payload(controller, intervals):
    install(controller)
    source = DirectAulaClient(controller.account.connection, Session(payload([{
        "institutionProfile": {"id": 1001}, "status": 5, "sleepIntervals": intervals,
    }])))
    with pytest.raises(AulaApiError, match="invalid nap intervals"):
        source.presence(CHILDREN[0])


def test_missing_easyiq_widget_is_explicit_and_keeps_cached_dates(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    controller.poll_cycle()
    previous = list(controller.store.child("child_101")["events"])
    source = controller.account.source
    source.context = {"pageConfiguration": {"widgetConfigurations": [
        {"widget": {"widgetId": "0004", "name": "Other provider"}},
    ]}}
    monkeypatch.setattr(source, "easyiq_events", DirectAulaClient.easyiq_events.__get__(source))
    controller.poll_cycle()
    assert controller.account.errors == []
    assert controller.account.last_sync
    assert controller.store.child("child_101")["events"] == previous
    status = controller.account.snapshot()["planning"]["child_101"]
    assert status["status"] == "not_available"
    assert "does not offer" in status["message"]
    assert controller.store.get("direct_planning:child_101") == status


@pytest.mark.parametrize("context", [{}, {"pageConfiguration": {}},
                                    {"pageConfiguration": {"widgetConfigurations": [None]}}])
def test_invalid_widget_metadata_is_not_treated_as_unavailable(controller, context):
    install(controller)
    source = DirectAulaClient(controller.account.connection, Session())
    source.context = context
    with pytest.raises(AulaApiError, match="availability information"):
        source.easyiq_events(CHILDREN[0])


def test_available_easyiq_widget_failure_remains_explicit(controller):
    install(controller)
    source = DirectAulaClient(controller.account.connection, Session(Response({}, 503)))
    source.context = WIDGETS
    with pytest.raises(AulaApiError, match="HTTP 503"):
        source.easyiq_events(CHILDREN[0])


def test_rate_limit_pauses_all_requests_briefly_and_the_endpoint_longer(controller):
    import time
    from aula_ui.aula_api import AulaRateLimited
    install(controller)
    session = Session(Response({}, 429, {"Retry-After": "5"}), payload({}))
    source = DirectAulaClient(controller.account.connection, session)
    with pytest.raises(AulaRateLimited, match="HTTP 429"):
        source.get("?method=calendar.getEvents")
    assert source.rate_limited()
    assert 55 <= source._paused_until - time.time() <= 60
    with pytest.raises(AulaRateLimited):
        source.get("?method=messaging.getThreads")
    assert len(session.calls) == 1
    source._paused_until = 0  # the short global pause has passed
    source.get("?method=messaging.getThreads")
    with pytest.raises(AulaRateLimited):
        source.get("?method=calendar.getEvents")
    assert len(session.calls) == 2
    assert 890 <= source._limits["calendar.getEvents"]["until"] - time.time() <= 900
    assert source.rate_limited("calendar.getEvents") and not source.rate_limited("messaging.getThreads")


def test_rate_limit_honours_longer_retry_after(controller):
    import time
    from aula_ui.aula_api import AulaRateLimited
    install(controller)
    source = DirectAulaClient(controller.account.connection, Session(Response({}, 429, {"Retry-After": "3600"})))
    with pytest.raises(AulaRateLimited):
        source.get("?method=anything")
    assert 3590 <= source._paused_until - time.time() <= 3600
    assert 3590 <= source._limits["anything"]["until"] - time.time() <= 3600


def test_repeated_rate_limits_escalate_and_success_resets(controller):
    import time
    from aula_ui.aula_api import AulaRateLimited
    install(controller)
    session = Session(Response({}, 429), Response({}, 429), Response({}, 429), payload({}), Response({}, 429))
    source = DirectAulaClient(controller.account.connection, session)

    def expire():
        source._paused_until = 0
        source._limits["anything"]["until"] = 0
    pauses = []
    for _ in range(3):
        with pytest.raises(AulaRateLimited):
            source.get("?method=anything")
        pauses.append(round(source._limits["anything"]["until"] - time.time(), -1))
        expire()
    assert pauses == [900, 1800, 3600]
    source.get("?method=anything")
    assert "anything" not in source._limits
    with pytest.raises(AulaRateLimited):
        source.get("?method=anything")
    assert round(source._limits["anything"]["until"] - time.time(), -1) == 900


def test_rate_limit_pause_is_capped_and_survives_restart(controller):
    import time
    from aula_ui.aula_api import AulaRateLimited
    install(controller)
    source = controller.account.source
    source.session = Session(Response({}, 429, {"Retry-After": "999999"}))
    with pytest.raises(AulaRateLimited):
        source.get("?method=anything")
    assert source._paused_until - time.time() <= DirectAulaClient.RATE_LIMIT_MAX_PAUSE
    saved = controller.store.get("aula_rate_limit")
    assert saved["methods"]["anything"]["strikes"] == 1 and saved["paused_until"] == source._paused_until
    controller.close()
    restarted_controller = create_controller(Path(controller.config.data_dir))
    try:
        restarted = restarted_controller.account.source
        session = Session()
        restarted.session = session
        assert restarted.rate_limited() and restarted._limits["anything"]["strikes"] == 1
        with pytest.raises(AulaRateLimited, match="HTTP 429"):
            restarted.get("?method=anything")
        assert session.calls == []
    finally:
        restarted_controller.close()


def test_unreadable_rate_state_is_ignored(controller):
    source = DirectAulaClient(controller.account.connection, Session())
    source.restore_rate_state({"paused_until": "x", "methods": {"a": {"until": 1}}})
    assert source._paused_until == 0 and source._limits == {}
    source.restore_rate_state({"paused_until": 5, "strikes": 1})  # 1.9.40-pre format
    assert source._paused_until == 5 and source._limits == {}


def test_background_budget_stops_before_contacting_aula(controller, monkeypatch):
    from aula_ui.aula_api import AulaBudgetExceeded
    install(controller)
    monkeypatch.setattr(DirectAulaClient, "BACKGROUND_PER_HOUR", 2)
    monkeypatch.setattr(DirectAulaClient, "TOTAL_PER_HOUR", 3)
    session = Session(payload({}), payload({}), payload({}))
    source = DirectAulaClient(controller.account.connection, session)
    with source.background():
        source.get("?method=a")
        source.get("?method=b")
        with pytest.raises(AulaBudgetExceeded, match="request budget"):
            source.get("?method=c")
    assert len(session.calls) == 2
    assert not source.rate_limited() and not source._limits
    source.get("?method=d")  # in-app requests keep a reserve beyond the background budget
    with pytest.raises(AulaBudgetExceeded):
        source.get("?method=e")
    assert len(session.calls) == 3


def test_requests_are_spaced(controller, monkeypatch):
    import aula_ui.direct_source as module
    install(controller)
    monkeypatch.setattr(DirectAulaClient, "MIN_INTERVAL", 1.0)
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    source = DirectAulaClient(controller.account.connection, Session(payload({}), payload({})))
    source.get("?method=a")
    source.get("?method=b")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0


def test_profile_lookups_are_cached_per_login(controller):
    install(controller)
    session = Session(payload({"profiles": PROFILES}), payload({"x": 1}))
    source = DirectAulaClient(controller.account.connection, session)
    first = source.get("?method=profiles.getProfilesByLogin")
    first["data"]["profiles"] = []
    assert source.get("?method=profiles.getProfilesByLogin")["data"]["profiles"] == PROFILES
    assert len(session.calls) == 1
    source.get("?method=messaging.getThreads")
    assert len(session.calls) == 2
    install(controller, username="other-guardian")
    session.responses = iter([payload({"profiles": []})])
    assert source.get("?method=profiles.getProfilesByLogin")["data"]["profiles"] == []
    assert len(session.calls) == 3


def test_slow_aula_data_is_refreshed_on_a_cadence(controller, monkeypatch):
    import aula_ui.feed as feed_module
    install(controller)
    monkeypatch.setattr(SyncSettings, "refresh_seconds", REAL_REFRESH_SECONDS)
    calendar_calls, posts_calls, album_calls = [], [], []

    def native(child, days):
        calendar_calls.append(child["profile_id"])
        if len(calendar_calls) == 1:
            raise AulaApiError("temporary")
        return []
    monkeypatch.setattr(controller.account.source, "native_events", native)
    monkeypatch.setattr(feed_module, "fetch_posts", lambda *args: posts_calls.append(1) or [])
    monkeypatch.setattr(feed_module.gallery, "feed_items", lambda *args: album_calls.append(1) or [])
    child = controller.config.children[0]
    with pytest.raises(AulaApiError):
        controller.process_events(child)  # a failed fetch is retried next cycle
    controller.process_events(child)
    controller.process_events(child)
    assert len(calendar_calls) == 2
    for _ in range(3):
        controller.feed.update()
    assert posts_calls == [1] and album_calls == [1]
    stamps = controller.store.get("sync_stamps")
    controller.store.set("sync_stamps", {key: value - 1000 for key, value in stamps.items()})
    controller.process_events(child)
    controller.feed.update()
    assert len(calendar_calls) == 2 and posts_calls == [1, 1] and album_calls == [1]


def test_presence_for_every_child_is_one_request(controller):
    install(controller)
    session = Session(payload([
        {"institutionProfile": {"id": 1001}, "status": 3, "checkInTime": "08:00"},
        {"institutionProfile": {"id": 1002}, "status": 5,
         "sleepIntervals": [{"startTime": "12:00:00", "endTime": None}]},
    ]))
    source = DirectAulaClient(controller.account.connection, session)
    result = source.presence_many(CHILDREN)
    assert len(session.calls) == 1
    params = session.calls[0][1]["params"]
    assert ("childIds[]", "1001") in params and ("childIds[]", "1002") in params
    assert result["101"]["status"] == "Present" and result["101"]["check_in_time"] == "08:00"
    assert result["102"]["status"] == "Napping"
    assert result["102"]["sleep_intervals"] == [{"start": "12:00:00", "end": ""}]


def test_easyiq_planning_follows_the_slow_cadence(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    monkeypatch.setattr(SyncSettings, "refresh_seconds", REAL_REFRESH_SECONDS)
    calls = []
    monkeypatch.setattr(controller.account.source, "easyiq_events",
                        lambda child, weeks: calls.append(child["slug"]) or [])
    controller.account.collect()
    controller.account.collect()
    assert sorted(calls) == ["child_101", "child_102"]
    stamps = controller.store.get("sync_stamps")
    controller.store.set("sync_stamps", {key: value - 2000 for key, value in stamps.items()})
    controller.account.collect()
    assert len(calls) == 4


def quick_setup(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    controller.poll_cycle()
    source = controller.account.source
    requests_made = []
    plain_get = source.get
    monkeypatch.setattr(source, "get", lambda uri: requests_made.append(uri.split("method=")[1].split("&")[0])
                        or plain_get(uri))
    plain_presence = source.presence_many
    monkeypatch.setattr(source, "presence_many",
                        lambda children: requests_made.append("presence") or plain_presence(children))
    monkeypatch.setattr(controller, "_quick_due", lambda kind: True)
    return requests_made


def test_quick_check_spots_new_messages_with_one_request(controller, monkeypatch):
    requests_made = quick_setup(controller, monkeypatch)
    controller._refresh.clear()
    controller.quick_check()
    assert requests_made == ["presence", "messaging.getThreads"]
    assert not controller._refresh.is_set()
    source = controller.account.source
    plain_get = source.get

    def newer(uri):
        result = plain_get(uri)
        if "messaging.getThreads" in uri:
            result["data"]["threads"][0]["latestMessage"]["id"] = 56
        return result
    monkeypatch.setattr(source, "get", newer)
    controller.quick_check()
    assert controller._refresh.is_set()


def test_quick_check_publishes_presence_changes(controller, monkeypatch):
    quick_setup(controller, monkeypatch)
    published = []
    plain_publish = controller.publish_feed
    monkeypatch.setattr(controller, "publish_feed", lambda: published.append(1) or plain_publish())
    controller.quick_check()
    assert published == [1]  # unchanged state is republished with a fresh check time
    patch_presence(monkeypatch, controller.account.source, lambda child: {
        "status": "Napping", "exit_with": "", "date": "", "present": True,
        "sleep_intervals": [{"start": "12:00:00", "end": ""}],
    })
    controller.quick_check()
    assert published == [1, 1]
    assert {row["status"] for row in json.loads(controller.feed_json)["presence"]} == {"Napping"}


def test_quick_check_respects_guardrails_and_quiet_hours(controller, monkeypatch):
    requests_made = quick_setup(controller, monkeypatch)
    source = controller.account.source
    source._paused_until = time.time() + 60
    controller.quick_check()
    source._paused_until = 0
    monkeypatch.setattr(AulaUIController, "QUICK_RESERVE", DirectAulaClient.BACKGROUND_PER_HOUR + 1)
    controller.quick_check()
    monkeypatch.setattr(AulaUIController, "QUICK_RESERVE", 40)
    monkeypatch.setattr(controller, "_quick_due", lambda kind: False)
    controller.quick_check()
    assert requests_made == []


def test_quick_windows_follow_local_time(controller, monkeypatch):
    import aula_ui.controller as module

    class Clock(module.datetime):
        current = None

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)
    monkeypatch.setattr(module, "datetime", Clock)
    Clock.current = module.datetime(2026, 10, 29, 6, 30, tzinfo=module.timezone.utc)  # 07:30 Thursday
    assert controller._quick_due("presence")
    Clock.current = module.datetime(2026, 10, 31, 9, 0, tzinfo=module.timezone.utc)  # Saturday
    assert not controller._quick_due("presence")
    assert controller._quick_due("messages")
    controller.set_sync_settings({"presence_weekdays_only": False, "quick_messages": False})
    assert controller._quick_due("presence") and not controller._quick_due("messages")
    controller.set_sync_settings({"quick_messages": True, "messages_from_hour": 11})
    assert not controller._quick_due("messages")
    Clock.current = module.datetime(2026, 10, 29, 23, 30, tzinfo=module.timezone.utc)  # 00:30 local
    assert not controller._quick_due("messages")


def test_run_loop_quick_checks_between_full_syncs(controller, monkeypatch):
    monkeypatch.setattr(SyncSettings, "full_sync_seconds", 3600)
    monkeypatch.setattr(SyncSettings, "quick_seconds", 0.01)
    cycles, checks = [], []
    monkeypatch.setattr(controller, "poll_cycle", lambda: cycles.append(1))

    def check():
        checks.append(1)
        if len(checks) == 2:
            controller.request_refresh()  # a new message starts a full sync at once
        if len(checks) == 4:
            controller._stop.set()
    monkeypatch.setattr(controller, "quick_check", check)
    worker = threading.Thread(target=controller._run_loop)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(cycles) == 2 and len(checks) == 4


def test_quick_check_skips_a_paused_endpoint_without_contacting_aula(controller, monkeypatch):
    install(controller)
    source = controller.account.source
    session = Session()
    source.session = session
    patch_presence(monkeypatch, source, lambda child: None)
    controller.thread_source._threads = [{"id": 1, "latestMessage": {"id": 2}, "read": True}]
    controller.thread_source.available = True
    source._limits["messaging.getThreads"] = {"until": time.time() + 900, "strikes": 1}
    monkeypatch.setattr(controller, "_quick_due", lambda kind: True)
    controller._refresh.clear()
    controller.quick_check()
    assert session.calls == [] and not controller._refresh.is_set()
