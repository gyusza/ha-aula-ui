"""Event-shape helpers extracted unchanged from the pinned MIT EasyIQ client."""

from __future__ import annotations

import datetime
import html as html_lib
import re
from typing import Any

_START_DATETIME_KEYS = (
    "start",
    "startDateTime",
    "startDatetime",
    "start_time",
    "startTime",
    "startTimeUtc",
    "from",
    "fromDateTime",
    "fromDatetime",
    "begin",
    "beginDateTime",
    "beginDatetime",
    "dateStart",
)
_START_DATE_KEYS = (
    "startDate",
    "start_date",
    "date",
    "eventDate",
    "day",
    "dato",
)
_START_TIME_KEYS = (
    "startTime",
    "start_time",
    "fromTime",
    "beginTime",
    "time",
    "tid",
)
_END_DATETIME_KEYS = (
    "end",
    "endDateTime",
    "endDatetime",
    "end_time",
    "endTime",
    "endTimeUtc",
    "to",
    "toDateTime",
    "toDatetime",
    "finish",
    "finishDateTime",
    "dateEnd",
)
_END_DATE_KEYS = (
    "endDate",
    "end_date",
    "date",
    "eventDate",
    "day",
    "dato",
)
_END_TIME_KEYS = (
    "endTime",
    "end_time",
    "toTime",
    "finishTime",
    "slutTid",
)
_COURSE_KEYS = (
    "coursesDisplay",
    "courseDisplay",
    "subjectsDisplay",
    "subjectDisplay",
    "courses",
    "course",
    "courseName",
    "courseTitle",
    "courseText",
    "subject",
    "subjectName",
    "subjectTitle",
    "subjectText",
    "schoolSubject",
    "schoolSubjectName",
    "title",
    "eventTitle",
    "calendarTitle",
    "calendarText",
    "chapterTitle",
    "icon",
    "iconTitle",
    "iconName",
    "entryTitle",
    "displayName",
    "heading",
    "headline",
    "name",
    "text",
    "fag",
    "fagNavn",
    "titel",
    "tekst",
)
_ACTIVITY_KEYS = (
    "activitiesDisplay",
    "activityDisplay",
    "activities",
    "activity",
    "activityName",
    "activityTitle",
    "activityText",
    "lesson",
    "lessonName",
    "lessonTitle",
    "class",
    "className",
    "team",
    "teamName",
    "hold",
    "holdNavn",
    "activityNames",
    "activityTitles",
)
_DESCRIPTION_KEYS = (
    "description",
    "details",
    "note",
    "notes",
    "content",
    "body",
    "comment",
)
_WEEKPLAN_EVENT_TYPES = (8, 9)
_HOMEWORK_EVENT_TYPES = (4,)


def _clean_text(value: Any) -> str:
    """Strip HTML and placeholder values from EasyIQ text fields."""
    text = html_lib.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in {"none", "null", "undefined", "nan"}:
        return ""
    return text


def _is_plain_image_path(text: str) -> bool:
    """Return true for bare EasyIQ icon/image paths that are not titles."""
    lowered = text.lower()
    if not (
        lowered.startswith("/images/")
        or lowered.startswith("images/")
        or lowered.startswith("http://")
        or lowered.startswith("https://")
    ):
        return False
    return bool(re.search(r"\.(?:png|svg|jpe?g|gif|webp)(?:$|\?)", lowered))


