"""Validated, persistent UI settings independent of the poller's state store."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from .diagnostics import observed, protect
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from .config import ChildConfig

_LOGGER = logging.getLogger(__name__)
AI_SOURCES = ("anthropic", "google", "cloudflare")
AI_TASK_ENTITY_RE = re.compile(r"^ai_task\.[a-z0-9_]+$")
_IDENTITY_FIELDS = ("slug", "name", "aula_name", "aula_child_id", "institution_type")
CALENDAR_STATE_RETENTION_DAYS = 180


def _iso_day(value):
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _review_day(review):
    """Latest calendar day a review refers to, or None when it carries no date."""
    days = [
        _iso_day(part.get(key))
        for part in (review.get("event"), review.get("candidate")) if isinstance(part, dict)
        for key in ("start", "end")
    ]
    days = [day for day in days if day]
    return max(days) if days else None


def _review_sources(review):
    sources = set()
    for part in (review.get("event"), review.get("candidate")):
        if isinstance(part, dict):
            sources.add(str(part.get("source_id") or ""))
            sources.update(str(source) for source in part.get("source_ids") or [] if source)
    sources.discard("")
    return sources


def _identity(child):
    return {field: getattr(child, field) for field in _IDENTITY_FIELDS}


class Profiles:
    def __init__(self, config):
        self.path = Path(config.data_dir) / "profiles.json"
        self.timezone = config.timezone
        self.lock = threading.RLock()
        self.data = {"children": {}, "calendar_token": secrets.token_urlsafe(32), "profile_version": 2}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        for settings in self.data.get("children", {}).values():
            settings.pop("timezone", None)
        self._children = self._resolve_children(config.children)
        if "ai_settings" not in self.data:
            # One-time seed from the retired ai_task_entity add-on option.
            self.data["ai_settings"] = {"ai_task_entity": config.ai_task_entity or ""}
        settings = self.data["ai_settings"]
        if "ai_task_entities" not in settings:
            selected = settings.get("ai_task_entity", "")
            self.data["ai_settings"] = {"ai_task_entities": [selected] if selected else []}
        settings = self.data["ai_settings"]
        if "providers" not in settings:
            # Merge the earlier single-source selection into one ordered list.
            first = settings.pop("source", "")
            settings["providers"] = ([first] if first else []) + list(settings.get("ai_task_entities", []))
        if self.path.exists():
            if "profile_version" not in self.data:
                # Pin the institution type that older profiles left implicit.
                for child in self.children:
                    saved = self.data["children"].setdefault(child.slug, {})
                    saved.setdefault("institution_type", child.institution_type or "school")
                self.data["profile_version"] = 2
        self.save()

    def _resolve_children(self, configured):
        """Merge legacy option children with identities remembered from discovery.

        Child slugs key all cached data, so identities discovered after login
        are remembered here and restored when the retired ``children`` option
        is no longer supplied by Supervisor.
        """
        remembered = {}
        for raw in self.data.get("child_identities", []):
            try:
                remembered[raw["slug"]] = ChildConfig(
                    name=raw["name"], slug=raw["slug"], messages_sensor="",
                    aula_name=raw.get("aula_name", ""), aula_child_id=raw.get("aula_child_id", ""),
                    institution_type=raw.get("institution_type", "school"),
                )
            except (KeyError, TypeError):
                _LOGGER.warning("Ignoring malformed remembered child identity")
        resolved = [
            replace(child, aula_child_id=child.aula_child_id or remembered[child.slug].aula_child_id,
                    aula_name=child.aula_name or remembered[child.slug].aula_name)
            if child.slug in remembered else child
            for child in configured
        ]
        slugs = {child.slug for child in configured}
        resolved.extend(child for slug, child in remembered.items() if slug not in slugs)
        self.data["child_identities"] = [_identity(child) for child in resolved]
        return tuple(resolved)

    @property
    def children(self):
        return self._children

    @children.setter
    def children(self, value):
        with self.lock:
            self._children = tuple(value)
            identities = [_identity(child) for child in self._children]
            if identities == self.data.get("child_identities"):
                return
            previous = self.data.get("child_identities")
            self.data["child_identities"] = identities
            try:
                self.save()
            except OSError as err:
                self.data["child_identities"] = previous
                _LOGGER.error("Could not remember discovered children: %s", err)

    @property
    def ai_task_entity(self):
        with self.lock:
            return next(iter(self.ai_task_entities), "")

    @property
    def ai_task_entities(self):
        with self.lock:
            return tuple(self.data.get("ai_settings", {}).get("ai_task_entities", []))

    @observed("settings.ai.update")
    def set_ai_task_entities(self, entity_ids):
        if (not isinstance(entity_ids, list) or len(entity_ids) > 20
                or any(not isinstance(value, str) or not AI_TASK_ENTITY_RE.fullmatch(value)
                       for value in entity_ids) or len(set(entity_ids)) != len(entity_ids)):
            raise ValueError("Choose a unique ordered list of at most 20 ai_task entities")
        with self.lock:
            previous = self.data.get("ai_settings")
            kept = [value for value in (previous or {}).get("providers", []) if value in AI_SOURCES]
            self.data["ai_settings"] = {**(previous or {}), "ai_task_entities": list(entity_ids),
                                        "providers": kept + list(entity_ids)}
            try:
                self.save()
            except OSError:
                self.data["ai_settings"] = previous
                raise

    @property
    def ai_providers(self):
        with self.lock:
            return tuple(self.data.get("ai_settings", {}).get("providers", []))

    @observed("settings.ai.update")
    def set_ai_providers(self, providers):
        if (not isinstance(providers, list) or len(providers) > 20 or len(set(providers)) != len(providers)
                or any(not isinstance(value, str) or not (value in AI_SOURCES or AI_TASK_ENTITY_RE.fullmatch(value))
                       for value in providers)):
            raise ValueError("Choose a unique ordered list of at most 20 AI providers")
        with self.lock:
            previous = self.data.get("ai_settings")
            self.data["ai_settings"] = {
                **(previous or {}), "providers": list(providers),
                "ai_task_entities": [value for value in providers if value not in AI_SOURCES]}
            try:
                self.save()
            except OSError:
                self.data["ai_settings"] = previous
                raise

    @property
    def ai_credentials(self):
        """Own-key settings entered in the app; for server-side use only."""
        with self.lock:
            return {source: dict(value) for source, value in
                    self.data.get("ai_settings", {}).get("credentials", {}).items()}

    @observed("settings.ai.update")
    def set_ai_credential(self, source, values):
        if source not in AI_SOURCES:
            raise ValueError("Unknown AI provider")
        with self.lock:
            previous = self.data.get("ai_settings")
            settings = {**(previous or {})}
            credentials = {name: dict(value) for name, value in settings.get("credentials", {}).items()}
            if values is None:
                credentials.pop(source, None)
            else:
                credentials[source] = values
            settings["credentials"] = credentials
            self.data["ai_settings"] = settings
            try:
                self.save()
            except OSError:
                self.data["ai_settings"] = previous
                raise

    @property
    def reading_entity(self):
        with self.lock:
            return self.data.get("ai_settings", {}).get("reading_entity", "")

    @observed("settings.ai.update")
    def set_reading_entity(self, entity_id):
        if not isinstance(entity_id, str) or (entity_id and not AI_TASK_ENTITY_RE.fullmatch(entity_id)):
            raise ValueError("Choose built-in search or an ai_task entity")
        with self.lock:
            previous = self.data.get("ai_settings")
            settings = {key: value for key, value in (previous or {}).items() if key != "reading_entity"}
            if entity_id:
                settings["reading_entity"] = entity_id
            self.data["ai_settings"] = settings
            try:
                self.save()
            except OSError:
                self.data["ai_settings"] = previous
                raise

    @observed("settings.ai.update")
    def set_ai_task_entity(self, entity_id):
        if not isinstance(entity_id, str) or (entity_id and not AI_TASK_ENTITY_RE.match(entity_id)):
            raise ValueError("Choose Home Assistant default or an ai_task entity")
        self.set_ai_task_entities([entity_id] if entity_id else [])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False) as handle:
            json.dump(self.data, handle)
            temporary = handle.name
        os.replace(temporary, self.path)

    def snapshot(self):
        with self.lock:
            return [
                {
                    "slug": child.slug, "name": child.name,
                    "aula_name": child.aula_name or child.name,
                    "display_name": self.data["children"].get(child.slug, {}).get("display_name", child.name),
                    "institution_type": self.data["children"].get(child.slug, {}).get("institution_type", child.institution_type),
                    "calendar_enabled": self.data["children"].get(child.slug, {}).get("calendar_enabled", True),
                    "timezone": self.timezone,
                }
                for child in self.children
            ]

    @observed("settings.child.update")
    def update(self, slug, value):
        if slug not in {child.slug for child in self.children}:
            raise ValueError("Unknown child")
        if not isinstance(value, dict) or set(value) != {
            "display_name", "institution_type", "calendar_enabled"
        }:
            raise ValueError(
                "Supply display_name, institution_type and calendar_enabled; "
                "timezone is managed in the Home Assistant add-on configuration"
            )
        if not isinstance(value["display_name"], str) or not 1 <= len(value["display_name"].strip()) <= 80:
            raise ValueError("Display name must be 1–80 characters")
        if value["institution_type"] not in ("school", "daycare"):
            raise ValueError("Institution type must be school or daycare")
        if not isinstance(value["calendar_enabled"], bool):
            raise ValueError("Calendar inclusion must be a boolean")
        with self.lock:
            previous = dict(self.data["children"])
            self.data["children"][slug] = {**value, "display_name": value["display_name"].strip()}
            try:
                self.save()
            except OSError:
                self.data["children"] = previous
                raise

    @property
    def calendar_path(self):
        with self.lock:
            protect(self.data["calendar_token"])
            return "/api/aula_calendar/" + self.data["calendar_token"] + ".ics"

    def siri_enabled(self):
        with self.lock:
            return bool(self.data.get("siri_key_hash"))

    def verify_siri_key(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return False
        with self.lock:
            expected = self.data.get("siri_key_hash", "")
            return bool(expected) and hmac.compare_digest(expected, hashlib.sha256(token.encode()).hexdigest())

    def set_siri_key(self, enabled):
        token = secrets.token_urlsafe(32) if enabled else ""
        protect(token)
        with self.lock:
            previous = self.data.get("siri_key_hash", "")
            self.data["siri_key_hash"] = hashlib.sha256(token.encode()).hexdigest() if token else ""
            try:
                self.save()
            except OSError:
                self.data["siri_key_hash"] = previous
                raise
        return token

    def rotate_calendar_token(self):
        with self.lock:
            previous = self.data["calendar_token"]
            self.data["calendar_token"] = secrets.token_urlsafe(32)
            try:
                self.save()
            except OSError:
                self.data["calendar_token"] = previous
                raise
            return previous + ".ics"

    def calendar_excluded_categories(self):
        with self.lock:
            return set(self.data.get("calendar_excluded_categories", []))

    def set_calendar_excluded_categories(self, keys):
        with self.lock:
            previous = self.data.get("calendar_excluded_categories", [])
            self.data["calendar_excluded_categories"] = list(keys)
            try:
                self.save()
            except OSError:
                self.data["calendar_excluded_categories"] = previous
                raise

    def archived_events(self):
        with self.lock:
            return set(self.data.get("archived_events", []))

    def set_event_archived(self, uid, archived):
        with self.lock:
            previous = self.data.get("archived_events", [])
            updated = set(previous)
            if archived:
                updated.add(uid)
            else:
                updated.discard(uid)
            self.data["archived_events"] = sorted(updated)
            try:
                self.save()
            except OSError:
                self.data["archived_events"] = previous
                raise

    def prune_calendar_state(self, known_uids, active_sources, today,
                             retention_days=CALENDAR_STATE_RETENTION_DAYS):
        """Forget archive flags and event reviews that can no longer matter.

        Only entries unrelated to any currently recognized event or source are
        pruned, and only when their event day (or, lacking one, the day the
        entry was first seen here) is more than ``retention_days`` ago.
        Accepted reviews are calendar events themselves and are always kept.
        Returns ``(archived_removed, reviews_removed)``.
        """
        cutoff = today - timedelta(days=retention_days)
        known_uids, active_sources = set(known_uids), set(active_sources)
        with self.lock:
            reviews = self.data.get("event_reviews", {})
            seen = self.data.get("calendar_state_seen", {})
            tracked = {}

            def expired(uid):
                # Date-less entries age from the first time a prune pass saw them.
                first = _iso_day(seen.get(uid)) or today
                tracked[uid] = first.isoformat()
                return first < cutoff

            kept_reviews = {}
            for uid, review in reviews.items():
                if (uid in known_uids or not isinstance(review, dict) or review.get("status") == "accepted"
                        or _review_sources(review) & active_sources):
                    kept_reviews[uid] = review
                    continue
                day = _review_day(review)
                if not (day < cutoff if day else expired(uid)):
                    kept_reviews[uid] = review
            archived = self.data.get("archived_events", [])
            kept_archived = [uid for uid in archived
                             if uid in known_uids or uid in kept_reviews or not expired(uid)]
            kept = set(kept_archived) | set(kept_reviews)
            tracked = {uid: day for uid, day in tracked.items() if uid in kept}
            removed = (len(archived) - len(kept_archived), len(reviews) - len(kept_reviews))
            if not any(removed) and tracked == seen:
                return removed
            previous = {key: self.data.get(key) for key in ("archived_events", "event_reviews", "calendar_state_seen")}
            self.data.update(archived_events=kept_archived, event_reviews=kept_reviews, calendar_state_seen=tracked)
            try:
                self.save()
            except OSError:
                for key, value in previous.items():
                    if value is None:
                        self.data.pop(key, None)
                    else:
                        self.data[key] = value
                raise
            return removed

    def event_reviews(self):
        with self.lock:
            return json.loads(json.dumps(self.data.get("event_reviews", {})))

    def save_event_review(self, uid, value):
        with self.lock:
            previous = dict(self.data.get("event_reviews", {}))
            self.data.setdefault("event_reviews", {})[uid] = value
            try:
                self.save()
            except OSError:
                self.data["event_reviews"] = previous
                raise
