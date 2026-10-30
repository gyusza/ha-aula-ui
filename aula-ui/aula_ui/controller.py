"""Main polling controller for the Aula UI add-on."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .aula_api import AulaThreadSource
from .config import AddonConfig, ChildConfig
from .events import collect_events
from .feed import Feed
from .profiles import Profiles
from .calendar_feed import CalendarFeed, event_uid
from .calendar_bridge import ensure_bridge
from .ha import HAError, HomeAssistantClient
from .direct_ai import SOURCES, configured_sources, credentials
from .llm import HomeAssistantAITaskProvider, build_provider
from .messages import extract_messages
from .state import StateStore
from .account import AulaAccount
from .account_auth import AuthenticationError
from .aula_api import AulaApiError, AulaRateLimited, fetch_threads, mark_thread_read
from .diagnostics import describe, observed, stack_locations
from .presence import snapshot as presence_snapshot
from .notifications import Notifier
from .related_links import RelatedReading
from .avatars import Avatars
from .sync_settings import SyncSettings

_LOGGER = logging.getLogger(__name__)
# AI batches publish after every step; coalesce those into at most one per window.
PUBLISH_COALESCE_SECONDS = 2.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AulaUIController:
    """Polls Aula/EasyIQ entities and publishes derived sensors."""

    def __init__(self, config: AddonConfig, client: HomeAssistantClient) -> None:
        self.client = client
        self.store = StateStore(config.data_dir)
        self.profiles = Profiles(config)
        self.sync_settings = SyncSettings(config.data_dir, config.poll_interval)
        # Discovered children and the in-app AI choice live in profiles.json.
        config = replace(config, children=self.profiles.children,
                         ai_task_entity=self.profiles.ai_task_entity,
                         ai_task_entities=self.profiles.ai_task_entities,
                         ai_providers=self.profiles.ai_providers,
                         llm_provider="ha_ai_task" if config.llm_provider != "none" else "none",
                         llm_command="")
        self.config = config
        self._settings_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self.calendar = CalendarFeed(self)
        self.calendar_bridge_error = None
        self.provider = build_provider(self._ai_config(config), client)
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self.feed_json = b'{"items":[],"errors":[],"updated_at":null}'
        self.feed = Feed(self)
        self.notifier = Notifier(self)
        self.related_reading = RelatedReading(self)
        self.avatars = Avatars(config.data_dir)
        self._cycle_errors: list[str] = []
        self._calendar_pruned_on = None
        self._batch = threading.local()
        self._publish_state_lock = threading.Lock()
        self._publish_dirty = False
        self._publish_timer: threading.Timer | None = None
        self._last_publish = float("-inf")
        self.source_states: list[dict[str, Any]] = []
        self.account = AulaAccount(self) if config.data_source == "direct" else None
        self.source_client = self.account.source if self.account else client

        self.thread_source: AulaThreadSource | None = None
        if config.data_source == "direct" or config.message_source in ("auto", "aula_api"):
            self.thread_source = AulaThreadSource(
                self.source_client, config.thread_pages, config.context_messages
            )

        if self.provider is None:
            _LOGGER.warning(
                "No LLM provider active (llm_provider=%s); using extractive summaries",
                config.llm_provider,
            )
        else:
            _LOGGER.info(
                "LLM provider '%s' active (%s)",
                self.provider.name,
                "Home Assistant's configured AI"
                if self.provider.name == "ha_ai_task"
                else f"model={getattr(self.provider, 'model', '')}",
            )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Request a graceful shutdown."""
        _LOGGER.info("Shutdown requested")
        self._stop.set()
        self._refresh.set()
        if self.account:
            self.account.login.cancel()

    def close(self) -> None:
        if self.account:
            self.account.close()

    def request_refresh(self) -> None:
        _LOGGER.info("event=sync.refresh_requested already_queued=%s", self._refresh.is_set())
        self._refresh.set()

    # ------------------------------------------------------------------ #
    # Runtime AI settings
    # ------------------------------------------------------------------ #

    def _ai_task_entities(self) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "entity_id": state["entity_id"],
                    "name": str((state.get("attributes") or {}).get("friendly_name") or state["entity_id"]),
                    "state": state.get("state"),
                    "available": state.get("state") != "unavailable",
                }
                for state in self.client.list_states()
                if isinstance(state, dict) and str(state.get("entity_id", "")).startswith("ai_task.")
            ),
            key=lambda entity: (entity["name"].casefold(), entity["entity_id"]),
        )

    def ai_settings(self) -> dict[str, Any]:
        """Selectable HA AI task entities; never includes credentials."""
        selected = self.profiles.ai_task_entity
        error = ""
        listed = True
        try:
            entities = self._ai_task_entities()
        except Exception as err:  # noqa: BLE001 - shown to the user
            _LOGGER.error("Could not list Home Assistant AI task entities: %s", err)
            entities, listed = [], False
            error = "Could not load Home Assistant AI task entities; try again."
        current = next((entity for entity in entities if entity["entity_id"] == selected), None)
        if selected and listed:
            if current is None:
                error = f"{selected} no longer exists in Home Assistant. AI requests will fail until you choose another."
            elif not current["available"]:
                error = f"{selected} is unavailable in Home Assistant. AI requests will fail until it recovers or you choose another."
        selected_entities = self.profiles.ai_task_entities
        missing = [entity_id for entity_id in selected_entities
                   if not any(entity["entity_id"] == entity_id and entity["available"] for entity in entities)]
        if listed and missing:
            error = "Missing or unavailable: " + ", ".join(missing) + ". Later entries will be tried if a request fails."
        return {
            "llm_provider": self.config.llm_provider,
            "applies": self.config.llm_provider == "ha_ai_task",
            "ai_task_entity": selected,
            "ai_task_entities": list(selected_entities),
            "default_label": "Home Assistant default",
            "entities": entities,
            "selected_available": bool(current and current["available"]) if selected and listed else None,
            "error": error,
            "fallback": {"configured": len(selected_entities) > 1, "provider": "ha_ai_task"},
            "reading_entity": self.profiles.reading_entity,
            "providers": list(self.profiles.ai_providers),
            "sources": [{**source, "stored": source["id"] in self.profiles.ai_credentials,
                         "account_id": (self._ai_config().cloudflare_account_id
                                        if source["id"] == "cloudflare" else "")}
                        for source in configured_sources(self._ai_config())],
            "active_provider": getattr(self.provider, "name", None),
            "last_provider": getattr(self.provider, "last_provider", "") or None,
        }

    def set_ai_task_entity(self, entity_id: str) -> None:
        """Persist an exact HA AI task selection and rebuild the provider live."""
        if not isinstance(entity_id, str):
            raise ValueError("ai_task_entity must be text")
        self.set_ai_task_entities([entity_id] if entity_id else [])

    def _ai_config(self, config=None):
        """Config with keys entered in the app laid over the add-on options."""
        config = config or self.config
        changes = {}
        for source, values in self.profiles.ai_credentials.items():
            if values.get("api_key"):
                changes["cloudflare_api_token" if source == "cloudflare" else f"{source}_api_key"] = values["api_key"]
            if values.get("model"):
                changes[f"{source}_model"] = values["model"]
            if source == "cloudflare" and values.get("account_id"):
                changes["cloudflare_account_id"] = values["account_id"]
        return replace(config, **changes) if changes else config

    def _rebuild_provider(self) -> None:
        self.config = replace(self.config, ai_providers=self.profiles.ai_providers,
                              ai_task_entities=self.profiles.ai_task_entities,
                              ai_task_entity=self.profiles.ai_task_entity, llm_provider="ha_ai_task")
        self.provider = build_provider(self._ai_config(), self.client)
        if hasattr(self.feed, "_retry_at"):
            self.feed._retry_at = 0

    def set_ai_providers(self, providers) -> None:
        """Save one ordered list mixing Home Assistant AI tasks and own-key providers."""
        if not isinstance(providers, list) or not all(isinstance(value, str) for value in providers):
            raise ValueError("ai_providers must be a list of provider ids")
        with self._settings_lock:
            ready = self._ai_config()
            for value in providers:
                if value in SOURCES and not credentials(ready, value):
                    raise ValueError(f"{SOURCES[value]} needs its key set up first")
            entities = [value for value in providers if value not in SOURCES]
            if any(value not in self.profiles.ai_task_entities for value in entities):
                try:
                    known = {entity["entity_id"] for entity in self._ai_task_entities()}
                except Exception as err:  # noqa: BLE001
                    raise LookupError("Could not verify Home Assistant AI task entities; try again") from err
                for value in entities:
                    if value not in known and value not in self.profiles.ai_task_entities:
                        raise ValueError(f"{value} is not an AI task entity in Home Assistant")
            self.profiles.set_ai_providers(providers)
            self._rebuild_provider()
        _LOGGER.info("event=settings.ai.providers count=%d", len(providers))
        self.request_refresh()

    def set_ai_credential(self, value) -> None:
        """Store or clear the key, model and account for one own-key provider."""
        if not isinstance(value, dict) or not isinstance(value.get("source"), str):
            raise ValueError("Supply the provider to configure")
        source = value["source"]
        if source not in SOURCES:
            raise ValueError("Unknown AI provider")
        with self._settings_lock:
            if value.get("clear") is True:
                self.profiles.set_ai_credential(source, None)
                if not credentials(self._ai_config(), source):
                    self.profiles.set_ai_providers([p for p in self.profiles.ai_providers if p != source])
            else:
                previous = self.profiles.ai_credentials.get(source)
                stored = dict(previous or {})
                for field, limit in (("api_key", 512), ("model", 120), ("account_id", 64)):
                    if field not in value:
                        continue
                    text = value[field]
                    if not isinstance(text, str) or len(text.strip()) > limit or any(c.isspace() for c in text.strip()):
                        raise ValueError(f"{field} is invalid")
                    if text.strip():
                        stored[field] = text.strip()
                    elif field != "api_key":
                        stored.pop(field, None)
                self.profiles.set_ai_credential(source, stored)
                if not credentials(self._ai_config(), source):
                    self.profiles.set_ai_credential(source, previous)
                    raise ValueError("Enter a valid key, model" + (" and Cloudflare account id" if source == "cloudflare" else ""))
            self._rebuild_provider()
        _LOGGER.info("event=settings.ai.credential source=%s cleared=%s", source, value.get("clear") is True)
        self.request_refresh()

    def set_reading_entity(self, entity_id) -> None:
        """Choose the web-search-enabled AI task used by Learn more ("" = built-in search)."""
        if not isinstance(entity_id, str):
            raise ValueError("reading_entity must be text")
        with self._settings_lock:
            if entity_id and entity_id != self.profiles.reading_entity:
                try:
                    entities = {entity["entity_id"] for entity in self._ai_task_entities()}
                except Exception as err:  # noqa: BLE001
                    raise LookupError("Could not verify Home Assistant AI task entities; try again") from err
                if entity_id not in entities:
                    raise ValueError(f"{entity_id} is not an AI task entity in Home Assistant")
            self.profiles.set_reading_entity(entity_id)

    def mark_read(self, item_id: str) -> bool:
        """Mark an unread Aula conversation read after the user opened it here."""
        if not isinstance(item_id, str) or not item_id.startswith("thread:"):
            raise ValueError("Only Aula conversations can be marked read")
        thread_id = item_id.split(":", 1)[1]
        if not thread_id.isdigit():
            raise ValueError("Invalid conversation id")
        with self._read_lock:
            item = next((entry for entry in self.store.get("feed", []) or [] if entry.get("id") == item_id), None)
            if item is None:
                raise LookupError("Conversation not found")
            if not item.get("unread"):
                return False
            changed = mark_thread_read(self.source_client, thread_id)
            for key in ("feed", "feed_threads"):
                entries = self.store.get(key, []) or []
                for entry in entries:
                    if entry.get("id") == item_id:
                        entry["unread"] = False
                self.store.set(key, entries)
            if self.thread_source is not None:
                self.thread_source.mark_read(thread_id)
        _LOGGER.info("event=aula.mark_read changed=%s", changed)
        self.publish_feed()
        return changed

    def reading_provider(self):
        entity_id = self.profiles.reading_entity
        return HomeAssistantAITaskProvider(self.client, entity_id) if entity_id else None

    def set_ai_task_entities(self, entity_ids) -> None:
        if (not isinstance(entity_ids, list) or len(entity_ids) > 20
                or any(not isinstance(entity_id, str) or not entity_id.startswith("ai_task.")
                       for entity_id in entity_ids) or len(set(entity_ids)) != len(entity_ids)):
            raise ValueError("Supply a unique ordered list of at most 20 AI task entities")
        with self._settings_lock:
            if entity_ids:
                try:
                    entities = {entity["entity_id"]: entity for entity in self._ai_task_entities()}
                except Exception as err:  # noqa: BLE001
                    raise LookupError("Could not verify Home Assistant AI task entities; try again") from err
                for entity_id in entity_ids:
                    if entity_id not in entities and entity_id not in self.profiles.ai_task_entities:
                        raise ValueError(f"{entity_id} is not an AI task entity in Home Assistant")
            self.profiles.set_ai_task_entities(entity_ids)
            self.config = replace(self.config, ai_task_entity=next(iter(entity_ids), ""),
                                  ai_task_entities=tuple(entity_ids), llm_provider="ha_ai_task",
                                  ai_providers=self.profiles.ai_providers)
            self.provider = build_provider(self._ai_config(), self.client)
            # Retry paused AI work immediately with the newly selected entity.
            if hasattr(self.feed, "_retry_at"):
                self.feed._retry_at = 0
        _LOGGER.info("event=settings.ai.priority entities=%s", ",".join(entity_ids) or "default")
        self.request_refresh()

    def publish_feed(self) -> None:
        """Rebuild the feed JSON (and ICS).

        Inside :meth:`coalesce_publishes` on the same thread, calls closer than
        ``PUBLISH_COALESCE_SECONDS`` to the previous publish are deferred to a
        trailing publish, so AI progress still reaches the UI within the window.
        Every other caller (web requests, sickness changes, tests) publishes
        immediately.
        """
        if getattr(self._batch, "depth", 0):
            with self._publish_state_lock:
                wait = self._last_publish + PUBLISH_COALESCE_SECONDS - time.monotonic()
                if wait > 0:
                    self._publish_dirty = True
                    if self._publish_timer is None:
                        self._publish_timer = threading.Timer(wait, self._flush_publish)
                        self._publish_timer.daemon = True
                        self._publish_timer.start()
                    return
        self._publish_now()

    @contextmanager
    def coalesce_publishes(self):
        """Throttle this thread's publishes; flush any deferred one on exit."""
        self._batch.depth = getattr(self._batch, "depth", 0) + 1
        try:
            yield
        finally:
            self._batch.depth -= 1
            if not self._batch.depth:
                with self._publish_state_lock:
                    if self._publish_timer is not None:
                        self._publish_timer.cancel()
                        self._publish_timer = None
                    dirty = self._publish_dirty
                if dirty:
                    self._publish_now()

    def _flush_publish(self) -> None:
        with self._publish_state_lock:
            self._publish_timer = None
            if not self._publish_dirty:
                return
        try:
            self._publish_now()
        except Exception as err:  # noqa: BLE001 - a deferred publish must not kill its timer thread
            _LOGGER.warning("Deferred feed publish failed: %s", describe(err))

    @observed("feed.publish", level=logging.DEBUG)
    def _publish_now(self) -> None:
        import json

        with self._publish_state_lock:
            # Everything marked dirty so far is included in this publish.
            self._publish_dirty = False
            self._last_publish = time.monotonic()
        self.calendar.publish()
        profiles = self.profiles.snapshot()
        names = {p["slug"]: p["display_name"] for p in profiles}
        active_events = self.calendar.snapshot_events()
        related_events = active_events + self.calendar.recommendations()
        native_sources = {
            source for event in self.calendar.snapshot_events(archived=True)
            for source in event.get("source_ids", [event["source_id"]])
            if source.startswith("event:")
        }
        items = [
            {
                **item,
                "children": [names.get(slug, slug) for slug in item.get("child_slugs", [])],
                "recognized_events": [
                    event for event in related_events
                    if item["id"] in event.get("source_ids", [event["source_id"]])
                ],
            }
            for item in self.store.get("feed", [])
            if item["id"] not in native_sources and item.get("content_type") != "Presence"
        ]
        events = active_events
        recommendations = self.calendar.recommendations()
        # Render each recognized event once as a regular Calendar post; source
        # posts remain independently readable and native feed items stay intact.
        for event in events + recommendations:
            uid = event["uid"]
            calendar_date = event["start"]
            try:
                datetime.fromisoformat(calendar_date.replace("Z", "+00:00"))
            except ValueError:
                calendar_date = event["source_date"]
            if event["source_id"].startswith("event:") and not event.get("review_required"):
                native = next((item for item in items if item["id"] == event["source_id"]), None)
                if native is not None:
                    native["calendar_event"] = event
                    native["title"] = event["title"]
                    native["date"] = event["start"]
                    native["summary"] = (
                        f"{event['start']}{' – ' + event['end'] if event['end'] else ''}"
                        f"{' · All day' if event['all_day'] else ''}"
                        f"{' · ' + event['location'] if event['location'] else ''}"
                        f"{chr(10) + event['description'] if event.get('description') else ''}"
                    )
                    continue
            items.append({
                "id": "calendar:" + uid, "title": event["title"],
                "date": calendar_date,
                "content_type": event.get("source_content_type") or "Calendar", "calendar_event": event,
                "children": [names.get(slug, slug) for slug in event["child_slugs"]],
                "child_slugs": event["child_slugs"],
                "sender": event.get("source_author") or (
                    "Aula calendar" if event.get("origin") == "aula" else "AI calendar suggestion"),
                "summary": (
                    f"Recommendation: {event.get('recommendation', '')}\n"
                    f"Issue: {event.get('issue', '')}" if event.get("review_required") else
                    f"{event['start']}{' – ' + event['end'] if event['end'] else ''}"
                    f"{' · All day' if event['all_day'] else ''}"
                    f"{' · ' + event['location'] if event['location'] else ''}"
                    f"{chr(10) + event['description'] if event.get('description') else ''}"
                ),
                "original_text": event["evidence"], "source": "calendar",
                "is_translated": False, "recognized_events": [],
            })
        from .feed import date_key
        items.sort(key=date_key, reverse=True)
        presence = presence_snapshot(self)
        self.feed_json = json.dumps({
            "items": items,
            "ai_progress": self.feed.ai_progress,
            "presence": presence,
            "errors": list(dict.fromkeys(self._cycle_errors + self.store.get("feed_errors", []))),
            "updated_at": _now(),
            "children": [{"name": p["display_name"], "slug": p["slug"],
                          "institution_type": p.get("institution_type", "school"),
                          "avatar": self.avatars.version(p["slug"])} for p in profiles],
            "authors": self.avatars.authors(),
        }, ensure_ascii=False).encode("utf-8")
        try:
            self.notifier.observe([item for item in items if item.get("content_type") != "Album"])
            self.notifier.observe_naps(presence)
        except Exception as err:  # noqa: BLE001 - notifications must never break the feed
            _LOGGER.error("Notification check failed; the feed is unaffected: %s", describe(err))

    def run_forever(self) -> None:
        """Publish the persisted feed, then poll until stopped."""
        # Everything on this thread is automatic sync and counts against the background budget.
        with self.account.source.background() if self.account else nullcontext():
            self._run_loop()

    def _run_loop(self) -> None:
        self.publish_feed()
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_cycle()
            except Exception:  # noqa: BLE001 - the loop must never die
                _LOGGER.exception("Unhandled error during poll cycle")
                self._publish_status("error")
            elapsed = time.monotonic() - started
            wait = max(1.0, self.sync_settings.full_sync_seconds - elapsed)
            _LOGGER.debug("Cycle took %.1fs, sleeping %.0fs", elapsed, wait)
            deadline = time.monotonic() + wait
            next_quick = time.monotonic() + self.sync_settings.quick_seconds
            enrich_failed = False
            while not self._stop.is_set() and not self._refresh.is_set():
                now = time.monotonic()
                if now >= deadline:
                    break
                if now >= next_quick:
                    try:
                        self.quick_check()
                    except Exception:  # noqa: BLE001 - quick checks must never stop syncing
                        _LOGGER.exception("Quick check failed")
                    next_quick = time.monotonic() + self.sync_settings.quick_seconds
                    continue
                if self.feed.pending and not enrich_failed and not self.feed.retry_delay:
                    try:
                        with self.coalesce_publishes():
                            self.feed.enrich()
                        self.store.save()
                        errors = self._cycle_errors + self.store.get("feed_errors", [])
                        self._publish_status("error" if errors else "ok", errors)
                    except Exception:
                        _LOGGER.exception("Feed enrichment failed")
                        self._publish_status("error", ["Feed enrichment failed"])
                        enrich_failed = True
                    continue
                timeout = min(deadline, next_quick) - now
                if self.feed.pending and not enrich_failed:
                    timeout = min(timeout, self.feed.retry_delay)
                self._refresh.wait(max(0, timeout))
            self._refresh.clear()
        _LOGGER.info("Polling loop stopped")

    # Between full syncs, messages and check-in/nap state are checked with a
    # single cheap Aula request each (Settings → Sync controls how often and when).
    QUICK_RESERVE = 40                  # background requests kept free for full syncs

    def _quick_due(self, kind: str) -> bool:
        settings = self.sync_settings.values()
        if not settings[f"quick_{kind}"]:
            return False
        local = datetime.now(ZoneInfo(self.config.timezone))
        if kind == "presence" and settings["presence_weekdays_only"] and local.weekday() >= 5:
            return False
        return settings[f"{kind}_from_hour"] <= local.hour < settings[f"{kind}_to_hour"]

    def quick_check(self) -> None:
        """Near real-time check for new messages and check-in/nap changes.

        A new or changed conversation triggers a full sync immediately; a
        presence change is published straight away. Every Aula guardrail
        (pauses, budget, spacing) still applies, and failures stay quiet until
        the next full sync reports them.
        """
        if not self.account or not self.account.connection.value:
            return
        source = self.account.source
        if source.rate_limited() or source.background_left() < self.QUICK_RESERVE:
            return
        if self._quick_due("presence") and not source.rate_limited("presence.getDailyOverview"):
            try:
                if self.account.refresh_presence():
                    _LOGGER.info("event=quick.presence_changed")
                self.source_states = self.account.presence_states
                self.store.save()
                # Republish even when unchanged so "updated" times stay current.
                self.publish_feed()
            except (AulaApiError, AuthenticationError) as err:
                _LOGGER.debug("Quick presence check skipped: %s", describe(err))
        thread_source = self.thread_source
        if thread_source is None or not thread_source.available:
            return
        if not self._quick_due("messages") or source.rate_limited("messaging.getThreads"):
            return
        try:
            newest = fetch_threads(self.source_client, 1)
        except (AulaApiError, AuthenticationError) as err:
            _LOGGER.debug("Quick message check skipped: %s", describe(err))
            return

        def signature(threads):
            return [(str(thread.get("id")), str((thread.get("latestMessage") or {}).get("id")),
                     bool(thread.get("read", True))) for thread in threads]

        known = thread_source.threads
        if signature(newest) != signature(known[:len(newest)]):
            _LOGGER.info("event=quick.messages_changed")
            self.request_refresh()

    # ------------------------------------------------------------------ #
    # Poll cycle
    # ------------------------------------------------------------------ #

    @observed("sync.cycle")
    def poll_cycle(self) -> None:
        _LOGGER.debug("Starting poll cycle")
        if self.config.data_dir == "/data":
            self.calendar_bridge_error = ensure_bridge(self.client)
        if not self.account:
            # Legacy integration mode reads EasyIQ presence from HA states.
            self.source_states = self._safe_list_states()

        direct_errors: list[str] = []
        if self.account:
            try:
                direct_errors = self.account.collect()
            except Exception as err:
                # Unknown library errors may echo login material, so only known errors are detailed.
                detail = describe(err) if isinstance(err, (AulaApiError, AuthenticationError)) else (
                    f"{type(err).__name__} at {stack_locations(err.__traceback__)}")
                # A rate-limit pause is expected operational state, not a failure.
                _LOGGER.log(logging.WARNING if isinstance(err, AulaRateLimited) else logging.ERROR,
                            "Direct Aula sync failed; cached content retained: %s", detail)
                message = str(err) if isinstance(err, (AulaApiError, AuthenticationError)) else (
                    "Direct Aula sync failed; check the add-on logs and Account login.")
                self.account.errors = [message]
                self.store.set("feed_errors", [message])
                self._cycle_errors = [message]
                self.publish_feed()
                self._publish_status("error", [message])
                return
            self.source_states = self.account.presence_states

        if self.thread_source is not None:
            if not self.thread_source.refresh():
                if self.account:
                    _LOGGER.warning("Direct Aula messages unavailable; retaining cached messages")
                elif self.config.message_source == "auto":
                    _LOGGER.info(
                        "Aula API unavailable this cycle; falling back to the message sensors"
                    )

        errors: list[str] = list(direct_errors)
        for child in self.config.children:
            for kind, process in (("messages", self.process_messages), ("events", self.process_events)):
                try:
                    process(child)
                except AulaRateLimited as err:
                    # Cached data is kept; the pause itself was already logged where it arose.
                    _LOGGER.warning("%s processing paused for %s: %s", kind.capitalize(), child.slug, describe(err))
                    errors.append(f"{child.slug}/{kind}: {err}")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("%s processing failed for %s", "Message" if kind == "messages" else "Event",
                                      child.slug)
                    errors.append(f"{child.slug}/{kind}: {err}")

        self._cycle_errors = list(errors)
        try:
            with self.coalesce_publishes():
                self.feed.update()
            if direct_errors:
                self.store.set("feed_errors", direct_errors + self.store.get("feed_errors", []))
                self.publish_feed()
            errors.extend(self.store.get("feed_errors", []))
        except Exception as err:
            _LOGGER.exception("Feed collection failed")
            errors.append(f"feed: {err}")
            self._cycle_errors.append(f"feed: {err}")
        try:
            if self.avatars.refresh_authors():
                self.publish_feed()
        except Exception as err:  # noqa: BLE001 - pictures must never break a sync
            _LOGGER.warning("Author pictures could not be refreshed: %s", describe(err))
        self.prune_calendar_state()
        self.store.save()
        if self.account:
            self.account.synced(errors)
        self._publish_status("error" if errors else "ok", errors)
        _LOGGER.debug("Poll cycle complete")

    def prune_calendar_state(self) -> None:
        """Daily, drop archive flags/reviews of long-past events no longer recognized."""
        today = datetime.now(ZoneInfo(self.config.timezone)).date()
        if self._calendar_pruned_on == today:
            return
        try:
            with self.calendar.lock:
                groups = {source: group for source, group in self.calendar.events.items() if group}
                if not groups:
                    # Nothing recognized yet (e.g. first sync): nothing to compare against.
                    return
                known = {event_uid(event) for group in groups.values() for event in group}
                sources = set(groups) | {item.get("id") for item in self.store.get("feed", []) if item.get("id")}
                archived, reviews = self.profiles.prune_calendar_state(known, sources, today)
            self._calendar_pruned_on = today
            if archived or reviews:
                _LOGGER.info("event=calendar.state_pruned archived=%d reviews=%d", archived, reviews)
        except Exception as err:  # noqa: BLE001 - housekeeping must never break a sync
            _LOGGER.warning("Calendar state pruning failed: %s", describe(err))

    def _safe_list_states(self) -> list[dict[str, Any]]:
        try:
            return self.client.list_states()
        except HAError as err:
            _LOGGER.error("Could not list Home Assistant states: %s", err)
            return []

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #

    def _child_messages(self, child: ChildConfig) -> tuple[list[dict[str, Any]], str]:
        """Return ``(messages, source)`` for a child, newest first.

        Prefers the Aula API, which attributes every thread to specific
        children via ``regardingChildren``. The message sensors are only a
        fallback: they expose a single globally-latest message with no child
        attribution, so every child would otherwise show the same thread.
        """
        if self.thread_source is not None and self.thread_source.available:
            messages = self.thread_source.messages_for(child)
            return messages, "aula_api"

        if self.account or self.config.message_source == "aula_api":
            return [], "aula_api"

        state = self.client.get_state(child.messages_sensor)
        if state is None:
            _LOGGER.warning(
                "Messages sensor %s does not exist (child %s)",
                child.messages_sensor,
                child.slug,
            )
            return [], "sensor"
        return extract_messages(state), "sensor"

    def process_messages(self, child: ChildConfig) -> None:
        messages, origin = self._child_messages(child)
        if not messages:
            _LOGGER.debug("No messages available for %s", child.slug)
            return

        latest = messages[0]
        bucket = self.store.child(child.slug)
        previous = bucket.get("last_message") or {}
        is_new = previous.get("id") != latest["id"]

        if is_new:
            _LOGGER.info(
                "event=message.new child=%s source=%s",
                child.slug,
                origin,
            )
            bucket["last_message"] = latest
            self.store.push_history(child.slug, latest)
        else:
            _LOGGER.debug("No new message for %s", child.slug)

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def sync_due(self, kind: str, key: str = "") -> bool:
        """Slow-changing Aula data is refreshed less often than messages (Settings → Sync)."""
        stamp = (self.store.get("sync_stamps") or {}).get(f"{kind}:{key}")
        return (not isinstance(stamp, (int, float))
                or time.time() - stamp >= self.sync_settings.refresh_seconds(kind))

    def set_sync_settings(self, value: Any) -> dict[str, Any]:
        self.sync_settings.update(value)
        _LOGGER.info("event=sync.settings_saved %s", " ".join(
            f"{key}={item}" for key, item in sorted(self.sync_settings.values().items())))
        # Wake the loop so a shorter interval applies now instead of after the old one.
        self.request_refresh()
        return self.sync_settings.snapshot()

    def mark_synced(self, kind: str, key: str = "") -> None:
        stamps = dict(self.store.get("sync_stamps") or {})
        stamps[f"{kind}:{key}"] = time.time()
        self.store.set("sync_stamps", stamps)

    def process_events(self, child: ChildConfig) -> None:
        if self.account:
            bucket = self.store.child(child.slug)
            native_key = "native_calendar:" + child.slug
            mapped = next((entry for entry in (self.account.connection.value or {}).get("children", [])
                           if str(entry["profile_id"]) == str(child.aula_child_id)), None)
            failure = None
            if mapped and (self.store.get(native_key) is None or self.sync_due("calendar", child.slug)):
                try:
                    native = self.account.source.native_events(mapped, self.config.event_lookahead_days)
                    current_ids = {event["id"] for event in native}
                    for previous in self.store.get(native_key, []):
                        if previous["id"] not in current_ids:
                            self.calendar.record({"id": f"event:{child.slug}:{previous['id']}"}, [])
                    self.store.set(native_key, native)
                    self.mark_synced("calendar", child.slug)
                except (AulaApiError, AuthenticationError) as err:
                    failure = err
            events = [event for event in bucket.get("events", [])
                      if event.get("calendar_source") != "aula"]
            native = self.store.get(native_key, [])
            lessons = {
                (event["title"].casefold(), datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")))
                for event in native if event.get("calendar_kind") == "lesson"
            }
            zone = self.config.timezone
            for event in events:
                if event.get("origin") == "Weekplan" and event.get("timestamp"):
                    first = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                    first = first.replace(tzinfo=first.tzinfo or ZoneInfo(zone))
                    event["calendar_kind"] = "lesson" if (event["title"].casefold(), first) in lessons else ""
            events += native
            bucket["events"] = events
            if failure:
                raise failure
            return
        weekplan_state = (
            self.client.get_state(child.weekplan_sensor) if child.weekplan_sensor else None
        )
        events = collect_events(
            self.client,
            child.calendars,
            weekplan_state,
            lookahead_days=self.config.event_lookahead_days,
        )
        bucket = self.store.child(child.slug)
        bucket["events"] = events

    def _publish_status(self, status: str, errors: list[str] | None = None) -> None:
        _LOGGER.info("event=sync.status status=%s error_count=%d feed_count=%d",
                     status, len(errors or []), len(self.store.get("feed", [])))
