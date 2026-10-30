"""Complete, durable translations and rich summaries without fabricated fallbacks."""

import json
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from aula_ui.aula_api import AulaApiError, fetch_thread_messages
from aula_ui.feed import ENRICHMENT_VERSION
from aula_ui.llm import (
    HomeAssistantAITaskProvider,
    LLMError,
    _translation_chunks,
    clean_output,
    enrich_content,
    render_prompt,
    translate_content,
    translate_text,
)
from test_controller import TWO_CHILDREN, build_controller
from test_feed import PostsClient, raw_post


def source_text(prompt):
    return prompt.split("BEGIN SOURCE\n", 1)[1].split("\nEND SOURCE", 1)[0]


class Translator:
    name = "command"

    def __init__(self):
        self.calls = []
        self.analyses = []
        self.fail = ""

    def generate(self, prompt):
        original = source_text(prompt)
        self.calls.append(original)
        if original == self.fail:
            return "not JSON"
        translations = {
            "Cykel tur": "Bike ride",
            "Vi mødes kl. 9. Medbring hjelm.": "We meet at 9. Bring a helmet.",
            "Tak. Vi kommer kl. 9.": "Thanks. We will come at 9.",
        }
        return json.dumps({"translated_text": translations.get(original, original),
                           "source_language": "da" if original in translations else "en"})

    def analyse(self, item, child, zone):
        self.analyses.append(item)
        return "First detail. Second detail. Third detail. Fourth detail.\n\nFifth detail.", True, []

    def summarise(self, prompt):
        return "First detail. Second detail. Third detail. Fourth detail.", True


RAW_MESSAGES = [
    {"id": "m2", "messageType": "Message", "sender": {"fullName": "Parent"},
     "sendDateTime": "2026-10-02T10:00:00Z", "text": {"html": "Tak. Vi kommer kl. 9."}},
    {"id": "m1", "messageType": "Message", "sender": {"fullName": "Teacher"},
     "sendDateTime": "2026-10-01T09:00:00Z", "text": {"html": "Vi mødes kl. 9. Medbring hjelm."}},
]

THREAD = {
    "id": 42, "subject": "Cykel tur", "regardingChildren": [{"displayName": "Alma Jensen"}],
    "latestMessage": RAW_MESSAGES[0], "creator": {"fullName": "Teacher"},
}


class ConversationClient(PostsClient):
    def __init__(self):
        super().__init__()
        self.message_calls = []
        self.messages = [dict(message) for message in RAW_MESSAGES]
        self.message_error = False

    def call_service(self, domain, service, data=None, **kwargs):
        query = parse_qs(data["uri"].lstrip("?"))
        if query["method"] == ["messaging.getMessagesForThread"]:
            page = int(query["page"][0])
            self.message_calls.append(page)
            if self.message_error:
                raise RuntimeError("Thread offline")
            return {"service_response": {"data": {
                "subject": "Cykel tur", "messages": self.messages[page:page + 1],
                "moreMessagesExist": page < len(self.messages) - 1,
            }}}
        return super().call_service(domain, service, data, **kwargs)


def controller_with_thread(tmp_path, monkeypatch):
    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    client = ConversationClient()
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[THREAD])
    controller.provider = Translator()
    return controller, client


@pytest.mark.parametrize("manual_retry", [False, True])
def test_backlog_batches_are_newest_first_even_when_restored_unsorted(
    tmp_path, monkeypatch, manual_retry,
):
    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    client = PostsClient({("11", 0): ([
        raw_post(index, title=f"Post {index}", date=f"2026-10-{index + 1:02d}T10:00:00Z")
        for index in range(8)
    ], False)})
    controller = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[])
    controller.provider = Translator()
    enrich = controller.feed.enrich
    monkeypatch.setattr(controller.feed, "enrich", lambda: None)
    controller.feed.update()
    items = controller.store.get("feed")
    controller.store.set("feed", list(reversed(items)))
    if manual_retry:
        controller.feed.request_reprocess("post:0")
    enrich()
    expected = [0, 7, 6, 5, 4, 3, 2, 1] if manual_retry else list(range(7, -1, -1))
    assert [item["id"] for item in controller.provider.analyses] == [
        f"post:{index}" for index in expected[:6]
    ]
    enrich()
    assert [item["id"] for item in controller.provider.analyses] == [
        f"post:{index}" for index in expected
    ]


