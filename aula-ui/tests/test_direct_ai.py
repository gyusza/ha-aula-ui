import json
from types import SimpleNamespace

import pytest
import requests

from aula_ui import direct_ai
from aula_ui.llm import AI_TASK_STRUCTURE, LLMError, translate_text

CONFIG = dict(anthropic_api_key="ak", google_api_key="gk", cloudflare_api_token="ct",
              cloudflare_account_id="0123456789abcdef0123456789abcdef",
              anthropic_model="", google_model="", cloudflare_model="")
OUTPUT = json.dumps({"overview": "Bring **boots**.", "is_translated": True})


class Reply:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class Calls(list):
    replies = None


@pytest.fixture
def calls(monkeypatch):
    log = Calls()
    replies = []

    def post(url, headers=None, json=None, timeout=None):
        log.append((url, headers, json))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(direct_ai.requests, "post", post)
    log.replies = replies
    return log


def provider(source, **extra):
    return direct_ai.build_direct(SimpleNamespace(**{**CONFIG, **extra}), source)


def test_anthropic_request_and_summary(calls):
    calls.replies.append(Reply({"content": [{"type": "text", "text": OUTPUT}], "stop_reason": "end_turn"}))
    text, translated = provider("anthropic").summarise("Summarise")
    assert (text, translated) == ("Bring **boots**.", True)
    url, headers, body = calls[0]
    assert url == "https://api.anthropic.com/v1/messages" and headers["x-api-key"] == "ak"
    assert body["model"] == "claude-haiku-4-5" and "overview" in body["messages"][0]["content"]


def test_google_request_uses_header_key_not_url(calls):
    calls.replies.append(Reply({"candidates": [{"content": {"parts": [{"text": OUTPUT}]}}]}))
    assert provider("google").summarise("x")[0] == "Bring **boots**."
    url, headers, body = calls[0]
    assert url.endswith("/models/gemini-2.5-flash:generateContent") and "gk" not in url
    assert headers == {"x-goog-api-key": "gk"} and body["generationConfig"]["responseMimeType"] == "application/json"


def test_cloudflare_workers_ai_shapes(calls):
    calls.replies.append(Reply({"success": True, "result": {"response": OUTPUT}}))
    calls.replies.append(Reply({"result": {"response": {"overview": "Hi", "is_translated": False}}}))
    p = provider("cloudflare")
    assert p.summarise("x") == ("Bring **boots**.", True)
    assert p.summarise("x") == ("Hi", False)
    assert calls[0][0] == ("https://api.cloudflare.com/client/v4/accounts/0123456789abcdef0123456789abcdef"
                           "/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    assert calls[0][1] == {"Authorization": "Bearer ct"} and calls[0][2]["max_tokens"] >= 4096


def test_events_array_is_serialised_for_analyse(calls):
    event = {"title": "Trip", "start": "2026-11-01", "end": "", "location": "", "evidence": "1. nov",
             "all_day": True}
    calls.replies.append(Reply({"content": [{"type": "text", "text": json.dumps(
        {"overview": "S", "is_translated": False, "events_json": [event]})}]}))
    summary, translated, events = provider("anthropic").analyse(
        {"date": "2026-10-20", "thread_messages": [{"title": "t", "original_text": "x", "date": "2026-10-20"}]},
        "Noah", "Europe/Copenhagen")
    assert events == [event] and summary == "S"


@pytest.mark.parametrize("reply, message", [
    (Reply({"error": {"message": "bad key"}}, 401), "HTTP 401: bad key"),
    (Reply({"content": [{"type": "text", "text": "{}"}]}), "invalid required"),
    (Reply({"content": [{"type": "text", "text": OUTPUT}], "stop_reason": "max_tokens"}), "cut off"),
    (requests.ConnectionError("boom https://x?key=secret"), "ConnectionError"),
])
def test_failures_become_llm_errors_without_secrets(calls, reply, message):
    calls.replies.append(reply)
    with pytest.raises(LLMError) as error:
        provider("anthropic").summarise("x")
    assert message in str(error.value) and "secret" not in str(error.value) and "ak" != str(error.value)


def test_credentials_validation():
    assert direct_ai.credentials(SimpleNamespace(**{**CONFIG, "cloudflare_account_id": "../x"}), "cloudflare") is None
    assert direct_ai.credentials(SimpleNamespace(**{**CONFIG, "google_model": "a/../b"}), "google") is None
    assert direct_ai.credentials(SimpleNamespace(**{**CONFIG, "google_api_key": ""}), "google") is None
    assert [s["configured"] for s in direct_ai.configured_sources(SimpleNamespace(**CONFIG))] == [True] * 3
