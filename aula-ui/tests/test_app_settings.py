"""App configuration simplification and in-app AI entity selection."""

import json
from pathlib import Path

import pytest
import requests
import yaml

from aula_ui.llm import OrderedAITaskProvider, HomeAssistantAITaskProvider
from aula_ui.web import start_server
from test_controller import FakeHA
from test_direct_account import CHILDREN, create_controller, install

ROOT = Path(__file__).resolve().parents[1]
GOOGLE = {"entity_id": "ai_task.google_ai_task", "state": "2026-10-26T00:00:00+00:00",
          "attributes": {"friendly_name": "Google AI Task"}}
OPENAI = {"entity_id": "ai_task.openai_ai_task", "state": "unknown",
          "attributes": {"friendly_name": "OpenAI AI Task"}}
BROKEN = {"entity_id": "ai_task.broken", "state": "unavailable", "attributes": {"friendly_name": "Broken AI"}}
SETTINGS = {"X-Aula-Request": "settings"}
# Optional own-key / Cloudflare AI credentials; never given defaults.
AI_KEY_OPTIONS = {"anthropic_api_key", "anthropic_model", "google_api_key", "google_model",
                  "cloudflare_account_id", "cloudflare_api_token", "cloudflare_model"}


@pytest.fixture(autouse=True)
def fast_shutdown(monkeypatch):
    from aula_ui import web
    monkeypatch.setattr(web, "SERVE_POLL_INTERVAL", 0.01)


def ai_controller(path, client=None, **options):
    from dataclasses import replace
    from aula_ui.config import build_config
    from aula_ui.controller import AulaUIController
    from test_controller import make_options

    config = build_config(make_options(data_source="direct", children=[], llm_provider="ha_ai_task", **options))
    return AulaUIController(replace(config, data_dir=str(path)), client or FakeHA())


def test_app_schema_only_offers_necessary_options():
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert set(config["schema"]) == set(config["options"]) | AI_KEY_OPTIONS
    assert set(config["options"]) == {
        "event_lookahead_days", "thread_pages", "log_level", "log_color",
        "access_team_domain", "access_aud", "access_allowed_emails", "public_url", "timezone", "mcp_key", "mcp_allow_writes",
    }
    assert all(config["schema"][key] in ("password?", "str?") for key in AI_KEY_OPTIONS)
    assert config["options"]["timezone"] == "Europe/Copenhagen"


def test_profile_form_has_no_editable_timezone():
    script = (ROOT / "aula_ui" / "static" / "app.js").read_text()
    profile_page = script.split('if (key.startsWith("profile/"))', 1)[1]
    form = profile_page.split('const form = el("form", "profile-form page-panel");', 1)[1].split(
        'container.append(form);', 1
    )[0]
    assert "timezone" not in form.lower()
    assert "calendar_enabled: enabled.checked" in form


def test_discovered_slugs_survive_removed_children_option_and_lost_connection(tmp_path):
    legacy = [{"name": "Alma", "slug": "alma", "aula_name": "Maja Example"},
              {"name": "Noah", "slug": "noah", "aula_name": "Emil Example"}]
    controller = create_controller(tmp_path, legacy)
    try:
        controller.profiles.update("alma", {"display_name": "Alma", "institution_type": "daycare",
                                               "calendar_enabled": True})
        install(controller)
    finally:
        controller.close()
    # Supervisor drops the retired children option from options.json.
    upgraded = create_controller(tmp_path, [])
    try:
        assert [child.slug for child in upgraded.config.children] == ["alma", "noah"]
        assert upgraded.profiles.snapshot()[0]["institution_type"] == "daycare"
    finally:
        upgraded.close()
    # Even without the encrypted connection, remembered identities keep slugs on re-login.
    (tmp_path / "aula_auth" / "connection.enc").unlink()
    relogin = create_controller(tmp_path, [])
    try:
        assert [child.slug for child in relogin.config.children] == ["alma", "noah"]
        mapped = relogin.account._mapped_children(CHILDREN)
        assert [(entry["slug"], entry["profile_id"]) for entry in mapped] == [("alma", "101"), ("noah", "102")]
        assert relogin.profiles.snapshot()[0]["institution_type"] == "daycare"
    finally:
        relogin.close()


def test_new_install_discovers_without_configured_children(tmp_path):
    controller = create_controller(tmp_path, [])
    try:
        assert controller.config.children == ()
        install(controller)
        identities = json.loads((tmp_path / "profiles.json").read_text())["child_identities"]
        assert [entry["aula_child_id"] for entry in identities] == ["101", "102"]
    finally:
        controller.close()


def test_ai_entity_seeded_from_retired_option_then_owned_by_profiles(tmp_path):
    controller = ai_controller(tmp_path, ai_task_entity="ai_task.google_ai_task")
    try:
        assert controller.profiles.ai_task_entity == "ai_task.google_ai_task"
        assert controller.provider._entity_id == "ai_task.google_ai_task"
    finally:
        controller.close()
    restarted = ai_controller(tmp_path)
    try:
        assert restarted.config.ai_task_entity == "ai_task.google_ai_task"
    finally:
        restarted.close()