def test_replies_translate_newest_first_without_reordering_originals():
    provider = Translator()
    messages = [
        {"id": "old", "date": "2026-10-02T10:00:00+02:00", "original_text": "Older reply"},
        {"id": "unknown", "date": "invalid", "original_text": "Undated reply"},
        {"id": "new", "date": "2026-10-02T09:00:00Z", "original_text": "Latest reply"},
    ]
    result = enrich_content(provider, {"title": "Thread title", "thread_messages": messages}, {})
    assert provider.calls == ["Thread title", "Latest reply", "Older reply", "Undated reply"]
    assert [message["id"] for message in result["thread_messages"]] == [
        message["id"] for message in messages
    ]
    assert all(message["translated_text"] == message["original_text"]
               for message in result["thread_messages"])


def test_targeted_reprocess_refreshes_summary_but_reuses_translations(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    translation_cache = dict(controller.store.get("feed_translations"))
    translation_calls = len(controller.provider.calls)
    analysis_calls = len(controller.provider.analyses)
    controller.feed._retry_at = float("inf")
    controller.feed.request_reprocess("thread:42")
    controller.feed.enrich()
    assert len(controller.provider.analyses) == analysis_calls + 1
    assert len(controller.provider.calls) == translation_calls
    assert controller.store.get("feed_translations") == translation_cache
    assert controller.store.get("feed_reprocess_requests") == []
    item = next(item for item in controller.store.get("feed") if item["id"] == "thread:42")
    assert item["summary"] and not item["analysis_pending"]
    with pytest.raises(ValueError, match="Unknown"):
        controller.feed.request_reprocess("thread:missing")
    item["thread_complete"] = False
    with pytest.raises(ValueError, match="complete conversation"):
        controller.feed.request_reprocess("thread:42")


def test_analysis_records_actual_failover_provider(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.provider.name = "ha_ai_task"
    controller.provider.last_provider = "anthropic"
    controller.feed.update()
    item = next(item for item in controller.store.get("feed") if item["id"] == "thread:42")
    assert item["source"] == "anthropic"
    assert controller.store.get("feed_summaries")["thread:42"]["source"] == "anthropic"


def test_full_thread_detail_and_summary_survive_restart(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert client.message_calls == [0, 1]
    assert item["title"] == "Cykel tur"
    assert item["translated_title"] == "Bike ride"
    assert item["original_text"] == RAW_MESSAGES[0]["text"]["html"]
    assert "Fifth detail" in item["summary"]
    messages = item["thread_messages"]
    assert [m["id"] for m in messages] == ["m2", "m1"]
    assert [m["sender"] for m in messages] == ["Parent", "Teacher"]
    assert [m["date"] for m in messages] == [m["sendDateTime"] for m in RAW_MESSAGES]
    assert messages[1]["original_text"] == RAW_MESSAGES[1]["text"]["html"]
    assert messages[1]["translated_text"] == "We meet at 9. Bring a helmet."
    assert messages[1]["is_translated"] is True
    assert messages[1]["source_language"] == "da"
    assert not item["translation_pending"] and not item["translation_error"]
    assert len(controller.provider.analyses[0]["thread_messages"]) == 2
    assert "Medbring hjelm" in controller.provider.analyses[0]["original_text"]
    assert controller.store.get("feed_summaries")["thread:42"]["analysis_version"] == ENRICHMENT_VERSION

    restored = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    restored.provider = Translator()
    restored.thread_source = SimpleNamespace(available=True, threads=[THREAD])
    restored.feed.update()
    assert restored.provider.calls == [] and restored.provider.analyses == []
    assert client.message_calls == [0, 1]
    assert json.loads(restored.feed_json)["items"] == [item]


def test_edited_only_thread_is_complete_and_translated(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    client.messages = [{**RAW_MESSAGES[1], "messageType": "MessageEdited"}]
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert item["thread_complete"]
    assert not item["thread_fetch_error"]
    assert item["translated_title"] == "Bike ride"
    assert len(item["thread_messages"]) == 1
    assert item["thread_messages"][0]["original_text"] == RAW_MESSAGES[1]["text"]["html"]
    assert item["thread_messages"][0]["translated_text"] == "We meet at 9. Bring a helmet."
    assert not item["translation_pending"]

def test_full_thread_fetch_is_not_limited_to_context_messages(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    client.messages = [{
        **RAW_MESSAGES[0], "id": f"m{index}",
        "text": {"html": f"Important message number {index}."},
    } for index in range(controller.config.context_messages + 8)]
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert len(item["thread_messages"]) == len(client.messages)
    assert len(client.message_calls) == len(client.messages)
    assert all(message["translated_text"] for message in item["thread_messages"])
    assert len(controller.provider.analyses[0]["thread_messages"]) == len(client.messages)


def test_legacy_short_summary_cache_is_regenerated(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    original = RAW_MESSAGES[0]["text"]["html"]
    legacy = hashlib.sha256(("Cykel tur" + original).encode()).hexdigest()
    controller.store.set("feed_summaries", {"thread:42": {
        "fingerprint": legacy, "analysis_version": 2, "source": "command",
        "summary": "Old three sentence teaser.", "is_translated": True,
    }})
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert "Fifth detail" in item["summary"]
    assert item["translated_title"] == "Bike ride"
    assert len(controller.provider.analyses) == 1


def test_partial_translation_failure_retries_only_missing_content(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("aula_ui.feed.time.monotonic", lambda: clock[0])
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.provider.fail = RAW_MESSAGES[1]["text"]["html"]
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert item["summary"]
    assert item["translated_title"] == "Bike ride"
    assert item["thread_messages"][0]["translated_text"]
    assert item["thread_messages"][1]["translated_text"] == ""
    assert item["thread_messages"][1]["translation_error"]
    assert item["translation_pending"]
    assert controller.feed.retry_delay == 60
    controller.provider.fail = ""
    clock[0] += 61
    controller.feed.enrich()
    assert len(controller.provider.analyses) == 1
    assert controller.provider.calls.count("Cykel tur") == 1
    assert controller.provider.calls.count(RAW_MESSAGES[0]["text"]["html"]) == 1
    assert not controller.feed.pending
    assert not json.loads(controller.feed_json)["errors"]


def test_partial_translations_resume_after_restart_without_repeating_summary(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.provider.fail = RAW_MESSAGES[1]["text"]["html"]
    controller.feed.update()
    restored = build_controller(tmp_path, client, children=TWO_CHILDREN, message_source="aula_api")
    restored.provider = Translator()
    restored.thread_source = SimpleNamespace(available=True, threads=[THREAD])
    restored.feed.update()
    assert restored.provider.analyses == []
    assert restored.provider.calls == [RAW_MESSAGES[1]["text"]["html"]]
    assert not json.loads(restored.feed_json)["items"][0]["translation_pending"]


def test_summary_failure_preserves_actual_error_during_backoff(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)

    def unavailable(*args):
        raise LLMError("Provider quota exhausted")

    controller.provider.analyse = unavailable
    controller.feed.update()
    assert json.loads(controller.feed_json)["items"][0]["analysis_error"] == "Provider quota exhausted"
    controller.feed.update()
    assert json.loads(controller.feed_json)["items"][0]["analysis_error"] == "Provider quota exhausted"


def test_incomplete_thread_fetch_is_visible_not_presented_as_complete(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    client.message_error = True
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert not item["thread_complete"]
    assert "Thread offline" in item["thread_fetch_error"]
    assert item["analysis_error"]
    assert item["translation_pending"]
    assert not item["thread_messages"][0]["translated_text"]
    assert controller.provider.analyses == []


def test_native_title_only_content_is_enriched(tmp_path, monkeypatch):
    monkeypatch.setattr("aula_ui.feed.AI_INTERVAL", 0)
    controller = build_controller(tmp_path, PostsClient(), children=TWO_CHILDREN, message_source="aula_api")
    controller.thread_source = SimpleNamespace(available=True, threads=[])
    controller.provider = Translator()
    for index, origin in enumerate(("Calendar", "Weekplan", "Homework")):
        controller.store.child("alma")["events"].append({
            "id": f"e{index}", "title": "Cykel tur", "description": "",
            "date": "2026-10-05", "type": "homework" if origin == "Homework" else "event",
            "origin": origin, "sender": "Aula teacher", "calendar_source": "aula",
        })
    controller.feed.update()
    items = json.loads(controller.feed_json)["items"]
    assert len(items) == 3
    assert all(item["title"] == "Cykel tur" and item["translated_title"] == "Bike ride" for item in items)
    assert all(item["sender"] == "Aula teacher" for item in items)
    assert not controller.provider.analyses  # Native events are not re-extracted by AI.


def test_thread_cache_refresh_detects_changes_to_older_replies(tmp_path, monkeypatch):
    wall_clock = [1000]
    monkeypatch.setattr("aula_ui.feed.time.time", lambda: wall_clock[0])
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    client.messages[1] = {**client.messages[1], "text": {"html": "Bring a helmet and lunch."}}
    wall_clock[0] += 301
    controller.feed.update()
    assert client.message_calls == [0, 1]  # Short-lived revalidation no longer refetches.
    wall_clock[0] += 24 * 3600
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert item["thread_messages"][1]["original_text"] == "Bring a helmet and lunch."
    assert item["thread_messages"][1]["translated_text"] == "Bring a helmet and lunch."
    assert len(controller.provider.analyses) == 2


def test_translation_chunking_never_discards_long_text():
    original = ("A detailed English paragraph with dates 21 and 2026.\n" * 400) + "Final essential instruction."
    assert "".join(_translation_chunks(original)) == original
    provider = Translator()
    translated = translate_text(provider, original, {})
    assert len(_translation_chunks(original)) > 5
    assert len(provider.calls) > 1
    assert translated.endswith("Final essential instruction.")
    assert translated.count("A detailed English paragraph") == 400
    assert translated == original


@pytest.mark.parametrize("response", [
    "not JSON", "[]", '{"translated_text": null}', '{"translated_text": ""}',
    '{"translated_text":"Cykel tur"}',
])
def test_invalid_translations_are_retried_but_never_fabricated(response):
    class Invalid:
        calls = 0

        def generate(self, prompt):
            self.calls += 1
            return response

    provider = Invalid()
    result = enrich_content(provider, {"title": "Cykel tur"}, {})
    assert provider.calls == 2
    assert result["translated_title"] == ""
    assert result["translation_pending"]
    assert result["translation_error"]


def test_numeric_detail_loss_and_summary_in_place_of_translation_are_rejected():
    class Invalid:
        def generate(self, prompt):
            return json.dumps({"translated_text": "Please come to school.", "source_language": "da"})

    with pytest.raises(LLMError, match="numeric"):
        translate_text(Invalid(), "Please come to school at 9.", {})
    with pytest.raises(LLMError, match="incomplete"):
        translate_text(Invalid(), "Detailed source text. " * 50, {})


def test_english_with_danish_name_is_not_falsely_rejected():
    original = "Hi Søren, the school trip is at 9."
    assert translate_text(Translator(), original, {}) == original


def test_home_assistant_translation_uses_structured_output():
    class Client:
        calls = []

        def call_service(self, domain, service, payload, **kwargs):
            self.calls.append(payload)
            return {"service_response": {"data": {
                "translated_text": "Bike ride", "source_language": "da",
            }}}

    client = Client()
    assert translate_text(HomeAssistantAITaskProvider(client), "Cykel tur", {}) == "Bike ride"
    assert "translated_text" in client.calls[0]["structure"]


def test_translation_progress_preserves_message_order_and_pending_states():
    snapshots = []
    result = enrich_content(Translator(), {"title": "Cykel tur", "thread_messages": [{
        "id": str(index), "original_text": f"English message {index}.",
        "sender": "Parent", "date": "",
    } for index in range(3)]}, {}, on_progress=lambda partial: snapshots.append(
        json.loads(json.dumps(partial))
    ))
    assert len(snapshots) == 4
    assert all([message["id"] for message in snapshot["thread_messages"]] == ["0", "1", "2"]
               for snapshot in snapshots)
    assert [sum(message["translation_pending"] for message in snapshot["thread_messages"])
            for snapshot in snapshots] == [3, 2, 1, 0]
    assert not result["translation_pending"]






def test_summary_cleaning_preserves_paragraphs_and_all_details():
    text = "[translated] First. Second. Third.\n\nFourth. Fifth. Sixth."
    summary, translated = clean_output(text)
    assert translated
    assert summary == "First. Second. Third.\n\nFourth. Fifth. Sixth."


def test_all_message_pages_are_deduplicated_and_keep_api_order():
    class Client:
        calls = []

        def call_service(self, domain, service, data, **kwargs):
            page = int(parse_qs(data["uri"].lstrip("?"))["page"][0])
            self.calls.append(page)
            return {"service_response": {"data": {
                "subject": "s", "messages": [RAW_MESSAGES[0]] if page == 0 else RAW_MESSAGES,
                "hasMoreMessages": page == 0,
            }}}

    client = Client()
    assert [m["id"] for m in fetch_thread_messages(client, 42)] == ["m2", "m1"]
    assert client.calls == [0, 1]


def test_repeating_incomplete_pagination_is_an_explicit_error():
    class Client:
        def call_service(self, *args, **kwargs):
            return {"service_response": {"data": {
                "messages": RAW_MESSAGES, "moreMessagesExist": True,
            }}}

    with pytest.raises(AulaApiError, match="repeated"):
        fetch_thread_messages(Client(), 42)


@pytest.mark.parametrize("original", ["Vi kommer kl. 9.", "Thanks, see you tomorrow!"])
def test_no_ai_provider_never_labels_originals_as_translated(original):
    result = enrich_content(None, {"title": "Cykel tur", "thread_messages": [{
        "id": "one", "original_text": original, "sender": "Parent", "date": "",
    }]}, {})
    assert result["translated_title"] == ""
    assert not result["translation_pending"]
    assert not result["translation_error"]
    assert result["translation_available"] is False
    assert result["thread_messages"][0]["translated_text"] == ""
    assert result["thread_messages"][0]["translation_status"] == "original"
    assert result["thread_messages"][0]["is_translated"] is None
    assert result["thread_messages"][0]["original_text"] == original
    assert not result["thread_messages"][0]["translation_error"]


class LanguageProvider:
    def __init__(self, language, output):
        self.language, self.output, self.calls = language, output, 0

    def generate(self, prompt):
        self.calls += 1
        return json.dumps({"source_language": self.language, "translated_text": self.output})


def test_english_is_copied_verbatim_even_when_provider_paraphrases():
    original = "  Hello Søren,\n\nPlease bring your OWN lunch at 09:30.\nThanks!  "
    provider = LanguageProvider("en", "Hi Soren! Pack lunch for half past nine.")
    cache = {}
    result = translate_content(provider, original, cache)
    assert result == {
        "translated_text": original, "source_language": "en", "is_translated": False,
    }
    assert provider.calls == 1
    assert translate_content(None, original, cache) == result
    assert provider.calls == 1


@pytest.mark.parametrize("original, language, english", [
    ("Cykel tur", "da", "Bike ride"),
    ("Fri leg", "da", "Free play"),
    ("Madpakke", "da", "Packed lunch"),
    ("Bonjour", "fr", "Hello"),
    ("Hola", "es", "Hello"),
    ("Bring a helmet. Vi tager afsted kl. 9.", "mixed",
     "Bring a helmet. We leave at 9."),
])
def test_semantic_language_decision_handles_short_and_mixed_sources(original, language, english):
    provider = LanguageProvider(language, english)
    result = translate_content(provider, original, {})
    assert result["translated_text"] == english
    assert result["source_language"] == language
    assert result["is_translated"] is True
    assert provider.calls == 1


def test_mixed_thread_has_truthful_individual_language_statuses(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    english = "Hi Søren,\n\nBring your OWN helmet.\nSee you soon!"
    client.messages[0] = {**client.messages[0], "text": {"html": english}}
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    en, da = item["thread_messages"]
    assert en["translated_text"] == en["original_text"]
    assert en["original_text"] == english.replace("\n\n", "\n")
    assert en["is_translated"] is False and en["source_language"] == "en"
    assert en["translation_status"] == "original"
    assert da["is_translated"] is True and da["source_language"] == "da"
    assert da["translation_status"] == "translated"
    assert item["is_translated"] is True


def test_all_english_thread_is_not_labeled_translated_even_if_summary_flag_wrong(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    client.messages = [{**RAW_MESSAGES[0], "text": {"html": "Thanks, see you tomorrow!"}}]
    controller.feed.update()
    item = json.loads(controller.feed_json)["items"][0]
    assert item["is_translated"] is False  # Mock summary provider incorrectly returns True.
    assert item["thread_messages"][0]["is_translated"] is False
    assert item["thread_messages"][0]["translation_status"] == "original"
    assert item["title_is_translated"] is True  # Danish title classified independently.


def test_old_string_translation_cache_is_not_trusted():
    import aula_ui.llm as llm

    original = "Hello, world!"
    old_key = hashlib.sha256(f"1\0{original}".encode()).hexdigest()
    new_key = hashlib.sha256(f"{llm.TRANSLATION_VERSION}\0{original}".encode()).hexdigest()
    provider = LanguageProvider("en", "A paraphrased greeting")
    cache = {old_key: "An old paraphrase", new_key: "An unclassified old string"}
    result = translate_content(provider, original, cache)
    assert result["translated_text"] == original
    assert result["is_translated"] is False
    assert provider.calls == 1
    assert cache[new_key]["source_language"] == "en"


def test_old_summary_version_is_preserved_without_automatic_reanalysis(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    cached = controller.store.get("feed_summaries")["thread:42"]
    cached.update(analysis_version=3, summary="Old exhaustive transcript")
    controller.feed.update()
    assert len(controller.provider.analyses) == 1
    item = json.loads(controller.feed_json)["items"][0]
    assert item["summary"] == "Old exhaustive transcript"
    assert all(message["is_translated"] is True for message in item["thread_messages"])


def test_provider_changes_and_restart_reuse_completed_work(tmp_path, monkeypatch):
    from aula_ui.feed import migrate_cached_content
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    original = controller.store.get("feed")[0]
    saved = controller.store.get("feed_summaries")["thread:42"]
    original["fingerprint"] = saved["fingerprint"] = "legacy-provider-dependent-fingerprint"
    saved.pop("content_fingerprint", None)
    migrate_cached_content(controller)
    assert saved["fingerprint"] != "legacy-provider-dependent-fingerprint"
    calls = len(controller.provider.calls)
    analyses = len(controller.provider.analyses)
    controller.provider._entity_id = "ai_task.different"
    controller.provider._model = "new model"
    controller.provider.name = "ha_ai_task"
    controller.feed.update()
    assert len(controller.provider.calls) == calls
    assert len(controller.provider.analyses) == analyses
    controller.store.save()
    controller.close()
    restarted, _ = controller_with_thread(tmp_path, monkeypatch)
    restarted.feed.update()
    assert restarted.provider.calls == []
    assert restarted.provider.analyses == []
    restarted.close()


def test_timezone_setting_migration_does_not_reprocess_unchanged_content(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    cached = json.loads(json.dumps(controller.store.get("feed_summaries")))
    controller.profiles.data["children"] = {
        child.slug: {"timezone": "Europe/Copenhagen"} for child in controller.profiles.children
    }
    controller.profiles.save()
    controller.store.save()
    controller.close()
    restarted, _ = controller_with_thread(tmp_path, monkeypatch)
    try:
        restarted.feed.update()
        assert restarted.store.get("feed_summaries") == cached
        assert restarted.provider.calls == []
        assert restarted.provider.analyses == []
        assert all("timezone" not in settings
                   for settings in restarted.profiles.data["children"].values())
    finally:
        restarted.close()


def test_disappearing_source_keeps_saved_work_and_reuses_it_when_returning(tmp_path, monkeypatch):
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    calls = len(controller.provider.calls)
    controller.thread_source.threads = []
    controller.feed.update()
    assert "thread:42" in controller.store.get("feed_summaries")
    controller.thread_source.threads = [THREAD]
    controller.feed.update()
    assert len(controller.provider.analyses) == 1
    assert len(controller.provider.calls) == calls
    controller.close()


def test_real_content_changes_still_reanalyse_without_retranslating_old_messages(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    previous_calls = list(controller.provider.calls)
    client.messages[0] = {**client.messages[0], "text": {"html": "A genuinely changed reply."}}
    controller.thread_source.threads = [{**THREAD, "latestMessage": client.messages[0]}]
    controller.feed.update()
    assert len(controller.provider.analyses) == 2
    assert controller.provider.calls[:len(previous_calls)] == previous_calls
    assert controller.provider.calls.count("Vi mødes kl. 9. Medbring hjelm.") == 1
    controller.close()


def test_legacy_cache_recovery_checks_exact_source_not_only_item_id(tmp_path, monkeypatch):
    from aula_ui.feed import migrate_cached_content
    controller, _ = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    item = controller.store.get("feed")[0]
    saved = controller.store.get("feed_summaries")[item["id"]]
    old = {
        "title": item["title"], "original_text": item["original_text"],
        "date": item["date"], "timezone": "Europe/Copenhagen",
        "provider": "ha_ai_task", "model": "claude-sonnet-4-5", "ai_task_entity": "",
        "messages": [{key: message.get(key, "") for key in ("id", "sender", "date", "original_text")}
                     for message in item["thread_messages"]],
        "thread_complete": True,
    }
    old_fingerprint = hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    saved["fingerprint"] = old_fingerprint
    saved.pop("content_fingerprint", None)
    migrate_cached_content(controller)
    assert saved["content_fingerprint"] == item["fingerprint"]
    saved["fingerprint"] = old_fingerprint
    saved.pop("content_fingerprint")
    item["original_text"] += " Real new content."
    migrate_cached_content(controller)
    assert "content_fingerprint" not in saved
    controller.close()


def test_summary_markdown_and_synthesis_instructions():
    raw = "[translated] **Current situation:** Friday is confirmed.\n\nBring:\n- **Helmet**\n- Lunch"
    assert clean_output(raw) == (raw.removeprefix("[translated] "), True)
    prompt = render_prompt("Family", [])
    assert "GENERAL SYNTHESIS" in prompt
    assert "latest developments" in prompt
    assert "NOT a transcript" in prompt
    assert "bullets ONLY for actionable" in prompt


@pytest.mark.parametrize("language", [None, "", True, "unknown", "xx", "English", 23])
def test_invalid_semantic_language_output_is_visible_not_guessed(language):
    provider = LanguageProvider(language, "Bike ride")
    result = enrich_content(provider, {"title": "Cykel tur"}, {})
    assert result["translated_title"] == ""
    assert result["title_is_translated"] is None
    assert result["translation_error"]
    assert provider.calls == 2


def test_thread_cache_survives_revision_churn_when_latest_message_is_cached(tmp_path, monkeypatch):
    wall_clock = [1000]
    monkeypatch.setattr("aula_ui.feed.time.time", lambda: wall_clock[0])
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    # Signed URLs inside the summary change on every login.
    controller.thread_source.threads = [{**THREAD, "latestMessage": {**RAW_MESSAGES[0], "pic": "sig=new"}}]
    wall_clock[0] += 900
    controller.feed.update()
    assert client.message_calls == [0, 1]
    assert json.loads(controller.feed_json)["items"][0]["thread_complete"]


def test_stale_thread_refreshes_are_capped_per_sync(tmp_path, monkeypatch):
    wall_clock = [1000]
    monkeypatch.setattr("aula_ui.feed.time.time", lambda: wall_clock[0])
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    wall_clock[0] += 25 * 3600
    controller.feed._stale_refreshes = 3
    item = controller.store.get("feed")[0]
    result = controller.feed._conversation(THREAD, {"id": "m3", "original_text": "x"})
    assert result["thread_complete"] and client.message_calls == [0, 1]
    assert item


def test_rate_limited_source_uses_cache_and_collapses_errors(tmp_path, monkeypatch):
    controller, client = controller_with_thread(tmp_path, monkeypatch)
    controller.feed.update()
    calls = list(client.message_calls)
    client.rate_limited = lambda: True
    client._rate_limit_error = lambda: RuntimeError("Aula is limiting requests (HTTP 429).")
    controller.thread_source.threads = [{**THREAD, "latestMessage": {**RAW_MESSAGES[0], "id": "new"}}]
    controller.feed.update()
    assert client.message_calls == calls
    item = json.loads(controller.feed_json)["items"][0]
    assert not item["thread_complete"]
    assert "HTTP 429" in item["thread_fetch_error"]
