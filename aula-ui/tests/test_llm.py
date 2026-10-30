"""Tests for prompt rendering, output cleaning and provider fallbacks."""

import pytest

from aula_ui.llm import (
    HomeAssistantAITaskProvider,
    LLMError,
    build_provider,
    clean_output,
    extractive_fallback,
    limit_sentences,
    render_prompt,
    summarise,
)

CONTEXT = [
    {
        "id": "2",
        "title": "Opgave 3",
        "original_text": "Følgende opgaver skal afleveres i morgen",
        "sender": "Lærer",
        "date": "2026-09-28T14:07:00+02:00",
    },
    {
        "id": "1",
        "title": "Velkommen",
        "original_text": "Vi glæder os til at se jer",
        "sender": "Skolen",
        "date": "2026-09-20T09:00:00+02:00",
    },
]


class OkProvider:
    """Stand-in for a text provider (Anthropic / command)."""

    name = "anthropic"

    def __init__(self, text):
        self.text = text
        self.prompts = []

    def summarise(self, prompt):
        self.prompts.append(prompt)
        return clean_output(self.text)


class FailingProvider:
    name = "anthropic"

    def summarise(self, prompt):
        raise LLMError("boom")


def test_render_prompt_includes_every_message():
    prompt = render_prompt("Alma", CONTEXT)
    assert "Alma" in prompt
    assert "Følgende opgaver skal afleveres i morgen" in prompt
    assert "Vi glæder os til at se jer" in prompt
    assert "no sentence cap" in prompt
    assert "No emojis" in prompt
    assert "Start directly with the substantive news" in prompt
    assert "Do not introduce or describe the briefing" in prompt
    assert "Opgave 3" in prompt


def test_limit_sentences_caps_at_three():
    text = "One. Two. Three. Four. Five."
    assert limit_sentences(text) == "One. Two. Three."
    assert limit_sentences("Only one") == "Only one"
    assert limit_sentences("") == ""


def test_clean_output_strips_translated_tag():
    overview, translated = clean_output("[translated] Homework is due Friday.")
    assert overview == "Homework is due Friday."
    assert translated is True


def test_clean_output_handles_markdown_wrapped_tag():
    overview, translated = clean_output("**[translated]** Homework is due Friday.")
    assert overview == "Homework is due Friday."
    assert translated is True


def test_clean_output_without_tag():
    overview, translated = clean_output("The trip is on Friday.")
    assert overview == "The trip is on Friday."
    assert translated is False


def test_clean_output_removes_emojis_and_code_fences():
    overview, _ = clean_output("```\nSchool starts 🎉 on Monday.\n```")
    assert "🎉" not in overview
    assert overview == "School starts on Monday."


def test_clean_output_preserves_all_sentences():
    overview, _ = clean_output("A. B. C. D.")
    assert overview == "A. B. C. D."


def test_extractive_fallback_builds_readable_text():
    text = extractive_fallback(CONTEXT)
    assert "Opgave 3" in text
    assert "Lærer" in text
    assert "Vi glæder os til at se jer" in text


def test_extractive_fallback_with_no_messages():
    assert extractive_fallback([]) == "No messages yet."


def test_summarise_uses_provider_output():
    provider = OkProvider("[translated] Homework is due Friday.")
    overview, translated, source = summarise(provider, "Alma", CONTEXT)
    assert overview == "Homework is due Friday."
    assert translated is True
    assert source == "anthropic"
    assert "Følgende" in provider.prompts[0]


def test_summarise_respects_explicit_provider_language_flag():
    provider = OkProvider("Homework is due Friday.")
    _, translated, _ = summarise(provider, "Alma", CONTEXT)
    assert translated is False


def test_summarise_does_not_flag_english_only_threads():
    english = [{"title": "Trip", "original_text": "The trip is on Friday.", "sender": "Office", "date": ""}]
    provider = OkProvider("The trip is on Friday.")
    _, translated, _ = summarise(provider, "Noah", english)
    assert translated is False


def test_summarise_falls_back_when_provider_fails():
    overview, translated, source = summarise(FailingProvider(), "Alma", CONTEXT)
    assert source == "fallback"
    assert translated is False
    assert "Opgave 3" in overview


