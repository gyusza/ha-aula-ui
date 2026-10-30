"""Optional AI sources that use the user's own key instead of a Home Assistant AI Task.

Anthropic and Google use the user's API key; Cloudflare uses Workers AI through its
REST API (the same service the newsletter app reaches through a Worker binding).
Credentials come from add-on options only and never leave this module.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import requests

from .diagnostics import observed
from .llm import AI_TASK_TIMEOUT, HomeAssistantAITaskProvider, LLMError, _parse_structured

SOURCES = {
    "anthropic": "Anthropic (my API key)",
    "google": "Google Gemini (my API key)",
    "cloudflare": "Cloudflare Workers AI",
}
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "google": "gemini-2.5-flash",
    "cloudflare": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
}
_MODEL_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9_.\-/]{0,119}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_MAX_TOKENS = 8192
_CLOUDFLARE_MAX_TOKENS = 4096


def credentials(config: Any, source: str) -> dict[str, str] | None:
    """Return validated settings for ``source`` or ``None`` when it is not configured."""
    if source == "anthropic" and config.anthropic_api_key:
        key = config.anthropic_api_key
    elif source == "google" and config.google_api_key:
        key = config.google_api_key
    elif (source == "cloudflare" and config.cloudflare_api_token
          and _ACCOUNT_RE.fullmatch(config.cloudflare_account_id or "")):
        key = config.cloudflare_api_token
    else:
        return None
    model = getattr(config, f"{source}_model", "") or DEFAULT_MODELS[source]
    if not _MODEL_RE.fullmatch(model) or ".." in model:
        return None
    return {"key": key, "model": model, "account": config.cloudflare_account_id}


def configured_sources(config: Any) -> list[dict[str, Any]]:
    result = []
    for source, label in SOURCES.items():
        found = credentials(config, source)
        result.append({"id": source, "label": label, "configured": bool(found),
                       "model": found["model"] if found else (getattr(config, f"{source}_model", "")
                                                              or DEFAULT_MODELS[source])})
    return result


class DirectProvider(HomeAssistantAITaskProvider):
    """Structured generation over a plain chat-completion API."""

    name = ""

    def __init__(self, key: str, model: str, account: str = "") -> None:
        super().__init__(None, "", AI_TASK_TIMEOUT)
        self._key = key
        self.model = model
        self._account = account
        self.last_provider = self.name

    def _complete(self, prompt: str) -> str:
        raise NotImplementedError

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
        try:
            response = requests.post(url, headers=headers, json=body, timeout=self._timeout)
        except requests.RequestException as err:
            raise LLMError(f"{self.name} request failed ({type(err).__name__})") from None
        if response.status_code >= 400:
            detail = ""
            try:
                error = response.json().get("error")
                detail = error.get("message") if isinstance(error, dict) else str(error or "")
            except (ValueError, AttributeError):
                pass
            raise LLMError(f"{self.name} returned HTTP {response.status_code}: {detail[:200]}".rstrip(": "))
        try:
            return response.json()
        except ValueError:
            raise LLMError(f"{self.name} returned invalid JSON") from None

    @observed("ai.direct.generate")
    def _generate(self, prompt: str, structure: dict[str, Any]) -> dict[str, Any]:
        fields = "\n".join(
            f'- "{key}" ({"boolean" if "boolean" in field.get("selector", {}) else "string"}): '
            f'{field.get("description", "")}' for key, field in structure.items())
        try:
            raw = self._complete(
                f"{prompt}\n\nReturn ONLY one JSON object, with no Markdown fences or commentary, "
                f"with exactly these keys:\n{fields}\nString values that contain a JSON array must "
                "hold that array serialised as text.")
        except (AttributeError, TypeError, KeyError, IndexError):
            # An unexpected response shape is a provider failure; let failover continue.
            raise LLMError(f"{self.name} returned an unexpected response") from None
        data = _parse_structured(raw)
        for key, field in structure.items():
            selector = field.get("selector", {})
            value = data.get(key)
            if "text" in selector and isinstance(value, (list, dict)):
                value = data[key] = json.dumps(value, ensure_ascii=False)
            if field.get("required"):
                if "text" in selector and (not isinstance(value, str) or not value.strip()):
                    raise LLMError("AI returned an invalid required text field")
                if "boolean" in selector and not isinstance(value, (bool, str)):
                    raise LLMError("AI returned an invalid required boolean")
        return data


class AnthropicProvider(DirectProvider):
    name = "anthropic"

    def _complete(self, prompt: str) -> str:
        data = self._post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": _MAX_TOKENS,
             "messages": [{"role": "user", "content": prompt}]})
        if data.get("stop_reason") == "max_tokens":
            raise LLMError("anthropic response was cut off")
        text = "".join(block.get("text", "") for block in data.get("content", [])
                       if isinstance(block, dict) and block.get("type") == "text")
        if not text.strip():
            raise LLMError("anthropic returned no text")
        return text


class GoogleProvider(DirectProvider):
    name = "google"

    def _complete(self, prompt: str) -> str:
        data = self._post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model, safe='')}:generateContent",
            {"x-goog-api-key": self._key},
            {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
             "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": _MAX_TOKENS}})
        candidates = data.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise LLMError("google response was cut off")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if not text.strip():
            raise LLMError("google returned no text")
        return text


class CloudflareProvider(DirectProvider):
    name = "cloudflare"

    def _complete(self, prompt: str) -> str:
        data = self._post(
            f"https://api.cloudflare.com/client/v4/accounts/{self._account}/ai/run/"
            f"{quote(self.model, safe='@/')}",
            {"Authorization": f"Bearer {self._key}"},
            {"messages": [{"role": "user", "content": prompt}], "max_tokens": _CLOUDFLARE_MAX_TOKENS})
        if data.get("success") is False:
            raise LLMError("cloudflare reported an error")
        result = data.get("result") if isinstance(data.get("result"), dict) else data
        text = result.get("response")
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        if not isinstance(text, str):
            choices = result.get("choices")
            message = choices[0].get("message", {}) if isinstance(choices, list) and choices else {}
            text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise LLMError("cloudflare returned no text")
        return text


PROVIDERS = {"anthropic": AnthropicProvider, "google": GoogleProvider, "cloudflare": CloudflareProvider}


def build_direct(config: Any, source: str) -> DirectProvider | None:
    found = credentials(config, source)
    if not found or source not in PROVIDERS:
        return None
    return PROVIDERS[source](found["key"], found["model"], found["account"])
