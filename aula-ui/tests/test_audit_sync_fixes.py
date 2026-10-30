"""Regression tests for sync robustness issues seen in production logs."""

import json
import logging
import os
import time
from pathlib import Path

import pytest

from aula_ui.aula_api import AulaApiError, AulaRateLimited, _call, fetch_thread_messages
from aula_ui.diagnostics import observed
from aula_ui.direct_source import DirectAulaClient
from aula_ui.state import StateStore
from test_direct_account import (
    CHILDREN, PROFILES, Response, Session, create_controller, install, patch_presence, payload, source_handlers,
)


@pytest.fixture
def controller(tmp_path):
    value = create_controller(tmp_path / "data")
    yield value
    value.close()


def test_working_api_version_is_learned_and_reused_after_restart(controller, caplog):
    install(controller)
    source = controller.account.source
    session = Session(Response({}, 410), Response({}, 410), payload({"threads": []}))
    source.session = session
    with caplog.at_level(logging.DEBUG, logger="aula_ui.direct_source"):
        source.get("?method=messaging.getThreads")
    assert [call[0][-3:] for call in session.calls] == ["v22", "v23", "v24"]
    assert controller.store.get("aula_api_version") == 24
    # Probing a retired version is expected and must not be logged as a warning.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "status=410" in r.getMessage()]
    controller.close()
    restarted = create_controller(Path(controller.config.data_dir))
    try:
        session = Session(payload({"threads": []}))
        restarted.account.source.session = session
        restarted.account.source.get("?method=messaging.getThreads")
        assert len(session.calls) == 1 and session.calls[0][0].endswith("/v24")
    finally:
        restarted.close()


def test_invalid_remembered_api_version_is_ignored(controller):
    source = DirectAulaClient(controller.account.connection, Session())
    for value in ("24", True, 3, 999, None):
        source.restore_version(value)
        assert source.version == 22


