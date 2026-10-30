"""Priority fallbacks supersede speculative direct-provider parallel work."""

from aula_ui.llm import OrderedAITaskProvider, translate_content
from test_controller import FakeHA, build_controller


def test_large_backlog_does_not_consume_fallback_when_primary_succeeds(tmp_path):
    controller = build_controller(tmp_path, FakeHA())
    calls = []
    class Client:
        def call_service(self, domain, service, payload, **kwargs):
            calls.append(payload["entity_id"])
            return {"service_response": {"data": {
                "source_language": "en", "translated_text": "English text",
            }}}
    controller.provider = OrderedAITaskProvider(Client(), ["ai_task.first", "ai_task.second"])
    controller.store.set("feed", [{
        "id": f"post:{index}", "title": "English text", "original_text": "English text",
        "content_type": "Post", "source": "original",
    } for index in range(150)])
    assert controller.feed.ai_progress["pending"] >= 200
    cache = {}
    translate_content(controller.provider, "English text", cache)
    translate_content(controller.provider, "English text", cache)
    assert calls == ["ai_task.first"]
    assert not controller.feed.ai_progress["parallel"]
    controller.store.set("feed_translations", cache)
    controller.store.save()
    controller.close()
    restored = build_controller(tmp_path, FakeHA())
    assert restored.store.get("feed_translations") == cache
    restored.close()
