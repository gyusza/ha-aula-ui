"""Ordered HA-only failover applies to summaries, analysis and translations."""

import json
from types import SimpleNamespace

import pytest

from aula_ui.llm import LLMError, OrderedAITaskProvider, build_provider, translate_content


class Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call_service(self, domain, service, payload, **kwargs):
        assert (domain, service) == ("ai_task", "generate_data")
        entity = payload.get("entity_id", "default")
        self.calls.append(entity)
        value = self.responses[entity]
        if isinstance(value, Exception):
            raise value
        return {"service_response": {"data": value}}


SUMMARY = {"overview": "Useful summary.", "is_translated": True}
TRANSLATION = {"translated_text": "English text", "source_language": "en"}
ITEM = {"id": "post:1", "date": "2026-10-26", "title": "Title", "original_text": "Original"}


@pytest.mark.parametrize("operation,data", [
    ("summary", SUMMARY),
    ("analysis", {**SUMMARY, "events_json": "[]"}),
    ("translation", TRANSLATION),
])
@pytest.mark.parametrize("invalid", [RuntimeError("private failure"), {}])
def test_ordered_fallback_for_every_operation(operation, data, invalid):
    client = Client({"ai_task.first": invalid, "ai_task.second": data})
    provider = OrderedAITaskProvider(client, ["ai_task.first", "ai_task.second"])
    if operation == "summary":
        assert provider.summarise("prompt") == ("Useful summary.", True)
    elif operation == "analysis":
        assert provider.analyse(ITEM, "Child", "Europe/Copenhagen")[2] == []
    else:
        assert translate_content(provider, "English text", {})["translated_text"] == "English text"
    assert client.calls == ["ai_task.first", "ai_task.second"]
    assert provider.last_provider == "ai_task.second"
    client.responses["ai_task.first"] = data
    if operation == "summary":
        provider.summarise("prompt")
    elif operation == "analysis":
        provider.analyse(ITEM, "Child", "Europe/Copenhagen")
    else:
        translate_content(provider, "English text", {})
    assert client.calls[-1] == "ai_task.first"


def test_all_failures_are_explicit_and_never_leak_error_content(caplog):
    client = Client({"ai_task.first": RuntimeError("private-key"), "ai_task.second": {}})
    provider = OrderedAITaskProvider(client, ["ai_task.first", "ai_task.second"])
    with pytest.raises(LLMError, match="All configured Home Assistant AI tasks failed"):
        provider.summarise("private prompt")
    assert client.calls == ["ai_task.first", "ai_task.second"]
    assert "private-key" not in caplog.text and "private prompt" not in caplog.text


def test_direct_credentials_cannot_activate_direct_provider():
    config = SimpleNamespace(llm_provider="ha_ai_task", ai_task_entity="",
                             ai_task_entities=("ai_task.first",), anthropic_api_key="private")
    provider = build_provider(config, Client({}))
    assert isinstance(provider, OrderedAITaskProvider)
    assert provider.entity_ids == ("ai_task.first",)
    config.llm_provider = "anthropic"
    with pytest.raises(LLMError, match="Only Home Assistant"):
        build_provider(config, Client({}))


def test_empty_selection_follows_home_assistant_default():
    client = Client({"default": SUMMARY})
    assert OrderedAITaskProvider(client, []).summarise("prompt") == ("Useful summary.", True)
    assert client.calls == ["default"]
