"""Normalisation of Aula / EasyIQ message sensors.

The upstream integrations expose messages in two different shapes:

* a rich ``all`` attribute holding a newest-first list of message dicts
  (documented in REQUIREMENTS.md, used by newer integration versions), and
* the flat ``subject`` / ``text`` / ``sender`` attributes that the installed
  ``aula`` and ``aula_easyiq`` integrations actually publish today.

Both are supported; everything downstream works on the normalised shape.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any
from .media import attachments

_LOGGER = logging.getLogger(__name__)


def date_key(item: dict[str, Any]) -> float:
    try:
        parsed = datetime.fromisoformat(str(item.get("date") or "").replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except ValueError:
        return float("-inf")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

# Common Danish function words; enough to flag "not English" reliably for
# school communication without pulling in a language-detection dependency.
# Words that are unambiguously Danish (never ordinary English words), so a
# couple of hits is a safe "not English" signal.
_DANISH_MARKERS = {
    "af", "alle", "bliver", "børn", "dag", "der", "det", "eller", "elever",
    "er", "fra", "forældre", "har", "hilsen", "hvis", "ikke", "jeg", "kan",
    "klasse", "lærer", "med", "mandag", "og", "om", "på", "skal", "skole",
    "som", "tak", "til", "uge", "ugen", "ved", "vi", "vil", "være",
    "cykel", "cykeltur", "ugebrev", "forældremøde", "lektier",
}
_NORDIC_CHARS = set("æøåÆØÅ")


def strip_html(raw: Any) -> str:
    """Turn an HTML message body into readable plain text."""
    if raw is None:
        return ""
    text = str(raw)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def message_id(subject: str, sender: str, text: str, explicit: Any = None) -> str:
    """Return a stable identifier for a message."""
    if explicit not in (None, "", "None"):
        return str(explicit)
    digest = hashlib.sha1(
        "\u241f".join((subject or "", sender or "", text or "")).encode("utf-8")
    ).hexdigest()
    return f"msg_{digest[:16]}"


def looks_non_english(text: str) -> bool:
    """Heuristically decide whether ``text`` is not English."""
    if not text:
        return False
    if _NORDIC_CHARS.intersection(text):
        return True
    words = re.findall(r"[^\W\d_]+", text.lower(), flags=re.UNICODE)
    if not words:
        return False
    hits = sum(1 for word in words if word in _DANISH_MARKERS)
    # Two or more Danish function words, or a high density in a short message.
    return hits >= 2 or (hits >= 1 and len(words) <= 6)


def normalise_message(raw: dict[str, Any], fallback_date: str | None = None) -> dict[str, Any] | None:
    """Convert one raw message dict into the add-on's canonical shape."""
    if not isinstance(raw, dict):
        return None

    subject = str(raw.get("subject") or raw.get("title") or "").strip()
    sender = str(raw.get("sender") or raw.get("from") or "").strip()
    body_raw = raw.get("text") if raw.get("text") is not None else raw.get("original_text")
    if body_raw is None:
        body_raw = raw.get("body") or raw.get("message") or ""
    text = strip_html(body_raw)
    media = attachments(raw, body_raw)

    if not subject and not text and not media:
        return None

    date = raw.get("date") or raw.get("timestamp") or raw.get("sendDateTime")
    date = str(date).strip() if date else (fallback_date or "")

    return {
        "id": message_id(subject, sender, text, raw.get("id")),
        "title": subject,
        "original_text": text,
        "sender": sender or "Unknown",
        "date": date,
        **({"attachments": media} if media else {}),
    }


def extract_messages(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return a newest-first list of normalised messages from a sensor state."""
    if not state or not isinstance(state.get("attributes"), dict):
        return []

    attributes = state["attributes"]
    fallback_date = state.get("last_changed") or datetime.now(timezone.utc).isoformat()

    raw_all = attributes.get("all")
    if isinstance(raw_all, list) and raw_all:
        messages = [normalise_message(item, fallback_date) for item in raw_all]
        return [message for message in messages if message]

    # Flat single-message shape used by the installed integrations.
    single = normalise_message(
        {
            "id": attributes.get("id"),
            "subject": attributes.get("subject"),
            "text": attributes.get("text"),
            "sender": attributes.get("sender"),
            "date": attributes.get("date"),
        },
        fallback_date,
    )
    return [single] if single else []