def test_ordered_priority_persists_and_save_failure_rolls_back(tmp_path, monkeypatch):
    client = FakeHA(states={s["entity_id"]: s for s in (GOOGLE, OPENAI, BROKEN)})
    controller = ai_controller(tmp_path, client)
    controller.set_ai_task_entities(["ai_task.openai_ai_task", "ai_task.google_ai_task"])
    controller.close()
    restarted = ai_controller(tmp_path, client)
    try:
        assert restarted.provider.entity_ids == ("ai_task.openai_ai_task", "ai_task.google_ai_task")
        def fail():
            raise OSError("disk unavailable")
        monkeypatch.setattr(restarted.profiles, "save", fail)
        with pytest.raises(OSError):
            restarted.set_ai_task_entities(["ai_task.google_ai_task"])
        assert restarted.provider.entity_ids == ("ai_task.openai_ai_task", "ai_task.google_ai_task")
        assert restarted.profiles.ai_task_entities == restarted.provider.entity_ids
    finally:
        restarted.close()


@pytest.fixture
def served(tmp_path):
    client = FakeHA(states={s["entity_id"]: s for s in (GOOGLE, OPENAI, BROKEN)})
    controller = ai_controller(tmp_path, client)
    service = start_server(controller, port=0, allowed_ip="127.0.0.1")
    yield controller, f"http://127.0.0.1:{service.server_port}"
    service.shutdown()
    service.server_close()
    controller.close()


def test_ai_settings_lists_entities_and_switches_provider_live(served):
    controller, url = served
    data = requests.get(url + "/api/ai-settings", timeout=5).json()
    assert data["ai_task_entity"] == "" and data["applies"] and data["error"] == ""
    assert [(e["entity_id"], e["available"]) for e in data["entities"]] == [
        ("ai_task.broken", False), ("ai_task.google_ai_task", True), ("ai_task.openai_ai_task", True)]
    response = requests.post(url + "/api/ai-settings", json={"ai_task_entities": ["ai_task.openai_ai_task", "ai_task.google_ai_task"]},
                             headers=SETTINGS, timeout=5)
    assert response.status_code == 200 and response.json()["ai_task_entity"] == "ai_task.openai_ai_task"
    assert isinstance(controller.provider, HomeAssistantAITaskProvider)
    assert controller.provider._entity_id == "ai_task.openai_ai_task"
    assert json.loads(Path(controller.config.data_dir, "profiles.json").read_text())["ai_settings"] == {
        "ai_task_entities": ["ai_task.openai_ai_task", "ai_task.google_ai_task"],
        "providers": ["ai_task.openai_ai_task", "ai_task.google_ai_task"]}
    assert controller.provider.entity_ids == ("ai_task.openai_ai_task", "ai_task.google_ai_task")
    assert requests.post(url + "/api/ai-settings", json={"ai_task_entities": []}, headers=SETTINGS,
                         timeout=5).status_code == 200
    assert controller.provider._entity_id == ""
    assert requests.get(url + "/ai-settings.js", timeout=5).status_code == 200


def test_learn_more_web_search_entity_is_separate_setting(served):
    controller, url = served
    assert requests.post(url + "/api/ai-settings", json={"ai_task_entities": ["ai_task.google_ai_task"]},
                         headers=SETTINGS, timeout=5).status_code == 200
    assert controller.reading_provider() is None
    response = requests.post(url + "/api/ai-settings", json={"reading_entity": "ai_task.openai_ai_task"},
                             headers=SETTINGS, timeout=5)
    assert response.status_code == 200 and response.json()["reading_entity"] == "ai_task.openai_ai_task"
    assert json.loads(Path(controller.config.data_dir, "profiles.json").read_text())["ai_settings"] == {
        "ai_task_entities": ["ai_task.google_ai_task"], "providers": ["ai_task.google_ai_task"],
            "reading_entity": "ai_task.openai_ai_task"}
    assert controller.reading_provider()._entity_id == "ai_task.openai_ai_task"
    assert requests.post(url + "/api/ai-settings", json={"ai_task_entities": ["ai_task.openai_ai_task"]},
                         headers=SETTINGS, timeout=5).status_code == 200
    assert controller.profiles.reading_entity == "ai_task.openai_ai_task"
    for body in ({"reading_entity": "ai_task.missing"}, {"reading_entity": 3},
                 {"reading_entity": "light.kitchen"}, {"reading_entity": "", "ai_task_entities": []}):
        assert requests.post(url + "/api/ai-settings", json=body, headers=SETTINGS, timeout=5).status_code == 400
    assert requests.post(url + "/api/ai-settings", json={"reading_entity": ""}, headers=SETTINGS,
                         timeout=5).status_code == 200
    assert controller.reading_provider() is None
    assert list(controller.profiles.ai_task_entities) == ["ai_task.openai_ai_task"]


