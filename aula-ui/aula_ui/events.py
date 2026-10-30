"""Calendar / weekplan event collection and classification."""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, Iterable

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = ("meeting", "party", "talk", "exam", "homework", "other")

# Ordered: the first matching bucket wins, so the more specific Danish /
# English keywords are checked before the broad "meeting" ones.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("exam", ("eksamen", "terminsprøve", "afgangsprøve", "prøve", "exam", "test", "quiz")),
    (
        "homework",
        ("lektie", "lektier", "aflevering", "afleveres", "opgave", "homework", "assignment"),
    ),
    (
        "party",
        (
            "fest", "fødselsdag", "julefrokost", "fastelavn", "disco", "party",
            "birthday", "celebration",
        ),
    ),
    (
        "talk",
        ("samtale", "foredrag", "oplæg", "konsultation", "talk", "lecture", "consultation"),
    ),
    (
        "meeting",
        (
            "forældremøde", "skole-hjem", "skolehjem", "møde", "meeting",
            "parent", "conference",
        ),
    ),
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TRANSLIT = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "ä": "ae", "ö": "oe", "ü": "ue"})


def slugify(value: str) -> str:
    """Slugify like Home Assistant does, so it matches ``input_boolean`` ids."""
    text = (value or "").strip().lower().translate(_TRANSLIT)
    text = _SLUG_RE.sub("_", text)
    return text.strip("_")


def classify_event_type(title: str, description: str = "") -> str:
    """Map an event title/description onto one of ``EVENT_TYPES``."""
    haystack = f"{title or ''} {description or ''}".lower()
    if not haystack.strip():
        return "other"
    for event_type, keywords in _KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return event_type
    return "other"


def _split_datetime(raw: Any) -> tuple[str, str | None]:
    """Return ``(YYYY-MM-DD, HH:MM | None)`` for a calendar date/datetime value."""
    if raw is None:
        return "", None
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date") or ""
    if isinstance(raw, datetime):
        return raw.date().isoformat(), raw.strftime("%H:%M")
    if isinstance(raw, date_cls):
        return raw.isoformat(), None

    text = str(raw).strip()
    if not text:
        return "", None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # All-day values and odd formats: keep the leading date part.
        return text[:10], None
    if len(text) <= 10:
        return parsed.date().isoformat(), None
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def make_event_id(title: str, event_date: str) -> str:
    """Build a slug-safe, stable event id usable in ``input_boolean`` names."""
    date_part = slugify(event_date) or "nodate"
    title_part = slugify(title) or "event"
    return f"{date_part}_{title_part}"[:120].strip("_")


def _timestamp(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date")
    if isinstance(raw, (datetime, date_cls)):
        return raw.isoformat()
    return str(raw or "")


def normalise_calendar_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one Home Assistant calendar API event."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("summary") or raw.get("title") or "").strip()
    description = str(raw.get("description") or "").strip()
    event_date, event_time = _split_datetime(raw.get("start"))
    if not title and not event_date:
        return None
    return {
        "id": make_event_id(title, event_date),
        "title": title or "Untitled",
        "date": event_date,
        "time": event_time,
        "type": classify_event_type(title, description),
        "description": description,
        "origin": "Calendar",
        "timestamp": _timestamp(raw.get("start")),
        "end": _timestamp(raw.get("end")),
        "location": str(raw.get("location") or ""),
        "sender": str(raw.get("sender") or raw.get("organizer") or "Aula calendar"),
    }


def normalise_weekplan_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one EasyIQ weekplan event (``courses`` / ``start`` / ``activities``)."""
    if not isinstance(raw, dict):
        return None
    title = str(
        raw.get("courses") or raw.get("activities") or raw.get("summary") or raw.get("title") or ""
    ).strip()
    description = str(raw.get("activities") or raw.get("description") or "").strip()
    event_date, event_time = _split_datetime(raw.get("start") or raw.get("date"))
    if not title and not event_date:
        return None
    return {
        "id": make_event_id(title, event_date),
        "title": title or "Untitled",
        "date": event_date,
        "time": event_time,
        "type": classify_event_type(title, description),
        "description": description,
        "origin": "Weekplan",
        "timestamp": _timestamp(raw.get("start") or raw.get("date")),
        "end": _timestamp(raw.get("end")),
        "location": str(raw.get("location") or ""),
        "sender": str(raw.get("sender") or "EasyIQ"),
    }


def _sort_key(event: dict[str, Any]) -> tuple[str, str, str]:
    return (event.get("date") or "9999-99-99", event.get("time") or "99:99", event.get("title") or "")


def dedupe_and_sort(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate ids and return events in chronological order."""
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        if event and event.get("id") and event["id"] not in unique:
            unique[event["id"]] = event
    return sorted(unique.values(), key=_sort_key)


def collect_events(
    client: Any,
    calendars: Iterable[str],
    weekplan_state: dict[str, Any] | None,
    lookahead_days: int = 21,
) -> list[dict[str, Any]]:
    """Gather events from the calendar entities, falling back to the weekplan sensor."""
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=lookahead_days)

    collected: list[dict[str, Any]] = []
    for entity_id in calendars:
        for raw in client.get_calendar_events(entity_id, start=start, end=end):
            normalised = normalise_calendar_event(raw)
            if normalised:
                collected.append(normalised)

    if not collected and weekplan_state:
        attributes = weekplan_state.get("attributes") or {}
        summary = attributes.get("weekplan_summary") or {}
        raw_events = summary.get("events") if isinstance(summary, dict) else None
        if not isinstance(raw_events, list):
            raw_events = attributes.get("events") if isinstance(attributes.get("events"), list) else []
        for raw in raw_events:
            normalised = normalise_weekplan_event(raw)
            if normalised:
                collected.append(normalised)

    return dedupe_and_sort(collected)