def test_summarise_falls_back_when_no_provider():
    overview, _, source = summarise(None, "Alma", CONTEXT)
    assert source == "fallback"
    assert overview


def test_summarise_falls_back_on_empty_provider_output():
    overview, _, source = summarise(OkProvider("   "), "Alma", CONTEXT)
    assert source == "fallback"
    assert overview


@pytest.mark.parametrize("raw", ["[TRANSLATED] Hi.", "[ translated ] Hi.", "[translated]: Hi."])
def test_clean_output_tag_variants(raw):
    overview, translated = clean_output(raw)
    assert translated is True
    assert overview == "Hi."


# --------------------------------------------------------------------------
# Home Assistant AI Task provider: uses whichever AI the HA instance is
# configured to use, via ai_task.generate_data.
# --------------------------------------------------------------------------


class FakeHAClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def call_service(self, domain, service, data=None, return_response=False, timeout=None):
        self.calls.append((domain, service, data, return_response, timeout))
        if self.error:
            raise self.error
        return self.response


def ai_task_response(overview, is_translated):
    return {"service_response": {"data": {"overview": overview, "is_translated": is_translated}}}


def test_ai_task_provider_calls_generate_data_without_entity():
    client = FakeHAClient(ai_task_response("Homework is due Friday.", True))
    provider = HomeAssistantAITaskProvider(client)
    overview, translated = provider.summarise("prompt text")

    domain, service, data, return_response, _ = client.calls[0]
    assert (domain, service, return_response) == ("ai_task", "generate_data", True)
    # No entity_id => Home Assistant routes to its own configured default.
    assert "entity_id" not in data
    assert data["instructions"] == "prompt text"
    assert set(data["structure"]) == {"overview", "is_translated"}
    assert overview == "Homework is due Friday."
    assert translated is True


def test_ai_task_provider_honours_explicit_entity():
    client = FakeHAClient(ai_task_response("All good.", False))
    provider = HomeAssistantAITaskProvider(client, "ai_task.openai_ai_task")
    provider.summarise("prompt")
    assert client.calls[0][2]["entity_id"] == "ai_task.openai_ai_task"


def test_ai_task_provider_cleans_without_truncating_output():
    client = FakeHAClient(ai_task_response("A 🎉. B. C. D.", False))
    overview, _ = HomeAssistantAITaskProvider(client).summarise("p")
    assert overview == "A. B. C. D."


def test_ai_task_provider_coerces_string_booleans():
    client = FakeHAClient(ai_task_response("Hi.", "true"))
    _, translated = HomeAssistantAITaskProvider(client).summarise("p")
    assert translated is True


def test_ai_task_provider_errors_on_empty_overview():
    client = FakeHAClient(ai_task_response("", False))
    with pytest.raises(LLMError, match="empty overview"):
        HomeAssistantAITaskProvider(client).summarise("p")


def test_ai_task_provider_errors_on_missing_data():
    with pytest.raises(LLMError, match="no data"):
        HomeAssistantAITaskProvider(FakeHAClient({})).summarise("p")


def test_ai_task_provider_wraps_transport_errors():
    client = FakeHAClient(error=RuntimeError("connection refused"))
    with pytest.raises(LLMError, match="connection refused"):
        HomeAssistantAITaskProvider(client).summarise("p")


def test_summarise_falls_back_when_ai_task_fails():
    client = FakeHAClient(error=RuntimeError("nope"))
    overview, _, source = summarise(HomeAssistantAITaskProvider(client), "Alma", CONTEXT)
    assert source == "fallback"
    assert "Opgave 3" in overview


class _Cfg:
    llm_provider = "ha_ai_task"
    ai_task_entity = ""
    anthropic_api_key = ""
    model = "x"
    llm_command = ""


def test_build_provider_defaults_to_ha_ai_task():
    provider = build_provider(_Cfg(), FakeHAClient())
    assert isinstance(provider, HomeAssistantAITaskProvider)
    assert provider.name == "ha_ai_task"


def test_build_provider_ha_ai_task_requires_client():
    assert build_provider(_Cfg(), None) is None