def test_login_discovery_starts_at_learned_version_and_survives_auth_retry(controller, monkeypatch):
    import aula_ui.account as account_module
    controller.account.source.version = 24
    sessions = []

    class Recorder(Session):
        def __init__(self):
            super().__init__(Response({}, 401), payload({"profiles": PROFILES}),
                             payload({"userId": 1, "institutionProfile": {"id": 2001}}))
            sessions.append(self)

    monkeypatch.setattr(account_module, "DirectAulaClient",
                        lambda connection: DirectAulaClient(connection, Recorder()))
    tokens = {"access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 3600}
    controller.account.connect("example-guardian", tokens)
    calls = sessions[0].calls
    assert all(url.endswith("/v24") for url, _ in calls)
    assert len(calls) == 3  # the 401 retry no longer crashes with a TypeError
    assert controller.account.connection.value["username"] == "example-guardian"


def test_call_preserves_rate_limit_type_and_does_not_mention_removed_integration(controller):
    install(controller)
    source = controller.account.source
    source._limits["messaging.getMessagesForThread"] = {"until": time.time() + 900, "strikes": 1}
    session = Session()
    source.session = session
    for thread_id in range(5):
        with pytest.raises(AulaRateLimited) as caught:
            fetch_thread_messages(source, thread_id)
        assert "aula.api_call" not in str(caught.value)
        assert "HTTP 429" in str(caught.value)
    assert session.calls == []

    class Broken:
        def call_service(self, *args, **kwargs):
            raise OSError("socket closed")
    with pytest.raises(AulaApiError) as caught:
        _call(Broken(), "?method=x")
    assert "aula.api_call" not in str(caught.value)


def test_sync_with_cached_discovery_and_expired_access_token_keeps_working(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    connection = controller.account.connection
    connection.value["tokens"]["expires_at"] = time.time() - 30
    connection.vault.write(connection.value)
    vault_file = connection.vault.path
    before = vault_file.stat().st_mtime_ns
    time.sleep(0.01)
    assert controller.account.collect() == []
    # Unchanged children are not rewritten to the encrypted vault on every sync.
    assert vault_file.stat().st_mtime_ns == before
    assert connection.value["tokens"]["refresh_token"] == "private-refresh-value"


def test_rate_limited_calendar_is_not_an_error_with_stack_and_keeps_cached_events(controller, monkeypatch, caplog):
    install(controller)
    source_handlers(controller, monkeypatch)
    controller.poll_cycle()
    cached = list(controller.store.child("child_101")["events"])
    assert cached

    def limited(child, days):
        raise AulaRateLimited("Aula is limiting requests (HTTP 429). Cached content is shown.")
    monkeypatch.setattr(controller.account.source, "native_events", limited)
    caplog.clear()
    controller.poll_cycle()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and r.name == "aula_ui.controller"]
    assert controller.store.child("child_101")["events"] == cached
    assert any("HTTP 429" in error for error in controller.account.errors)


def test_rate_limited_direct_sync_is_a_warning(controller, monkeypatch, caplog):
    install(controller)
    monkeypatch.setattr(controller.account, "collect",
                        lambda: (_ for _ in ()).throw(AulaRateLimited("Aula is limiting requests (HTTP 429).")))
    controller.poll_cycle()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert controller.account.errors == ["Aula is limiting requests (HTTP 429)."]


def test_expected_errors_are_not_traced_as_warnings(caplog):
    @observed("demo")
    def paused():
        raise AulaRateLimited("paused")

    @observed("demo")
    def broken():
        raise ValueError("bad")
    for function in (paused, broken):
        with pytest.raises(Exception):
            function()
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "error_type=ValueError" in warnings[0]


def test_exhausted_budget_warns_once(controller, monkeypatch, caplog):
    from aula_ui.aula_api import AulaBudgetExceeded
    install(controller)
    monkeypatch.setattr(DirectAulaClient, "TOTAL_PER_HOUR", 1)
    source = DirectAulaClient(controller.account.connection, Session(payload({})))
    source.get("?method=a")
    for _ in range(5):
        with pytest.raises(AulaBudgetExceeded):
            source.get("?method=b")
    assert len([r for r in caplog.records if "budget_exhausted" in r.getMessage()
                and r.levelno == logging.WARNING]) == 1


def test_quick_check_skips_paused_presence_without_marking_it_failed(controller, monkeypatch):
    install(controller)
    source_handlers(controller, monkeypatch)
    controller.poll_cycle()
    source = controller.account.source
    calls = []
    patch_presence(monkeypatch, source, lambda child: calls.append(child) or None)
    source._limits["presence.getDailyOverview"] = {"until": time.time() + 900, "strikes": 1}
    monkeypatch.setattr(controller, "_quick_due", lambda kind: kind == "presence")
    controller.quick_check()
    assert calls == []
    assert not controller.store.get("presence_sync:child_101").get("failed")


def test_feed_publish_builds_presence_once(controller, monkeypatch):
    import aula_ui.controller as module
    install(controller)
    calls = []
    plain = module.presence_snapshot
    monkeypatch.setattr(module, "presence_snapshot", lambda value: calls.append(1) or plain(value))
    controller.publish_feed()
    assert calls == [1]


def test_state_save_survives_concurrent_mutation_and_leaves_no_temp_files(tmp_path, monkeypatch):
    import aula_ui.state as module
    store = StateStore(str(tmp_path))
    store.set("feed", [{"id": "a"}])
    real_dumps = json.dumps
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("dictionary changed size during iteration")
        return real_dumps(*args, **kwargs)
    monkeypatch.setattr(module.json, "dumps", flaky)
    store.save()
    monkeypatch.setattr(module.json, "dumps", real_dumps)
    assert json.loads((tmp_path / "state.json").read_text())["feed"] == [{"id": "a"}]

    def fail_replace(*args):
        raise OSError("disk full")
    monkeypatch.setattr(module.os, "replace", fail_replace)
    store.save()
    assert sorted(os.listdir(tmp_path)) == ["state.json"]
