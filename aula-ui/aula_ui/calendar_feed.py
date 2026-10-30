"""Grounded AI event validation and RFC 5545 calendar publication."""

import hashlib
import logging
import os
import re
import tempfile
import threading
from .diagnostics import observed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from difflib import SequenceMatcher
import unicodedata
from zoneinfo import ZoneInfo

from .config import DEFAULT_TIMEZONE

_LOGGER = logging.getLogger(__name__)

THREAD_RECOMMENDATION_LIMIT = 3
_MONTHS = (
    "januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december|"
    "january|february|march|may|june|july|october"
)
_RELATIVE_DAY = (
    "i dag|i morgen|i overmorgen|today|tomorrow|mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag|"
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
)
_EXPLICIT_DATE = re.compile(rf"\d|\b(?:{_MONTHS})\b", re.IGNORECASE)
_ANY_DATE = re.compile(rf"\d|\b(?:{_MONTHS}|{_RELATIVE_DAY})\b", re.IGNORECASE)
_PAST_REPORT = re.compile(
    r"\b(?:i går|igår|yesterday|last (?:week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(?:vi|børnene|de|we|the children|they)\s+(?:har været|har haft|havde|var|tog|besøgte|lavede|went|had|visited|enjoyed)"
    r"|(?:var|havde|tog|besøgte)\s+(?:vi|børnene|de)"
    r"|we have been|tak for (?:i dag|sidst|en dejlig)|thanks for (?:today|a lovely))\b",
    re.IGNORECASE,
)
_FUTURE_PLAN = re.compile(
    r"\b(?:skal|vil|kommer|inviterer|invitation|tilmeld|husk|will|tomorrow|i morgen|"
    r"upcoming|please|invited|join us|we are going|vi tager)\b", re.IGNORECASE,
)


def calendar_today(timezone_name=DEFAULT_TIMEZONE):
    return datetime.now(ZoneInfo(timezone_name)).date()


def retrospective_reason(event, today=None, timezone_name=DEFAULT_TIMEZONE):
    evidence = str(event.get("evidence") or "")
    # Evaluate the quoted sentence, not unrelated past/future sentences elsewhere.
    if _PAST_REPORT.search(evidence) and not _FUTURE_PLAN.search(evidence):
        return "source describes an activity that already happened"
    try:
        start = date.fromisoformat(_day(event.get("start")))
    except ValueError:
        return ""
    if start < (today or calendar_today(timezone_name)):
        return "event date is in the past"
    return ""