@pytest.mark.parametrize("body,status", [
    ({"ai_task_entities": ["ai_task.missing"]}, 400),
    ({"ai_task_entities": ["light.kitchen"]}, 400),
    ({"ai_task_entities": ["ai_task.google_ai_task", "ai_task.google_ai_task"]}, 400),
    ({"ai_task_entities": "ai_task.google_ai_task"}, 400),
    ({"ai_task_entities": [3]}, 400),
    ({"ai_task_entities": [], "extra": 1}, 400),
])
def test_ai_settings_rejects_invalid_selection(served, body, status):
    controller, url = served
    assert requests.post(url + "/api/ai-settings", json=body, headers=SETTINGS, timeout=5).status_code == status
    assert controller.profiles.ai_task_entity == ""
    assert requests.post(url + "/api/ai-settings", json={"ai_task_entities": []}, timeout=5).status_code == 403


def test_unavailable_selected_entity_is_reported_not_replaced(served):
    controller, url = served
    controller.set_ai_task_entity("ai_task.google_ai_task")
    controller.client.states["ai_task.google_ai_task"] = {**GOOGLE, "state": "unavailable"}
    data = requests.get(url + "/api/ai-settings", timeout=5).json()
    assert data["ai_task_entity"] == "ai_task.google_ai_task"
    assert data["selected_available"] is False and "unavailable" in data["error"]
    assert controller.provider._entity_id == "ai_task.google_ai_task"
    del controller.client.states["ai_task.google_ai_task"]
    assert "Missing" in requests.get(url + "/api/ai-settings", timeout=5).json()["error"]


def test_ai_settings_never_exposes_secret_and_keeps_failover(tmp_path):
    secret = "sk-ant-private-value"
    controller = ai_controller(tmp_path, FakeHA(states={GOOGLE["entity_id"]: GOOGLE}), anthropic_api_key=secret)
    try:
        controller.set_ai_task_entity("ai_task.google_ai_task")
        assert isinstance(controller.provider, OrderedAITaskProvider)
        assert controller.provider.providers[0]._entity_id == "ai_task.google_ai_task"
        data = controller.ai_settings()
        assert secret not in json.dumps(data) and not data["fallback"]["configured"]
        assert controller.provider.name != "anthropic"
        assert secret not in (tmp_path / "profiles.json").read_text()
    finally:
        controller.close()


def test_ai_settings_listing_failure_is_explicit(served):
    controller, url = served

    def fail():
        raise RuntimeError("HA down")
    controller.client.list_states = fail
    data = requests.get(url + "/api/ai-settings", timeout=5).json()
    assert data["entities"] == [] and data["error"].startswith("Could not load")
    response = requests.post(url + "/api/ai-settings", json={"ai_task_entities": ["ai_task.google_ai_task"]},
                             headers=SETTINGS, timeout=5)
    assert response.status_code == 503


def test_unified_provider_list_with_keys_entered_in_the_app(served):
    controller, url = served
    post = lambda body: requests.post(url + "/api/ai-settings", json=body, headers=SETTINGS, timeout=5)
    data = requests.get(url + "/api/ai-settings", timeout=5).json()
    assert data["providers"] == [] and [s["configured"] for s in data["sources"]] == [False, False, False]
    assert post({"ai_providers": ["google"]}).status_code == 400
    assert post({"ai_credential": {"source": "google", "api_key": "bad key"}}).status_code == 400
    assert post({"ai_credential": {"source": "cloudflare", "api_key": "secret-c"}}).status_code == 400
    response = post({"ai_credential": {"source": "google", "api_key": "secret-g"}})
    assert response.status_code == 200 and "secret" not in response.text
    assert [s["configured"] for s in response.json()["sources"]] == [False, True, False]
    assert post({"ai_credential": {"source": "cloudflare", "api_key": "secret-c",
                                   "account_id": "0123456789abcdef0123456789abcdef"}}).status_code == 200
    response = post({"ai_providers": ["google", "ai_task.openai_ai_task", "cloudflare"]})
    assert response.status_code == 200 and "secret" not in response.text
    assert response.json()["providers"] == ["google", "ai_task.openai_ai_task", "cloudflare"]
    assert [p.name for p in controller.provider.providers] == ["google", "ha_ai_task", "cloudflare"]
    assert controller.profiles.ai_task_entities == ("ai_task.openai_ai_task",)
    assert post({"ai_providers": ["bogus"]}).status_code == 400
    assert post({"ai_credential": {"source": "google", "clear": True}}).status_code == 200
    assert controller.profiles.ai_providers == ("ai_task.openai_ai_task", "cloudflare")
    assert post({"ai_providers": []}).status_code == 200
    assert controller.provider.name == "ha_ai_task"