def _event_value(event: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-empty value for any key, case-insensitively."""
    lower_key_map = {str(key).lower(): key for key in event}
    for key in keys:
        actual_key = key if key in event else lower_key_map.get(key.lower())
        if actual_key is None:
            continue
        value = event.get(actual_key)
        if isinstance(value, str):
            if _clean_text(value):
                return value
            continue
        if value not in (None, ""):
            return value
    return None


def _field_text(value: Any) -> str:
    """Return readable text from common EasyIQ scalar/list/dict fields."""
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        lower_key_map = {str(key).lower(): key for key in value}
        for key in (
            "name",
            "displayName",
            "fullName",
            "shortName",
            "title",
            "text",
            "label",
            "value",
            "description",
            "subject",
            "course",
            "heading",
            "headline",
            "caption",
        ):
            actual_key = key if key in value else lower_key_map.get(key.lower())
            text = _field_text(value.get(actual_key)) if actual_key is not None else ""
            if text:
                return text
        return ""
    if isinstance(value, list):
        parts = [_field_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    text = _clean_text(value)
    if _is_plain_image_path(text):
        return ""
    return text


def _event_title_text(event: dict[str, Any]) -> str:
    """Return the best visible title for a calendar event."""
    for keys in (_COURSE_KEYS, _ACTIVITY_KEYS):
        text = _field_text(_event_value(event, keys)).strip()
        if text:
            return text
    return "School Event"


def _is_generic_calendar_title(value: Any) -> bool:
    """Return true when a title is blank, generic, or agenda/body text."""
    text = _clean_text(value).lower()
    if not text:
        return True
    if text == "school event":
        return True
    if text.startswith("dagsorden"):
        return True
    return False


def _parse_easyiq_datetime(value: Any) -> datetime.datetime | None:
    """Parse EasyIQ calendar date strings in known legacy and ISO formats."""
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(normalized)
    except ValueError:
        pass

    for date_format in (
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y-%m-%d",
    ):
        try:
            return datetime.datetime.strptime(text, date_format)
        except ValueError:
            continue

    return None


def _event_date(value: Any) -> datetime.date | None:
    """Return the calendar date for an EasyIQ event timestamp."""
    parsed = _parse_easyiq_datetime(value)
    if parsed is not None:
        return parsed.date()
    return None


def _event_start_date(event: dict[str, Any]) -> datetime.date | None:
    """Return the calendar date for an event from known EasyIQ fields."""
    parsed = _event_start_datetime(event)
    if parsed is not None:
        return parsed.date()
    return _event_date(event.get("start"))


def _event_time_text(value: Any) -> str:
    """Return a readable time for an EasyIQ event timestamp."""
    parsed = _parse_easyiq_datetime(value)
    if parsed is not None:
        return parsed.strftime("%H:%M")
    text = str(value or "")
    if " " in text:
        return text.split(" ", 1)[1]
    if "T" in text:
        return text.split("T", 1)[1].replace("Z", "")[:5]
    return text


def _event_datetime(
    event: dict[str, Any],
    datetime_keys: tuple[str, ...],
    date_keys: tuple[str, ...],
    time_keys: tuple[str, ...],
) -> datetime.datetime | None:
    """Parse an event timestamp from combined or split EasyIQ fields."""
    for key in datetime_keys:
        value = _event_value(event, (key,))
        parsed = _parse_easyiq_datetime(value)
        if parsed is not None:
            return parsed

    date_value = _event_value(event, date_keys)
    time_value = _event_value(event, time_keys)
    if date_value and time_value:
        for separator in (" ", "T"):
            parsed = _parse_easyiq_datetime(f"{date_value}{separator}{time_value}")
            if parsed is not None:
                return parsed

    return _parse_easyiq_datetime(date_value)


def _event_start_datetime(event: dict[str, Any]) -> datetime.datetime | None:
    """Return the event start timestamp from known EasyIQ fields."""
    return _event_datetime(
        event,
        _START_DATETIME_KEYS,
        _START_DATE_KEYS,
        _START_TIME_KEYS,
    )


def _event_end_datetime(event: dict[str, Any]) -> datetime.datetime | None:
    """Return the event end timestamp from known EasyIQ fields."""
    return _event_datetime(
        event,
        _END_DATETIME_KEYS,
        _END_DATE_KEYS,
        _END_TIME_KEYS,
    )


def _event_item_type(event: dict[str, Any]) -> int | None:
    """Return the EasyIQ item type as an integer when present."""
    for key in (
        "itemType",
        "itemTypeId",
        "type",
        "typeId",
        "eventType",
        "eventTypeId",
        "calendarItemType",
        "calendarItemTypeId",
        "activityType",
        "activityTypeId",
    ):
        value = _event_value(event, (key,))
        if isinstance(value, dict):
            value = value.get("id", value.get("value"))

        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                continue

    return None


def _events_of_type(events: list[dict[str, Any]], item_type: int) -> list[dict[str, Any]]:
    """Return EasyIQ events matching a normalized item type."""
    return [event for event in events if _event_item_type(event) == item_type]


def _events_of_types(
    events: list[dict[str, Any]],
    item_types: tuple[int, ...],
) -> list[dict[str, Any]]:
    """Return EasyIQ events matching any normalized item type."""
    return [event for event in events if _event_item_type(event) in item_types]


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    """Return a compact item type histogram for diagnostics."""
    counts: dict[str, int] = {}
    for event in events:
        item_type = _event_item_type(event)
        key = "missing" if item_type is None else str(item_type)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _preview_value(value: Any) -> Any:
    """Return a small JSON-friendly preview value for diagnostics."""
    if isinstance(value, dict):
        preview: dict[str, Any] = {}
        for key, nested_value in list(value.items())[:8]:
            preview[str(key)] = _preview_value(nested_value)
        return preview
    if isinstance(value, list):
        return [_preview_value(item) for item in value[:3]]
    text = str(value)
    return text[:160] if len(text) > 160 else text


def _event_preview(event: dict[str, Any]) -> dict[str, Any]:
    """Return a compact event preview for diagnostics."""
    return {
        str(key): _preview_value(value)
        for key, value in list(event.items())[:60]
    }


def _normalize_calendar_event(event: dict[str, Any]) -> dict[str, Any]:
    """Populate the legacy fields this integration expects from newer shapes."""
    normalized = dict(event)
    start = _event_start_datetime(event)
    end = _event_end_datetime(event)

    if start is not None and not normalized.get("start"):
        normalized["start"] = start.isoformat()
    if end is not None and not normalized.get("end"):
        normalized["end"] = end.isoformat()
    elif start is not None and not normalized.get("end"):
        normalized["end"] = (start + datetime.timedelta(hours=1)).isoformat()

    courses = _event_title_text(event)
    if courses and _is_generic_calendar_title(normalized.get("courses")):
        normalized["courses"] = courses

    activities = _field_text(_event_value(event, _ACTIVITY_KEYS))
    if activities and not _clean_text(normalized.get("activities")):
        normalized["activities"] = activities

    description = _field_text(_event_value(event, _DESCRIPTION_KEYS))
    if description and not _clean_text(normalized.get("description")):
        normalized["description"] = description

    if _event_item_type(normalized) is None and start is not None:
        normalized["itemType"] = 9
        normalized["_easyiq_item_type_inferred"] = "weekplan"

    return normalized


def _payload_summary(payload: Any) -> dict[str, Any]:
    """Return a small, serializable description of an API payload."""
    if isinstance(payload, list):
        return {"type": "list", "length": len(payload)}
    if isinstance(payload, dict):
        return {
            "type": "dict",
            "keys": [str(key) for key in list(payload.keys())[:12]],
        }
    return {"type": type(payload).__name__}


def _extract_calendar_event_list(
    payload: Any,
    *,
    normalize: bool = True,
) -> list[dict[str, Any]]:
    """Normalize known EasyIQ calendar response wrappers to a list of events."""
    events: list[dict[str, Any]] = []
    if isinstance(payload, list):
        events = [event for event in payload if isinstance(event, dict)]
    elif isinstance(payload, dict):
        for key in (
            "events",
            "calendarEvents",
            "items",
            "data",
            "value",
            "result",
            "results",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                events = [event for event in value if isinstance(event, dict)]
                break
            if isinstance(value, dict):
                nested_events = _extract_calendar_event_list(
                    value,
                    normalize=normalize,
                )
                if nested_events:
                    return nested_events

    if normalize:
        return [_normalize_calendar_event(event) for event in events]
    return events
