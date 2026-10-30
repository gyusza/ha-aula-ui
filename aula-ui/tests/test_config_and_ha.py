"""Tests for configuration parsing."""

import json

import pytest

from aula_ui.config import ConfigError, build_config, load_options

BASE_OPTIONS = {
    "ha_token": "token",
    "ha_url": "http://ha.local:8123/api",
    "llm_provider": "none",
    "data_source": "integration",
    "children": [
        {
            "name": "Alma",
            "slug": "alma",
            "messages_sensor": "binary_sensor.easyiq_messages",
            "weekplan_sensor": "sensor.easyiq_alma_jensen_weekplan",
            "calendars": ["calendar.easyiq_alma_jensen_weekplan"],
        }
    ],
}


def test_build_config_parses_children():
    config = build_config(dict(BASE_OPTIONS))
    assert config.ha_url == "http://ha.local:8123/api"
    assert len(config.children) == 1
    child = config.children[0]
    assert child.slug == "alma"
    assert child.thread_id == "alma"
    assert child.calendars == ("calendar.easyiq_alma_jensen_weekplan",)


def test_build_config_defaults():
    config = build_config(dict(BASE_OPTIONS))
    assert config.poll_interval == 900
    assert config.context_messages == 5
    assert config.event_lookahead_days == 21
    assert config.model == ""
    assert config.timezone == "Europe/Copenhagen"


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_build_config_accepts_central_timezone(zone):
    assert build_config(dict(BASE_OPTIONS, timezone=zone)).timezone == zone


@pytest.mark.parametrize("zone", ["Invalid/Place", "", None, 123, "../UTC", "/etc/localtime"])
def test_build_config_rejects_invalid_central_timezone(zone):
    with pytest.raises(ConfigError, match="'timezone'.*IANA"):
        build_config(dict(BASE_OPTIONS, timezone=zone))


def test_build_config_falls_back_to_supervisor(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    options = {k: v for k, v in BASE_OPTIONS.items() if k not in ("ha_url", "ha_token")}
    config = build_config(options)
    assert config.ha_url == "http://supervisor/core/api"
    assert config.ha_token == "supervisor-token"


def test_build_config_requires_credentials(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    options = {k: v for k, v in BASE_OPTIONS.items() if k not in ("ha_url", "ha_token")}
    with pytest.raises(ConfigError, match="credentials"):
        build_config(options)


def test_build_config_rejects_bad_slug():
    options = json.loads(json.dumps(BASE_OPTIONS))
    options["children"][0]["slug"] = "Jo-Hanna"
    with pytest.raises(ConfigError, match="slug"):
        build_config(options)


def test_build_config_rejects_duplicate_slugs():
    options = json.loads(json.dumps(BASE_OPTIONS))
    options["children"].append(dict(options["children"][0]))
    with pytest.raises(ConfigError, match="Duplicate"):
        build_config(options)


def test_build_config_rejects_missing_messages_sensor():
    options = json.loads(json.dumps(BASE_OPTIONS))
    options["children"][0]["messages_sensor"] = ""
    with pytest.raises(ConfigError, match="messages_sensor"):
        build_config(options)


def test_build_config_rejects_empty_children():
    options = dict(BASE_OPTIONS)
    options["children"] = []
    with pytest.raises(ConfigError, match="children"):
        build_config(options)


def test_build_config_rejects_out_of_range_poll_interval():
    options = dict(BASE_OPTIONS, poll_interval=5)
    with pytest.raises(ConfigError, match="poll_interval"):
        build_config(options)


def test_build_config_migrates_retired_command_provider_without_command():
    options = dict(BASE_OPTIONS, llm_provider="command", llm_command="")
    assert build_config(options).llm_provider == "ha_ai_task"


def test_build_config_accepts_command_provider():
    options = dict(BASE_OPTIONS, llm_provider="command", llm_command="paseo send --model {model}")
    config = build_config(options)
    assert config.llm_command == "" and config.llm_provider == "ha_ai_task"


def test_build_config_rejects_unknown_provider():
    options = dict(BASE_OPTIONS, llm_provider="openai")
    with pytest.raises(ConfigError, match="llm_provider"):
        build_config(options)


def test_load_options_missing_file_returns_empty(tmp_path):
    assert load_options(str(tmp_path / "nope.json")) == {}


def test_load_options_reads_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"poll_interval": 60}), encoding="utf-8")
    assert load_options(str(path)) == {"poll_interval": 60}