def _event_words(value):
    text = unicodedata.normalize("NFKD", str(value).casefold())
    words = re.findall(r"[a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c)))
    aliases = {
        "tur": "trip", "udflugt": "trip", "excursion": "trip", "outing": "trip", "ride": "trip",
        "cykel": "bike", "cykeltur": "bike", "bicycle": "bike", "cycling": "bike",
        "fodselsdag": "birthday", "fodselsdagsfest": "birthday",
        "foraeldremode": "parents", "meeting": "meeting", "mode": "meeting",
        "julefest": "christmas", "jul": "christmas", "sommerfest": "summer",
    }
    stop = {"the", "a", "an", "and", "for", "of", "to", "with", "pa", "til", "og", "i", "en", "et",
            "day", "dag", "event", "arrangement", "reminder", "pamindelse"}
    return {aliases.get(word, word) for word in words if word not in stop}


def similar_calendar_event(candidate, existing):
    day = _day(candidate.get("start"))
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    other_day = _day(existing.get("start"))
    same_day = day == other_day
    if existing.get("all_day") and existing.get("end"):
        same_day = other_day <= day < _day(existing["end"])
    if not same_day:
        return False
    children, other_children = set(candidate.get("child_slugs") or []), set(existing.get("child_slugs") or [])
    if children and other_children and not children.intersection(other_children):
        return False
    left, right = _event_words(candidate.get("title")), _event_words(existing.get("title"))
    if not left or not right:
        return False
    overlap = left.intersection(right)
    return (left == right or (len(overlap) >= 2 and len(overlap) / min(len(left), len(right)) >= .6)
            or (bool(overlap - {"trip", "meeting", "party"}) and min(len(left), len(right)) == 1)
            or SequenceMatcher(None, " ".join(sorted(left)), " ".join(sorted(right))).ratio() >= .82)


def _day(value):
    return str(value or "")[:10]


def weak_evidence_reason(event, source_id, validated=False):
    """Explain why a pending AI suggestion lacks enough evidence, or return ''.

    Chat threads are conversational, so their suggestions need a longer quote that
    itself states the date; other sources only need a dated, non-trivial quote.
    """
    evidence = " ".join(str(event.get("evidence") or "").split())
    words = len(evidence.split())
    if source_id.startswith("thread:"):
        if words < (3 if validated else 6):
            return f"thread quote too short ({words} words): {evidence!r}"
        if not _EXPLICIT_DATE.search(evidence) and not re.search(
                r"\b(?:i dag|i morgen|i overmorgen|today|tomorrow)\b", evidence, re.IGNORECASE):
            return f"thread quote has no explicit date: {evidence[:80]!r}"
        return ""
    if words < 3:
        return f"quote too short ({words} words): {evidence!r}"
    if not _ANY_DATE.search(evidence):
        return f"quote has no date: {evidence[:80]!r}"
    return ""


def gate_suggestions(item, confirmed, suggestions, timezone_name=DEFAULT_TIMEZONE):
    """Keep only well-evidenced, non-duplicate suggestions for one analysed source.

    ``confirmed`` are validated events and ``suggestions`` are (raw, issue) pairs.
    Returns (kept_confirmed, kept_suggestions, dropped) where dropped lists reasons.
    """
    source_id = item.get("id", "")
    thread = source_id.startswith("thread:")
    dropped, kept_confirmed, days = [], [], set()
    for event in confirmed:
        reason = retrospective_reason(event, timezone_name=timezone_name)
        reason = reason or (weak_evidence_reason(event, source_id, validated=True) if thread else "")
        if reason:
            dropped.append(f"{event.get('title', '')}: {reason}")
            continue
        kept_confirmed.append(event)
        days.add(_day(event.get("start")))
    kept = []
    # Prefer the best-evidenced suggestion when several describe the same day.
    ordered = sorted(suggestions, key=lambda pair: -len(str((pair[0] or {}).get("evidence") or "")))
    for raw, issue in ordered:
        raw = raw if isinstance(raw, dict) else {}
        title = str(raw.get("title") or "")
        reason = retrospective_reason(raw, timezone_name=timezone_name) or weak_evidence_reason(raw, source_id)
        if not reason and item.get("original_text") is not None:
            try:
                source_quote(raw.get("evidence"), item["original_text"])
            except ValueError:
                reason = "quote not found in the source text"
        day = _day(raw.get("start"))
        if not reason and day and day in days:
            reason = f"another event from this source is already on {day}"
        if not reason and thread and len(kept) >= THREAD_RECOMMENDATION_LIMIT:
            reason = f"thread already has {THREAD_RECOMMENDATION_LIMIT} suggestions"
        if reason:
            dropped.append(f"{title}: {reason}")
            continue
        kept.append((raw, issue))
        if day:
            days.add(day)
    return kept_confirmed, kept, dropped


def provenance(event, item=None):
    item = item or {}
    source_id = item.get("id", event.get("source_id", ""))
    origin = "parent" if source_id.startswith("sick:") else "aula" if source_id.startswith("event:") else "ai"
    return {
        "origin": origin,
        "source_id": source_id,
        "source_title": event.get("source_title", item.get("title", "")),
        "source_date": event.get("source_date", item.get("date", "")),
        "source_author": event.get("source_author") or item.get("sender", ""),
        "source_excerpt": event.get("source_excerpt") or item.get("original_text", ""),
        "date_rationale": event.get("date_rationale", ""),
        "review_required": origin == "ai" and not event.get("manually_confirmed"),
    }


def source_quote(evidence, text):
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("Event evidence must quote the original content")
    match = re.search(r"\s+".join(re.escape(word) for word in evidence.split()), text, re.IGNORECASE)
    if not match:
        raise ValueError("Event evidence must quote the original content")
    return match.group()


def validate_events(raw, item, timezone_name=DEFAULT_TIMEZONE):
    if not isinstance(raw, list) or len(raw) > 40:
        raise ValueError("Extracted events must be a list of at most 40 events")
    result = []
    for event in raw:
        if not isinstance(event, dict):
            raise ValueError("Every event must be an object")
        title, evidence = event.get("title"), event.get("evidence")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Event title is missing")
        evidence = source_quote(evidence, item["original_text"])
        if not isinstance(event.get("date_rationale", ""), str):
            raise ValueError("Event date rationale must be text")
        start, end = event.get("start"), event.get("end") or ""
        all_day = event.get("all_day")
        if not isinstance(all_day, bool) or not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("Event requires start, optional end and a boolean all_day")
        if all_day:
            first = date.fromisoformat(start)
            last = date.fromisoformat(end) if end else first + timedelta(days=1)
            if last <= first:
                raise ValueError("All-day end must be exclusive and later than start")
            start, end = first.isoformat(), last.isoformat()
        else:
            first = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if "T" not in start:
                raise ValueError("Timed event needs an ISO datetime")
            first = first.replace(tzinfo=first.tzinfo or ZoneInfo(timezone_name))
            start = first.astimezone(timezone.utc).isoformat()
            if end:
                if "T" not in end:
                    raise ValueError("Timed event end needs an ISO datetime")
                last = datetime.fromisoformat(end.replace("Z", "+00:00"))
                last = last.replace(tzinfo=last.tzinfo or ZoneInfo(timezone_name))
                if last <= first:
                    raise ValueError("Event end must be later than start")
                end = last.astimezone(timezone.utc).isoformat()
        location = event.get("location") or ""
        if not isinstance(location, str):
            raise ValueError("Event location must be text")
        result.append({
            "title": title.strip(), "start": start, "end": end, "all_day": all_day,
            "location": location, "evidence": evidence.strip(),
            "source_id": item["id"], "source_title": item["title"],
            "source_date": item["date"], "child_slugs": item["child_slugs"],
            **provenance({"date_rationale": event.get("date_rationale", "")}, item),
            **({
                "calendar_kind": item.get("calendar_kind", ""),
                "source_content_type": item.get("content_type", "Calendar"),
            } if item["id"].startswith("event:") else {}),
        })
    return result


def escape(value):
    return str(value).replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _without_dtstamp(content):
    return re.sub(rb"\r\nDTSTAMP:[0-9TZ]+", b"", content)


def folded(line):
    # RFC 5545 folding is 75 OCTETS, not 75 Unicode codepoints.
    chunks, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            chunks.append(current)
            current = " "
        current += char
    chunks.append(current)
    return "\r\n".join(chunks)


def event_uid(event):
    if event.get("calendar_uid"):
        return event["calendar_uid"]
    if provenance(event)["origin"] == "parent":
        return hashlib.sha256(event["source_id"].encode()).hexdigest()[:32] + "@aula-feed"
    identity = "\n".join([event["title"].casefold(), event["start"], event["location"].casefold()])
    if provenance(event)["origin"] == "aula":
        identity = "aula\n" + identity
    return hashlib.sha256(identity.encode()).hexdigest()[:32] + "@aula-feed"


_AULA_CATEGORY_LABELS = {
    "lesson": "Lessons / classroom bookings",
    "event": "School events",
    "school_home_meeting": "School-home meetings",
    "parental_meeting": "Parent meetings",
    "presence_holiday": "Holidays & absence",
    "holiday": "Holidays",
    "excursion": "Excursions",
    "birthday": "Birthdays",
    "other": "Other Aula calendar events",
}
_AI_CATEGORY_LABELS = {
    "ai:post": "AI events from posts",
    "ai:message": "AI events from messages",
    "ai:other": "Other AI-recognized events",
}
CATEGORY_KEY = re.compile(r"^(aula|ai):[a-z0-9_]{1,40}$")


def event_category(event):
    """Stable subscription category key for a recognized event."""
    if provenance(event)["origin"] == "parent":
        return "parent:sickness"
    if provenance(event)["origin"] == "aula":
        kind = re.sub(r"[^a-z0-9_]+", "_", str(event.get("calendar_kind") or "").casefold()).strip("_")[:40]
        return "aula:" + (kind or "other")
    source = str(event.get("source_id") or "")
    return "ai:message" if source.startswith("thread:") else "ai:post" if source.startswith("post:") else "ai:other"


def category_label(key):
    if key == "parent:sickness":
        return "Reported sick days"
    if key in _AI_CATEGORY_LABELS:
        return _AI_CATEGORY_LABELS[key]
    kind = key.removeprefix("aula:")
    return _AULA_CATEGORY_LABELS.get(kind) or "Aula: " + kind.replace("_", " ").capitalize()


def build_ics(events, profiles, excluded_categories=()):
    children = {p["slug"]: p for p in profiles if p["calendar_enabled"]}
    excluded = set(excluded_categories)
    merged = {}
    for event in events:
        if excluded and event_category(event) in excluded:
            continue
        slugs = [slug for slug in event["child_slugs"] if slug in children]
        if not slugs and event["child_slugs"]:
            continue
        uid = event_uid(event)
        entry = merged.setdefault(uid, {**event, "slugs": set(), "sources": set()})
        entry["slugs"].update(slugs)
        entry["sources"].add(f'{event["source_title"]} ({event["source_date"]})\n{event["evidence"]}')
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Aula UI//Family calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Aula family events",
             "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"]
    for uid, event in sorted(merged.items()):
        names = ", ".join(children[s]["display_name"] for s in sorted(event["slugs"])) or "Family (child unassigned)"
        lines += ["BEGIN:VEVENT", "UID:" + uid,
                  "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                  "SUMMARY:" + escape(names + " · " + event["title"])]
        if event["all_day"]:
            lines += ["DTSTART;VALUE=DATE:" + event["start"].replace("-", ""),
                      "DTEND;VALUE=DATE:" + event["end"].replace("-", "")]
        else:
            for key, value in [("DTSTART", event["start"]), ("DTEND", event["end"])]:
                if value:
                    lines.append(key + ":" + datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        lines += ["DESCRIPTION:" + escape(
            ("Manually confirmed by parent.\n" if event.get("manually_confirmed") else "")
            + (event.get("description", "") + "\n\n" if event.get("description") else "")
            + ("Parent sickness report confirmed by Aula.\n\n" if provenance(event)["origin"] == "parent"
               else "Official Aula calendar event.\n\n" if provenance(event)["origin"] == "aula"
               else "Recognized from Aula; verify against the original.\n\n")
            + "\n\n".join(sorted(event["sources"]))),
                  "LOCATION:" + escape(event["location"]), "STATUS:CONFIRMED", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(folded(line) for line in lines) + "\r\n").encode("utf-8"), len(merged)


class CalendarFeed:
    def __init__(self, controller, directory="/homeassistant/www/aula-feed"):
        self.controller = controller
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.events = {
            source: group for source, group in (controller.store.get("recognized_events", {}) or {}).items()
            if group
        }
        self.error = None
        self.count = 0
        self.content = b""
        self._rendered = None
        self._written = None
        # Legacy automatically published AI guesses now need confirmation.
        # Explicit parent approvals and their original UIDs remain authoritative.
        ai_uids = {
            event_uid(event) for group in self.events.values() for event in group
            if not event["source_id"].startswith("event:")
        }
        ai_uids.update(
            uid for uid, review in controller.profiles.event_reviews().items()
            if provenance(review["event"])["origin"] == "ai"
        )
        for group in list(self.events.values()):
            for event in group:
                if event["source_id"].startswith("event:") and event.get("calendar_uid") in ai_uids:
                    event.pop("calendar_uid", None)
                if event["source_id"].startswith("event:") and not event.get("origin"):
                    legacy_uid = event_uid({**event, "source_id": ""})
                    if legacy_uid not in ai_uids:
                        event["calendar_uid"] = legacy_uid
                source = next((item for item in controller.store.get("feed", [])
                               if item["id"] == event["source_id"]), {})
                event.update(provenance(event, source))
                if event["review_required"]:
                    self._pending(event, event_uid(event))
        self.controller.store.set("recognized_events", self.events)

    def snapshot_events(self, archived=False):
        with self.lock:
            hidden = self.controller.profiles.archived_events()
            reviews = self.controller.profiles.event_reviews()
            # (source, title) -> decided starts; "" means any start. Indexed once
            # instead of scanning every review for every event.
            decisions = {}
            for review in reviews.values():
                if not review.get("is_recommendation") or review.get("status") not in ("accepted", "declined"):
                    continue
                candidate = review.get("candidate", review.get("event", {}))
                candidate_start = candidate.get("start", "")
                if candidate_start and not isinstance(candidate_start, str):
                    continue
                decisions.setdefault(
                    (candidate.get("source_id"), str(candidate.get("title", "")).casefold()), set()
                ).add(candidate_start or "")
            unique = {}
            for group in self.events.values():
                for event in group:
                    event = {**event, **provenance(event)}
                    if event["review_required"]:
                        continue
                    # A parent's decision about a recommendation wins if AI
                    # later reports the same source event as fully validated.
                    if event["origin"] != "aula":
                        starts = decisions.get((event["source_id"], event["title"].casefold()))
                        if starts and ("" in starts or event["start"] in starts):
                            continue
                    uid = event_uid(event)
                    if uid in unique:
                        unique[uid]["child_slugs"] = sorted(set(unique[uid]["child_slugs"] + event["child_slugs"]))
                        unique[uid]["source_ids"] = sorted(set(unique[uid]["source_ids"] + [event["source_id"]]))
                    else:
                        unique[uid] = {**event, "uid": uid, "source_ids": [event["source_id"]]}
            for uid, review in reviews.items():
                if review.get("status") == "accepted":
                    reviewed = {**unique.get(uid, {}), **review["event"], **provenance(review["event"]),
                                "review_required": False}
                    unique[uid] = {**reviewed, "uid": uid,
                                   "source_ids": unique.get(uid, {}).get(
                                       "source_ids", review["event"].get("source_ids", [review["event"]["source_id"]]))}
            return [
                event for uid, event in unique.items()
                if ((uid in hidden) or reviews.get(uid, {}).get("status") == "declined") == archived
            ]

    def recommendations(self, archived=False):
        with self.lock:
            return [
                {**review["event"], **provenance(review["event"]), "uid": uid, "review_required": True,
                 "issue": review["issue"], "recommendation": review["recommendation"]}
                for uid, review in self.controller.profiles.event_reviews().items()
                if review.get("status") == ("declined" if archived else "pending")
                and review.get("is_recommendation")
            ]

    @observed("calendar.recommend", level=logging.DEBUG)
    def recommend(self, item, raw, issue):
        if not isinstance(raw, dict):
            raw = {}
        candidate = {
            key: raw.get(key, "") if isinstance(raw.get(key, ""), str) else ""
            for key in ("title", "start", "end", "location", "evidence")
        }
        candidate["title"] = candidate["title"] or f"Possible event from {item['title']}"
        candidate["all_day"] = raw.get("all_day") if isinstance(raw.get("all_day"), bool) else True
        try:
            candidate["evidence"] = source_quote(candidate["evidence"], item.get("original_text", ""))
        except ValueError:
            candidate["evidence"] = ""
        candidate.update(source_id=item["id"], source_title=item["title"],
                         source_date=item["date"], child_slugs=item["child_slugs"])
        candidate.update(provenance({
            "date_rationale": raw.get("date_rationale", "")
            if isinstance(raw.get("date_rationale", ""), str) else "",
        }, item))
        identity = item["id"] + "\n" + candidate["title"].casefold() + "\n" + candidate["start"]
        uid = hashlib.sha256(identity.encode()).hexdigest()[:32] + "@aula-review"
        with self.lock:
            self._pending(candidate, uid, issue, raw.get("recommendation"))

    def _pending(self, candidate, uid, issue="", recommendation=""):
        reason = retrospective_reason(candidate, timezone_name=self.controller.config.timezone)
        if not reason and any(similar_calendar_event(candidate, event)
                              for event in self.snapshot_events()):
            reason = "similar event already exists in the calendar"
        if reason:
            _LOGGER.info("event=calendar.suppressed source_id=%s reason=%s", candidate["source_id"], reason)
            return
        reviews = self.controller.profiles.event_reviews()
        for review in reviews.values():
            original = review.get("candidate", review.get("event", {}))
            if (review.get("status") in ("accepted", "declined")
                and original.get("source_id") == candidate["source_id"]
                and str(original.get("title", "")).casefold() == candidate["title"].casefold()
                and (not original.get("start") or original["start"] == candidate["start"])):
                return
        if uid not in reviews or reviews[uid].get("status") == "superseded":
            self.controller.profiles.save_event_review(uid, {
                "status": "pending", "event": candidate,
                "issue": str(issue or "AI-extracted date and details need parent approval."),
                "recommendation": str(recommendation or
                    "Check the original quote, source and date, then approve or correct this event."),
                "is_recommendation": True, "candidate": candidate,
            })
        elif reviews[uid].get("status") == "pending":
            previous = reviews[uid]
            event = {**candidate,
                     "child_slugs": sorted(set(candidate["child_slugs"] + previous["event"]["child_slugs"])),
                     "source_ids": sorted(set(previous["event"].get("source_ids", [previous["event"]["source_id"]])
                                              + [candidate["source_id"]]))}
            self.controller.profiles.save_event_review(uid, {**previous, "event": event})

    @observed("calendar.review")
    def review(self, uid, action, changes=None):
        _LOGGER.info("event=calendar.review_requested action=%s edited=%s",
                     action if action in ("accept", "decline", "edit") else "invalid", changes is not None)
        with self.lock:
            reviews = self.controller.profiles.event_reviews()
            previous = reviews.get(uid)
            event = next((e for e in self.snapshot_events() + self.snapshot_events(True)
                          if e["uid"] == uid), None)
            if previous:
                event = previous["event"]
            if event is None:
                raise ValueError("Unknown calendar event")
            if action == "decline":
                self.controller.profiles.save_event_review(uid, {
                    **(previous or {}), "status": "declined", "event": event,
                })
            elif action in ("accept", "edit"):
                if changes is not None:
                    required = {"title", "start", "end", "all_day", "location"}
                    if not isinstance(changes, dict) or not required <= set(changes) or set(changes) - required - {"description"}:
                        raise ValueError("Supply title, start, end, all_day, location and optional description")
                    event = {**event, **changes}
                if not isinstance(event.get("description", ""), str):
                    raise ValueError("Event description must be text")
                # Manual confirmation is authoritative, but dates still must
                # be real and ordered. Preserve the original evidence separately.
                checked = validate_events([{**event, "evidence": "Manually confirmed"}], {
                    "id": event["source_id"], "title": event["source_title"],
                    "date": event["source_date"], "child_slugs": event["child_slugs"],
                    "original_text": "Manually confirmed",
                }, self.controller.config.timezone)[0]
                checked.update(evidence=event.get("evidence") or "Manually confirmed by parent",
                               calendar_uid=uid, manually_confirmed=True,
                               description=event.get("description", ""))
                checked.update({key: event.get(key, "") for key in
                                ("source_author", "source_excerpt", "date_rationale",
                                 "calendar_kind", "source_content_type")})
                checked["review_required"] = False
                checked["source_ids"] = event.get("source_ids", [event["source_id"]])
                self.controller.profiles.save_event_review(uid, {
                    **(previous or {}), "status": "accepted", "event": checked,
                })
                self.controller.profiles.set_event_archived(uid, False)
            else:
                raise ValueError("Action must be accept, decline or edit")
        self.publish()
        if self.error:
            raise OSError(self.error)

    @observed("calendar.archive")
    def set_archived(self, uid, archived):
        _LOGGER.info("event=calendar.archive_requested archived=%s", bool(archived))
        with self.lock:
            if not any(event["uid"] == uid for event in self.snapshot_events() + self.snapshot_events(True)):
                raise ValueError("Unknown calendar event")
            review = self.controller.profiles.event_reviews().get(uid)
            if review and review.get("status") == "declined":
                self.controller.profiles.save_event_review(uid, {**review, "status": "accepted"})
            self.controller.profiles.set_event_archived(uid, archived)
        self.publish()
        if self.error:
            raise OSError(self.error)

    def categories(self):
        """Categories present in the calendar, plus any excluded ones, with event counts."""
        excluded = self.controller.profiles.calendar_excluded_categories()
        counts = dict.fromkeys(excluded, 0)
        for event in self.snapshot_events():
            key = event_category(event)
            counts[key] = counts.get(key, 0) + 1
        return [
            {"key": key, "label": category_label(key), "count": count, "excluded": key in excluded}
            for key, count in sorted(counts.items(), key=lambda pair: (not pair[0].startswith("aula:"), category_label(pair[0])))
        ]

    @observed("calendar.subscription.categories")
    def set_excluded_categories(self, keys):
        if not isinstance(keys, list) or len(keys) > 50 or not all(
                isinstance(key, str) and CATEGORY_KEY.match(key) for key in keys):
            raise ValueError("excluded must be a list of category keys")
        with self.lock:
            self.controller.profiles.set_calendar_excluded_categories(sorted(set(keys)))
            _LOGGER.info("Calendar subscription now excludes %d categor%s: %s", len(set(keys)),
                         "y" if len(set(keys)) == 1 else "ies", ", ".join(sorted(set(keys))) or "none")
        self.publish()
        if self.error:
            raise OSError(self.error)

    @observed("calendar.subscription.rotate")
    def rotate(self):
        with self.lock:
            old_filename = self.controller.profiles.calendar_path.rsplit("/", 1)[-1]
            old_path = self.directory / old_filename
            # Revoke the previous URL before issuing another one.
            old_path.unlink(missing_ok=True)
            self.controller.profiles.rotate_calendar_token()
        self.publish()

    @observed("calendar.record", level=logging.DEBUG)
    def record(self, item, events):
        _LOGGER.debug("event=calendar.candidates count=%d", len(events))
        with self.lock:
            prepared = []
            for event in events:
                event = {**event, **provenance(event, item)}
                for previous in self.events.get(item["id"], []):
                    if (event["origin"] == "aula" and previous.get("calendar_uid")
                        and previous["title"] == event["title"] and previous["start"] == event["start"]):
                        event["calendar_uid"] = previous["calendar_uid"]
                if event["review_required"]:
                    self._pending(event, event_uid(event))
                prepared.append(event)
            if prepared:
                self.events[item["id"]] = prepared
            else:
                # Sources without events (e.g. native events past the sync window) leave no entry.
                self.events.pop(item["id"], None)
            pending = [
                (uid, review) for uid, review in self.controller.profiles.event_reviews().items()
                if review.get("status") == "pending" and review["event"]["source_id"] == item["id"]
            ]
            # Only needed (and costly across all sources) when this item has open reviews.
            active = {event_uid(event) for group in self.events.values() for event in group} if pending else set()
            for uid, review in pending:
                if (uid not in active
                    and (uid.endswith("@aula-feed") or (
                        item.get("original_text") is not None and review["event"].get("source_excerpt")
                        and review["event"]["source_excerpt"] != item["original_text"]))):
                    self.controller.profiles.save_event_review(uid, {**review, "status": "superseded"})
            self.controller.store.set("recognized_events", self.events)

    def prune_weak_reviews(self):
        """Retire pending AI suggestions that fail the evidence gate or duplicate a day."""
        with self.lock:
            reviews = self.controller.profiles.event_reviews()
            default_issue = "AI-extracted date and details need parent approval."
            decided_days = {
                (review["event"].get("source_id"), _day(review["event"].get("start")))
                for review in reviews.values() if review.get("status") == "accepted"
            }
            pending = sorted(
                ((uid, review) for uid, review in reviews.items()
                 if review.get("status") == "pending" and review.get("is_recommendation")),
                key=lambda pair: -len(str(pair[1]["event"].get("evidence") or "")),
            )
            seen, per_source, retired = set(), {}, 0
            existing_events = self.snapshot_events()
            for uid, review in pending:
                event = review["event"]
                source_id = str(event.get("source_id") or "")
                if source_id.startswith("event:"):
                    continue
                day = _day(event.get("start"))
                reason = retrospective_reason(event, timezone_name=self.controller.config.timezone)
                if not reason and any(similar_calendar_event(event, existing) for existing in existing_events):
                    reason = "similar event already exists in the calendar"
                reason = reason or weak_evidence_reason(event, source_id, validated=review.get("issue") == default_issue)
                if not reason and day and ((source_id, day) in seen or (source_id, day) in decided_days):
                    reason = f"duplicate suggestion for {day} from the same source"
                if (not reason and source_id.startswith("thread:")
                        and per_source.get(source_id, 0) >= THREAD_RECOMMENDATION_LIMIT):
                    reason = f"thread already has {THREAD_RECOMMENDATION_LIMIT} suggestions"
                if reason:
                    self.controller.profiles.save_event_review(
                        uid, {**review, "status": "superseded", "dismissed_reason": reason})
                    _LOGGER.info("Retired calendar suggestion %r from %s: %s",
                                 event.get("title", ""), source_id, reason)
                    retired += 1
                    continue
                if day:
                    seen.add((source_id, day))
                per_source[source_id] = per_source.get(source_id, 0) + 1
            return retired

    @observed("calendar.publish", level=logging.DEBUG)
    def publish(self):
        with self.lock:
            self.prune_weak_reviews()
            content, self.count = build_ics(
                self.snapshot_events(),
                self.controller.profiles.snapshot(),
                self.controller.profiles.calendar_excluded_categories(),
            )
            # publish() runs after every AI step; unchanged events keep the
            # previous bytes (and DTSTAMP) so the file is not rewritten.
            signature = _without_dtstamp(content)
            if self._rendered and self._rendered[0] == signature:
                content = self._rendered[1]
            self._rendered = (signature, content)
            self.content = content
            _LOGGER.debug("event=calendar.rendered event_count=%d bytes=%d", self.count, len(self.content))
            # Unit tests/off-host runs have no HA config mount.
            if not self.directory.parent.exists():
                self.error = "Calendar subscription unavailable: Home Assistant config mount is missing."
                return
            temporary = None
            try:
                self.directory.mkdir(exist_ok=True)
                filename = self.controller.profiles.calendar_path.rsplit("/", 1)[-1]
                target = self.directory / filename
                if self._written == (target, signature) and target.exists():
                    self.error = None
                    return
                with tempfile.NamedTemporaryFile("wb", dir=self.directory, delete=False) as handle:
                    temporary = handle.name
                    handle.write(self.content)
                os.chmod(temporary, 0o644)
                os.replace(temporary, target)
                temporary = None
                self._written = (target, signature)
                self.error = None
            except OSError as err:
                self.error = f"Calendar publication failed: {err}"
                _LOGGER.error("%s", self.error)
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
