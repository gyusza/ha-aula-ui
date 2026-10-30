"""Child-attributed announcements and a durable, mixed chronological feed."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from .diagnostics import describe, observed
import queue
import time
from datetime import datetime, time as dt_time, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .aula_api import (
    AulaApiError, AulaRateLimited, _call, fetch_thread_messages, message_from_thread_summary, thread_matches_child,
)
from .llm import (
    LLMError, enrich_content, summarise,
    translate_content, translation_keys, translation_work,
)
from .calendar_feed import gate_suggestions, validate_events
from .messages import date_key, normalise_message, strip_html
from .avatars import note_author
from . import gallery

_LOGGER = logging.getLogger(__name__)
AI_INTERVAL = 6.5
THREAD_REFRESH_SECONDS = 24 * 3600
STALE_REFRESHES_PER_SYNC = 3
THREAD_METHOD = "messaging.getMessagesForThread"
# Completed AI work and full threads for content no longer in the feed are kept
# this long, so briefly missing sources return without new AI calls.
CACHE_RETENTION_SECONDS = 90 * 24 * 3600
AI_WARNING = "AI is temporarily unavailable. Originals remain readable; summaries and translations retry automatically."
ENRICHMENT_VERSION = 4
_ENRICHMENT_FIELDS = (
    "summary", "is_translated", "source", "translated_title", "thread_messages",
    "translation_pending", "translation_error", "analysis_error",
    "recognized_events", "recognition_warning",
    "title_is_translated", "title_source_language",
    "translation_available",
)


def rate_limit_cause(err: BaseException) -> AulaRateLimited | None:
    """The 429/budget pause behind ``err``, also when wrapped by the API helper."""
    for candidate in (err, err.__cause__):
        if isinstance(candidate, AulaRateLimited):
            return candidate
    return None


def source_error_text(err: BaseException) -> str:
    """User-facing source error; a rate limit explains the pause, not the wrapper."""
    return str(rate_limit_cause(err) or err)


def _source_paused(client: Any, method: str) -> bool:
    check = getattr(client, "rate_limited", None)
    if not callable(check):
        return False
    try:
        return bool(check(method))
    except TypeError:
        return bool(check())


def _source_pause_error(client: Any, method: str) -> BaseException:
    try:
        return client._rate_limit_error(method)
    except TypeError:
        return client._rate_limit_error()


def _all_day_source_end(start: str, end: str) -> str:
    """Aula/EasyIQ may end an all-day entry on its last included day; ICS needs it exclusive."""
    try:
        first = datetime.fromisoformat(start[:10]).date()
        last_moment = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None
    except ValueError:
        return end
    if last_moment is None:
        return ""
    last = last_moment.date()
    if last <= first or (len(end) > 10 and last_moment.time() != dt_time.min):
        last += timedelta(days=1)
    return max(last, first + timedelta(days=1)).isoformat()


def content_fingerprint(item, timezone_name):
    """AI routing is not source content and must never invalidate saved work."""
    return hashlib.sha256(json.dumps({
        "title": item["title"], "original_text": item["original_text"],
        "date": item["date"], "timezone": timezone_name,
        "messages": sorted([{
            key: message.get(key, "") for key in ("id", "sender", "date", "original_text")
        } for message in item.get("thread_messages", [])],
            key=lambda message: (str(message["date"]), str(message["id"]))),
        "thread_complete": item.get("thread_complete", True),
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def migrate_cached_content(controller):
    """Bind legacy cached results to their persisted source before refreshing."""
    cache = controller.store.get("feed_summaries", {}) or {}
    migrated = 0
    for item in controller.store.get("feed", []) or []:
        if item.get("content_type") == "Presence":
            continue
        saved = cache.get(item["id"], {})
        if saved.get("content_fingerprint"):
            continue
        zone = controller.config.timezone
        if not item.get("fingerprint"):
            continue
        if saved.get("fingerprint") != item["fingerprint"]:
            # Before this fix, changing the provider rekeyed the visible item
            # before its old completed cache was consumed. Verify that cache
            # against the exact old algorithm, never just its item ID.
            source = {
                "title": item["title"], "original_text": item["original_text"],
                "date": item["date"], "timezone": zone,
                "messages": [{key: message.get(key, "") for key in ("id", "sender", "date", "original_text")}
                             for message in item.get("thread_messages", [])],
                "thread_complete": item.get("thread_complete", True),
            }
            entities = {"", controller.config.ai_task_entity, *controller.profiles.ai_task_entities}
            candidates = {
                hashlib.sha256(json.dumps({
                    **source, "provider": "ha_ai_task", "model": model, "ai_task_entity": entity,
                }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                for model in ("", "claude-sonnet-4-5") for entity in entities
            }
            if saved.get("fingerprint") not in candidates:
                continue
        fingerprint = content_fingerprint(item, zone)
        saved["content_fingerprint"] = fingerprint
        saved["fingerprint"] = fingerprint
        item["fingerprint"] = fingerprint
        migrated += 1
    controller.store.set("feed_summaries", cache)
    if migrated:
        controller.store.save()
        _LOGGER.info("event=ai.cache.migrated count=%d", migrated)


def fetch_posts(client: Any, children: Any, pages: int) -> list[dict[str, Any]]:
    """Query each child's posts using both child and guardian profiles.

    Aula silently returns an empty list when either profile is omitted.
    Attribution comes from the filtered request, not the author's institution.
    """
    payload = _call(client, "?method=profiles.getProfilesByLogin")
    profiles = (payload.get("data") or {}).get("profiles")
    if not isinstance(profiles, list):
        raise AulaApiError("Aula profiles response has no profiles list")
    posts: dict[str, dict[str, Any]] = {}
    for child in children:
        pairs: list[tuple[str, str]] = []
        for profile in profiles:
            for relation in profile.get("children") or []:
                matching = {
                    "regardingChildren": [{
                        "displayName": relation.get("name"),
                        "profileId": relation.get("profileId"),
                    }]
                }
                if not thread_matches_child(matching, child):
                    continue
                for guardian in profile.get("institutionProfiles") or []:
                    if guardian.get("institutionCode") == relation.get("institutionCode"):
                        pairs.append((str(guardian["id"]), str(relation["id"])))
        if not pairs:
            raise AulaApiError(f"No guardian/child institution profiles for {child.slug}")
        for guardian_id, child_id in pairs:
            for page in range(pages):
                query = urlencode([
                    ("method", "posts.getAllPosts"), ("parent", "profile"),
                    ("index", page * 20), ("limit", 20),
                    ("institutionProfileIds[]", guardian_id),
                    ("institutionProfileIds[]", child_id),
                ])
                data = (_call(client, "?" + query).get("data") or {})
                batch = data.get("posts")
                if not isinstance(batch, list):
                    raise AulaApiError("Aula posts response has no posts list")
                for raw in batch:
                    owner = raw.get("ownerProfile") or {}
                    if isinstance(owner, dict):
                        note_author(owner.get("fullName") or owner.get("displayName"), owner)
                    content = raw.get("content") or {}
                    message = normalise_message({
                        "id": raw.get("id"), "title": raw.get("title"),
                        "text": content.get("html") if isinstance(content, dict) else content,
                        "sender": owner.get("fullName") or owner.get("displayName"),
                        "date": raw.get("publishAt") or raw.get("timestamp"),
                        "attachments": raw.get("attachments"),
                        "hasAttachments": raw.get("hasAttachments"),
                        "attachmentCount": raw.get("attachmentCount"),
                    })
                    if not message:
                        continue
                    key = "post:" + message["id"]
                    item = posts.setdefault(key, {
                        **message, "id": key, "content_type": "Post", "children": [],
                        "child_slugs": [], "comment_count": raw.get("commentCount", 0),
                    })
                    if child.slug not in item["child_slugs"]:
                        item["children"].append(child.name)
                        item["child_slugs"].append(child.slug)
                if not data.get("hasMorePosts") or not batch:
                    break
    return list(posts.values())


class Feed:
    def __init__(self, controller: Any) -> None:
        self.controller = controller
        migrate_cached_content(controller)
        self._last_ai_call = float("-inf")
        self._retry_at = 0.0
        self._failures = 0
        self._stale_refreshes = 0
        self._thread_pause: BaseException | None = None
        self._reprocess: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._active_call_start: float | None = None
        # Items whose analysis failed this session retry after other work, so
        # one persistently failing item cannot block the whole backlog.
        self._item_failures: dict[str, int] = {}

    @staticmethod
    def _summary_complete(item: dict[str, Any], saved: dict[str, Any]) -> bool:
        return bool(
            saved.get("fingerprint") == item.get("fingerprint")
            and saved.get("summary") and not saved.get("analysis_error")
            and saved.get("source") not in (None, "original", "fallback")
        )

    @property
    def ai_progress(self) -> dict[str, Any]:
        """Count source summaries, titles and individual messages, not queue items."""
        store = self.controller.store
        summaries = store.get("feed_summaries", {}) or {}
        translations = store.get("feed_translations", {}) or {}
        completed = total = remaining_calls = 0
        blocked = runnable = False
        missing_chunks: set[str] = set()
        for item in store.get("feed", []) or []:
            if item.get("content_type") == "Presence":
                continue
            summary_done = self._summary_complete(item, summaries.get(item["id"], {}))
            tasks = [(item.get("title", ""), item.get("title_source_language"), False)]
            tasks.extend(
                (message.get("original_text", ""), message.get("source_language"),
                 bool(message.get("translation_pending") or message.get("translation_error")))
                for message in item.get("thread_messages", []) or []
            )
            total += 1 + len(tasks)
            completed += int(summary_done)
            item_calls = int(not summary_done)
            item_pending = not summary_done
            for text, language, pending in tasks:
                chunks = translation_work(text, translations)
                done = not text.strip() or bool(language and not pending) or not chunks
                completed += int(done)
                if not done:
                    item_pending = True
                    if item.get("thread_complete", True):
                        missing_chunks.update(chunks)
            if item_pending:
                if not item.get("thread_complete", True):
                    blocked = True
                else:
                    runnable = True
                    remaining_calls += item_calls
        remaining_calls += len(missing_chunks)
        pending = total - completed
        retry = math.ceil(self.retry_delay)
        enabled = self.controller.provider is not None
        status = (
            "disabled" if not enabled else
            "idle" if not pending else
            "retrying" if retry and runnable else
            "processing" if runnable else "blocked"
        )
        eta = None
        samples = store.get("feed_ai_timings", []) or []
        samples = [value for value in samples[-32:]
                   if isinstance(value, (int, float)) and 0 < value <= 600]
        if enabled and not pending:
            eta = 0
        elif enabled and samples and not blocked:
            # Timings measure requests only, never thread fetches or retry waits.
            duration = sum(samples) / len(samples)
            elapsed = (max(0, time.monotonic() - self._active_call_start)
                       if self._active_call_start is not None else 0)
            seconds = remaining_calls * max(duration, AI_INTERVAL)
            seconds -= min(elapsed, duration)
            eta = math.ceil(max(0, seconds) + retry)
        return {
            "completed": completed, "total": total, "pending": pending,
            "status": status, "eta_seconds": eta, "retry_seconds": retry,
            "parallel": False,
        }

    def _ai_call_complete(self, successful: bool) -> None:
        started = self._active_call_start
        self._active_call_start = None
        if successful and started is not None:
            duration = time.monotonic() - started
            if 0 < duration <= 600:
                samples = self.controller.store.get("feed_ai_timings", []) or []
                self.controller.store.set("feed_ai_timings", [*samples[-31:], duration])
        self.controller.store.save()
        self.controller.publish_feed()

    @observed("feed.reprocess")
    def request_reprocess(self, item_id: str) -> None:
        if self.controller.provider is None:
            raise ValueError("Configure an AI provider before reprocessing content")
        item = next((item for item in self.controller.store.get("feed", [])
                     if item["id"] == item_id), None)
        if item is None:
            raise ValueError("Unknown feed item")
        if not item.get("thread_complete", True):
            raise ValueError("Refresh the complete conversation before reprocessing it")
        self._reprocess.put(item_id)

    def _conversation(self, thread: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
        """Reuse durable full originals only while the thread revision is unchanged."""
        store = self.controller.store
        cache = store.get("feed_thread_content", {}) or {}
        key = str(thread["id"])
        revision = hashlib.sha256(json.dumps({
            k: thread.get(k) for k in (
                "subject", "latestMessage", "messageCount", "numberOfMessages", "lastUpdated",
            )
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        saved = cache.get(key, {})
        cached = list(saved.get("messages") or [])
        # The newest message already being cached means nothing new was posted.
        # Revisions alone are unreliable: thread summaries embed signed URLs.
        current = bool(cached) and (saved.get("revision") == revision
                                    or str(preview["id"]) in {str(m.get("id")) for m in cached})
        age = time.time() - saved.get("fetched_at", 0)
        client = self.controller.source_client
        limited = self._thread_pause is not None or _source_paused(client, THREAD_METHOD)
        if current and (age < THREAD_REFRESH_SECONDS or limited
                        or self._stale_refreshes >= STALE_REFRESHES_PER_SYNC):
            return {"thread_messages": cached, "thread_complete": True, "thread_fetch_error": ""}
        if current:
            self._stale_refreshes += 1
        if limited:
            return self._conversation_unavailable(
                preview, cached, self._thread_pause or _source_pause_error(client, THREAD_METHOD))
        try:
            messages = fetch_thread_messages(self.controller.source_client, thread["id"])
            if not messages and preview.get("original_text"):
                raise AulaApiError("Thread returned no messages despite a non-empty latest message")
            cache[key] = {"revision": revision, "messages": messages, "fetched_at": time.time()}
            store.set("feed_thread_content", cache)
            return {"thread_messages": messages, "thread_complete": True, "thread_fetch_error": ""}
        except AulaApiError as err:
            pause = rate_limit_cause(err)
            if pause is not None:
                # Expected while Aula throttles: stop fetching threads this cycle
                # and log once instead of once per remaining conversation.
                self._thread_pause = pause
                _LOGGER.warning("event=feed.thread_fetch_paused thread=%s reason=%s; remaining "
                                "conversations show cached/latest messages this cycle", key, describe(pause))
            else:
                _LOGGER.warning("Full thread %s unavailable, showing cached/preview messages: %s",
                                key, source_error_text(err))
            if current:
                return {"thread_messages": cached, "thread_complete": True, "thread_fetch_error": ""}
            return self._conversation_unavailable(preview, cached, err)

    @staticmethod
    def _conversation_unavailable(preview, known, err):
        known = list(known)
        if preview["id"] not in {message["id"] for message in known}:
            known.insert(0, preview)
        return {"thread_messages": known, "thread_complete": False, "thread_fetch_error": source_error_text(err)}

    @observed("feed.collect")
    def update(self) -> None:
        migrate_cached_content(self.controller)
        controller = self.controller
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        self._stale_refreshes = 0
        self._thread_pause = None
        source = controller.thread_source
        if source is not None:
            try:
                if controller.store.get("feed_posts") is None or controller.sync_due("posts"):
                    posts = fetch_posts(
                        controller.source_client, controller.config.children, controller.config.thread_pages
                    )
                    controller.store.set("feed_posts", posts)
                    controller.mark_synced("posts")
            except AulaApiError as err:
                _LOGGER.log(logging.WARNING if rate_limit_cause(err) else logging.ERROR,
                            "Posts collection failed; keeping cached posts: %s", describe(err))
                errors.append(f"Posts unavailable: {source_error_text(err)}")
            items.extend(controller.store.get("feed_posts", []) or [])
            try:
                if controller.store.get("feed_albums") is None or controller.sync_due("albums"):
                    albums = gallery.feed_items(controller.source_client, controller.config.children)
                    controller.store.set("feed_albums", albums)
                    controller.mark_synced("albums")
            except (AulaApiError, ValueError) as err:
                _LOGGER.log(logging.WARNING if rate_limit_cause(err) else logging.ERROR,
                            "Albums collection failed; keeping cached albums: %s", describe(err))
            items.extend(controller.store.get("feed_albums", []) or [])
            if source.available:
                for thread in source.threads:
                    message = message_from_thread_summary(thread)
                    if not message:
                        continue
                    children = [
                        child for child in controller.config.children
                        if thread_matches_child(thread, child)
                    ]
                    items.append({
                        **message, "id": f"thread:{thread['id']}",
                        "content_type": "Message",
                        "children": [child.name for child in children],
                        "child_slugs": [child.slug for child in children],
                        "unread": not thread.get("read", True),
                        **self._conversation(thread, message),
                    })
                controller.store.set("feed_threads", [
                    item for item in items if item["content_type"] == "Message"
                ])
            else:
                errors.append("Messages API unavailable; showing cached messages.")
                items.extend(controller.store.get("feed_threads", []) or [])
        for child in controller.config.children:
            bucket = controller.store.child(child.slug)
            if source is None:
                for message in bucket.get("history", []):
                    items.append({
                        **message, "id": "sensor:" + message["id"],
                        "content_type": "Message", "children": [],
                        "child_slugs": [],
                        "thread_messages": [message], "thread_complete": True,
                    })
            for event in bucket.get("events", []):
                items.append({
                    "id": f"event:{child.slug}:{event['id']}",
                    "title": event["title"], "original_text": strip_html(event.get("description", "")),
                    "date": event.get("timestamp") or event["date"] + (
                        "T" + event["time"] + ":00" if event.get("time") else ""
                    ),
                    "content_type": "Homework" if event["type"] == "homework"
                    else event.get("origin", "Calendar"),
                    "event_type": event["type"], "children": [child.name],
                    "end": event.get("end", ""), "location": event.get("location", ""),
                    "child_slugs": [child.slug], "sender": event.get("sender") or "EasyIQ",
                    "calendar_source": event.get("calendar_source", "easyiq"),
                    "calendar_kind": event.get("calendar_kind", ""),
                })
        unique = {item["id"]: item for item in items}
        ordered = sorted(unique.values(), key=date_key, reverse=True)[:300]
        _LOGGER.info("event=feed.collected item_count=%d source_error_count=%d", len(ordered), len(errors))
        for item in ordered:
            if item["content_type"] not in ("Calendar", "Weekplan", "Homework") or not item["date"]:
                continue
            zone = controller.config.timezone
            all_day = len(item["date"]) == 10
            end = item.get("end", "") or ""
            if all_day:
                end = _all_day_source_end(item["date"], end)
            else:
                # A zero-length or inverted source end is not an end; keep the entry.
                try:
                    local = ZoneInfo(zone)
                    first, last = (datetime.fromisoformat(value.replace("Z", "+00:00"))
                                   for value in (item["date"], end or item["date"]))
                    if end and "T" in end and (last.replace(tzinfo=last.tzinfo or local)
                                               <= first.replace(tzinfo=first.tzinfo or local)):
                        end = ""
                except ValueError:
                    pass
            try:
                events = validate_events([{
                    "title": item["title"], "start": item["date"], "end": end,
                    "all_day": all_day, "location": item.get("location", ""),
                    "evidence": item["title"] + "\n" + item["date"],
                    "origin": "aula", "date_rationale": "Date supplied by the source calendar.",
                }], {**item, "original_text": (
                    item["title"] + "\n" + item["date"] + "\n" + item["original_text"]
                )}, zone)
                controller.calendar.record(item, events)
            except ValueError as err:
                _LOGGER.error("Invalid source calendar event %s, skipped: %s", item["id"], describe(err))
                errors.append(f"Invalid calendar event: {item['title']}")
        cache = controller.store.get("feed_summaries", {}) or {}
        unavailable = []
        for item in ordered:
            zone = controller.config.timezone
            fingerprint = content_fingerprint(item, zone)
            item["fingerprint"] = fingerprint
            saved = cache.get(item["id"], {})
            item["analysis_pending"] = False
            item.update(
                summary="", is_translated=False, source="original", translated_title="",
                translation_pending=controller.provider is not None, translation_error="",
                analysis_error=item.get("thread_fetch_error", ""),
                title_is_translated=None, title_source_language="",
                translation_available=controller.provider is not None,
            )
            item["thread_messages"] = [{
                **message, "translated_text": "", "translation_pending": controller.provider is not None,
                "translation_error": "",
                "is_translated": None, "source_language": "",
                "translation_status": "pending" if controller.provider is not None else "original",
            } for message in item.get("thread_messages", [])]
            if saved.get("fingerprint") == fingerprint:
                item.update({k: saved[k] for k in _ENRICHMENT_FIELDS if k in saved})
                item["analysis_pending"] = bool(saved.get("translation_pending"))
            else:
                # Old 3-sentence caches are deliberately regenerated, but remain
                # readable while the richer pipeline catches up.
                legacy = hashlib.sha256((item["title"] + item["original_text"]).encode()).hexdigest()
                if saved.get("fingerprint") == legacy:
                    item.update({k: saved[k] for k in ("summary", "is_translated", "source")
                                 if k in saved})
                item["analysis_pending"] = True
            if not item.get("thread_complete", True):
                item["analysis_pending"] = False
                item["translation_pending"] = controller.provider is not None
                unavailable.append(item)
        if len(unavailable) == 1:
            errors.append(f"Full conversation unavailable for {unavailable[0]['title']}: "
                          f"{unavailable[0]['thread_fetch_error']}")
        elif unavailable:
            reasons = list(dict.fromkeys(item["thread_fetch_error"] for item in unavailable))
            errors.append(f"{len(unavailable)} conversations could not be refreshed and show their latest "
                          f"message only: {reasons[0]}" + (f" (+{len(reasons) - 1} other errors)" if len(reasons) > 1 else ""))
        self._prune_caches(ordered, cache)
        controller.store.set("feed", ordered)
        if self.retry_delay:
            errors.append(AI_WARNING)
        controller.store.set("feed_errors", errors)
        controller.store.save()
        controller.publish_feed()
        self.enrich()

    def _prune_caches(self, ordered: list[dict[str, Any]], summaries: dict[str, Any]) -> None:
        """Bound durable AI/thread caches without discarding work for current content.

        Last-seen times live in their own small map so cached entries stay
        untouched. Unreferenced entries are dropped only after
        CACHE_RETENTION_SECONDS, so content that briefly disappears from the
        source still returns without new AI calls.
        """
        store = self.controller.store
        now = int(time.time())
        threads = store.get("feed_thread_content", {}) or {}
        translations = store.get("feed_translations", {}) or {}
        seen_all = store.get("feed_cache_seen", {}) or {}
        ids = {item["id"] for item in ordered}
        thread_ids = {item["id"][len("thread:"):] for item in ordered if item["id"].startswith("thread:")}
        chunk_keys: set[str] = set()
        for item in ordered:
            texts = [item.get("title", ""), item.get("original_text", "")]
            texts.extend(message.get("original_text", "") for message in item.get("thread_messages", []) or [])
            for text in texts:
                if isinstance(text, str) and text.strip():
                    chunk_keys.update(translation_keys(text))
        updated = {}
        for name, cache, current in (("summaries", summaries, ids), ("threads", threads, thread_ids),
                                     ("translations", translations, chunk_keys)):
            seen = seen_all.get(name) if isinstance(seen_all.get(name), dict) else {}
            kept = {}
            for key in list(cache):
                stamp = seen.get(key)
                if key in current or not isinstance(stamp, (int, float)):
                    kept[key] = now
                elif now - stamp > CACHE_RETENTION_SECONDS:
                    del cache[key]
                else:
                    kept[key] = stamp
            updated[name] = kept
        store.set("feed_summaries", summaries)
        store.set("feed_thread_content", threads)
        store.set("feed_translations", translations)
        store.set("feed_cache_seen", updated)
        self._item_failures = {key: value for key, value in self._item_failures.items() if key in ids}

    @property
    def pending(self) -> bool:
        return any(self._needs_enrichment(item) for item in self.controller.store.get("feed", []))

    @staticmethod
    def _needs_enrichment(item: dict[str, Any]) -> bool:
        return bool(
            item.get("content_type") != "Presence"
            and (item.get("source") == "original" or item.get("analysis_pending"))
            and (item.get("original_text") or item.get("title"))
            and item.get("thread_complete", True)
        )

    @property
    def retry_delay(self) -> float:
        return max(0, self._retry_at - time.monotonic())

    def _before_translation(self) -> None:
        # Persist completed chunks before attempting the next request; a long
        # conversation can resume after a restart without retranslating them.
        self.controller.store.save()
        delay = max(0, AI_INTERVAL - (time.monotonic() - self._last_ai_call))
        if self.controller._stop.wait(delay):
            raise LLMError("Content enrichment interrupted; translation will resume")
        self._last_ai_call = time.monotonic()
        self._active_call_start = self._last_ai_call
        self.controller.publish_feed()

    @observed("ai.enrich")
    def enrich(self) -> None:
        priorities = list(self.controller.store.get("feed_reprocess_requests", []))
        while True:
            try:
                item_id = self._reprocess.get_nowait()
            except queue.Empty:
                break
            if item_id not in priorities:
                priorities.append(item_id)
            cache = self.controller.store.get("feed_summaries", {}) or {}
            cache.pop(item_id, None)
            self.controller.store.set("feed_summaries", cache)
            for item in self.controller.store.get("feed", []):
                if item["id"] == item_id:
                    item["analysis_pending"] = True
                    item["analysis_error"] = ""
            self._retry_at = 0
        self.controller.store.set("feed_reprocess_requests", priorities)
        if self.retry_delay:
            _LOGGER.info("event=ai.deferred retry_seconds=%.0f", self.retry_delay)
            return
        controller = self.controller
        ordered = controller.store.get("feed", [])
        cache = controller.store.get("feed_summaries", {}) or {}
        # Publish originals first, then progressively enrich recent items. Keep
        # default-AI calls bounded so a large first import doesn't block forever.
        pending = sorted(
            (item for item in ordered if self._needs_enrichment(item)),
            key=date_key, reverse=True,
        )
        _LOGGER.info("event=ai.queue pending_count=%d batch_limit=6", len(pending))
        # Items that keep failing yield to newer work so one persistently
        # rejected item cannot block the whole queue after every backoff.
        pending.sort(key=lambda item: (priorities.index(item["id"])
                                       if item["id"] in priorities else len(priorities),
                                       self._item_failures.get(item["id"], 0)))
        translations = controller.store.get("feed_translations", {}) or {}
        controller.store.set("feed_translations", translations)
        for item in pending[:6]:
            _LOGGER.info("event=ai.item phase=start item_ref=%s content_type=%s",
                         hashlib.sha256(item["id"].encode()).hexdigest()[:12],
                         item["content_type"] if item["content_type"] in
                         ("Post", "Message", "Calendar", "Weekplan", "Homework", "Presence") else "other")
            if controller._stop.is_set():
                break
            if controller.provider is not None:
                delay = max(0, AI_INTERVAL - (time.monotonic() - self._last_ai_call))
                if controller._stop.wait(delay):
                    break
                self._last_ai_call = time.monotonic()
            events = []
            recognition_warning = ""
            analysis_error = ""
            zone = controller.config.timezone
            # Event evidence may occur in any reply, not just the list preview.
            analysis_item = {**item, "original_text": "\n".join(
                message["original_text"] for message in item.get("thread_messages", [])
            ) or item["original_text"], "existing_calendar": [
                {key: event.get(key, "") for key in ("title", "start", "end", "location", "child_slugs")}
                for event in controller.calendar.snapshot_events()
                if not item.get("child_slugs") or not event.get("child_slugs")
                or set(item["child_slugs"]).intersection(event["child_slugs"])
            ]}
            analyse_events = hasattr(controller.provider, "analyse") and item["content_type"] in ("Post", "Message")
            saved = cache.get(item["id"], {})
            summary_cached = self._summary_complete(item, saved)
            if controller.provider is not None and not summary_cached:
                self._active_call_start = time.monotonic()
            if summary_cached:
                overview, translated, origin = saved["summary"], saved["is_translated"], saved["source"]
                events = saved.get("recognized_events", [])
                recognition_warning = saved.get("recognition_warning", "")
            elif analyse_events:
                try:
                    overview, translated, raw = controller.provider.analyse(
                        analysis_item, ", ".join(item["children"]) or "Family", zone
                    )
                    if len(raw) > 40:
                        recognition_warning = "More than 40 event candidates were returned; extras were omitted."
                    suggestions = []
                    for candidate in raw[:40]:
                        try:
                            if isinstance(candidate, dict) and candidate.get("issue"):
                                raise ValueError(str(candidate["issue"]))
                            events.extend(validate_events([candidate], analysis_item, zone))
                        except ValueError as err:
                            suggestions.append((candidate, err))
                    events, suggestions, dropped = gate_suggestions(analysis_item, events, suggestions, zone)
                    for reason in dropped:
                        _LOGGER.info("Calendar suggestion from %s dropped for weak evidence: %s", item["id"], reason)
                    for candidate, err in suggestions:
                        controller.calendar.recommend(analysis_item, candidate, err)
                        recognition_warning = "Some proposed events need your review before publication. Accept, correct or decline their Calendar recommendations."
                        _LOGGER.warning("Calendar candidate from %s needs review: %s", item["id"], describe(err))
                    origin = getattr(controller.provider, "last_provider", controller.provider.name)
                except (LLMError, ValueError) as err:
                    _LOGGER.error("Content analysis failed for %s: %s", item["id"], describe(err))
                    overview, translated, origin = "", False, "fallback"
                    analysis_error = str(err)
            else:
                try:
                    overview, translated, origin = summarise(
                        controller.provider, ", ".join(item["children"]) or "Family",
                        item.get("thread_messages") or [item], raise_errors=True,
                    )
                except LLMError as err:
                    _LOGGER.error("Content summary failed for %s: %s", item["id"], describe(err))
                    overview, translated, origin = "", False, "fallback"
                    analysis_error = str(err)
            if origin != "fallback" or controller.provider is None:
                item.update(
                    summary=overview, is_translated=origin != "fallback" and (
                        any(message.get("is_translated") is True
                            for message in item["thread_messages"])
                        if item.get("thread_messages") else bool(translated)
                    ),
                    source=origin, recognized_events=events, analysis_pending=True,
                    recognition_warning=recognition_warning, analysis_error=analysis_error,
                )
                cache[item["id"]] = {
                    k: item[k] for k in ("fingerprint", *_ENRICHMENT_FIELDS) if k in item
                }
                cache[item["id"]]["content_fingerprint"] = item["fingerprint"]
                cache[item["id"]]["analysis_version"] = ENRICHMENT_VERSION
                controller.store.set("feed_summaries", cache)

                if not summary_cached and controller.provider is not None:
                    self._ai_call_complete(True)

                def publish_translation(partial: dict[str, Any]) -> None:
                    item.update(partial)
                    if item.get("thread_messages"):
                        item["is_translated"] = any(
                            message.get("is_translated") is True
                            for message in item["thread_messages"]
                        )
                    controller.store.save()
                    controller.publish_feed()

                controller.store.save()
                controller.publish_feed()
                enrichment = enrich_content(
                    controller.provider, item, translations, self._before_translation,
                    publish_translation, self._ai_call_complete,
                )
                item.update(enrichment)
                controller.store.set("feed_translations", translations)
            item["analysis_error"] = analysis_error
            if origin == "fallback" and controller.provider is not None:
                self._ai_call_complete(False)
                self._item_failures[item["id"]] = self._item_failures.get(item["id"], 0) + 1
                self._failures += 1
                self._retry_at = time.monotonic() + min(900, 60 * 2 ** min(self._failures - 1, 4))
                errors = controller.store.get("feed_errors", [])
                if AI_WARNING not in errors:
                    errors.append(AI_WARNING)
                controller.store.set("feed_errors", errors)
                cache[item["id"]] = {
                    k: item[k] for k in ("fingerprint", *_ENRICHMENT_FIELDS) if k in item
                }
                cache[item["id"]]["analysis_version"] = ENRICHMENT_VERSION
                controller.store.set("feed_summaries", cache)
                _LOGGER.warning(
                    "Pausing feed AI calls for %.0fs after provider failure #%d on %s: %s",
                    self.retry_delay, self._failures, item["id"], describe(analysis_error or "AI provider unavailable"),
                )
                controller.store.save()
                controller.publish_feed()
                break
            _LOGGER.info("event=ai.item phase=complete item_ref=%s summary_cached=%s translated=%s event_count=%d translation_pending=%s",
                         hashlib.sha256(item["id"].encode()).hexdigest()[:12],
                         summary_cached, bool(translated), len(events), bool(item.get("translation_pending")))
            if not item.get("translation_pending"):
                self._failures = 0
                self._retry_at = 0
                self._item_failures.pop(item["id"], None)
            controller.store.set("feed_errors", [
                error for error in controller.store.get("feed_errors", [])
                if error != AI_WARNING
            ])
            item.update(
                summary=overview, is_translated=origin != "fallback" and (
                    any(message.get("is_translated") is True
                        for message in item["thread_messages"])
                    if item.get("thread_messages") else bool(translated)
                ),
                source=origin,
                recognized_events=events,
                analysis_pending=item.get("translation_pending", False),
                recognition_warning=recognition_warning,
            )
            if analyse_events:
                controller.calendar.record(analysis_item, events)
            if origin != "fallback" or controller.provider is None:
                cache[item["id"]] = {
                    k: item[k] for k in ("fingerprint", *_ENRICHMENT_FIELDS) if k in item
                }
                cache[item["id"]].update(
                    content_fingerprint=item["fingerprint"],
                    analysis_version=ENRICHMENT_VERSION, recognized_events=events,
                    recognition_warning=recognition_warning,
                )
            controller.store.set("feed_summaries", cache)
            controller.store.set("feed", ordered)
            if not item.get("translation_pending") and item["id"] in priorities:
                priorities.remove(item["id"])
                controller.store.set("feed_reprocess_requests", priorities)
            controller.store.save()
            controller.publish_feed()
            if item.get("translation_pending"):
                self._item_failures[item["id"]] = self._item_failures.get(item["id"], 0) + 1
                self._failures += 1
                self._retry_at = time.monotonic() + min(900, 60 * 2 ** min(self._failures - 1, 4))
                errors = controller.store.get("feed_errors", [])
                if AI_WARNING not in errors:
                    errors.append(AI_WARNING)
                controller.store.set("feed_errors", errors)
                controller.store.save()
                controller.publish_feed()
                break
        # Sources may temporarily disappear or fall outside the fetched window.
        # Keep their completed work so returning content does not cost AI calls.
        controller.store.set("feed_summaries", cache)
