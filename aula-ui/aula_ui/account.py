"""Own Aula connection; Home Assistant is used only for UI, AI and output sensors."""

import math
import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .account_auth import AuthenticationError, Connection, LoginFlow, token_state
from .auth_storage import Vault
from .config import ChildConfig
from .direct_source import DirectAulaClient
from .events import make_event_id
from .aula_api import AulaApiError
from .diagnostics import observed

_LOGGER = logging.getLogger(__name__)

class AulaAccount:
    def __init__(self, controller):
        self.controller = controller
        self.vault = Vault(Path(controller.config.data_dir) / "aula_auth")
        self.connection = Connection(self.vault)
        self.source = DirectAulaClient(self.connection)
        self.source.restore_rate_state(controller.store.get("aula_rate_limit"))
        self.source.on_rate_state = self._save_rate_state
        self.source.restore_version(controller.store.get("aula_api_version"))
        self.source.on_version = self._save_version
        self.lock = threading.RLock()
        self.login = LoginFlow(self)
        self.presence_states = []
        self.last_sync = controller.store.get("direct_last_sync")
        self.errors = []
        try:
            if self.connection.value:
                self._apply_children(self.connection.value["children"])
        except Exception:
            self.vault.close()
            raise

    def _save_rate_state(self, state):
        self.controller.store.set("aula_rate_limit", state)
        self.controller.store.save()

    def _save_version(self, version):
        self.controller.store.set("aula_api_version", version)
        self.controller.store.save()

    def _mapped_children(self, discovered):
        previous = self.connection.value["children"] if self.connection.value else []
        expected = {entry["profile_id"] for entry in previous}
        if expected - {entry["profile_id"] for entry in discovered}:
            raise AuthenticationError(
                "Guardian child access changed. Existing data was retained; use a separate add-on data backup "
                "and restore access to the same guardian before reconnecting.")
        configured = self.controller.config.children
        mapped = []
        used = set()
        for raw in discovered:
            profile_id = raw["profile_id"]
            saved = next((entry for entry in previous if entry["profile_id"] == profile_id), None)
            exact = [child for child in configured if child.aula_child_id == profile_id]
            if not exact:
                exact = [child for child in configured if not child.aula_child_id and
                         (child.aula_name or child.name).strip().casefold() == raw["name"].strip().casefold()]
            if not exact:
                # Support pre-login options containing only a unique first name.
                exact = [child for child in configured if not child.aula_child_id and not child.aula_name and
                         child.name.strip().casefold() == raw["name"].split()[0].casefold()]
            if len(exact) > 1:
                raise AuthenticationError("Multiple configured children match one Aula identity. Set unique aula_child_id values.")
            match = exact[0] if exact else None
            slug = saved.get("slug") if saved else match.slug if match else "child_" + profile_id
            if not slug or slug in used:
                raise AuthenticationError("Child slug mapping is ambiguous; check add-on options.")
            used.add(slug)
            mapped.append({**raw, "slug": slug, "display_name": (saved or {}).get("display_name") or
                           (match.name if match else raw["name"]),
                           "institution_type": (saved or {}).get("institution_type") or
                           (match.institution_type if match else "school")})
        if {child.slug for child in configured} - used:
            raise AuthenticationError(
                "This login does not match every configured child. Choose the correct guardian identity "
                "or correct the children's Aula names/IDs; cached family data has not been replaced.")
        return mapped

    def _apply_children(self, children):
        current = {child.slug: child for child in self.controller.config.children}
        resolved = tuple(
            replace(current[entry["slug"]], aula_child_id=entry["profile_id"], aula_name=entry["name"])
            if entry["slug"] in current else ChildConfig(
                name=entry.get("display_name", entry["name"]), slug=entry["slug"], messages_sensor="",
                aula_name=entry["name"], aula_child_id=entry["profile_id"],
                institution_type=entry.get("institution_type", "school"),
            )
            for entry in children
        )
        self.controller.config = replace(self.controller.config, children=resolved)
        self.controller.profiles.children = resolved

    @observed("account.connect")
    def connect(self, username, tokens, cancelled=None):
        class PendingConnection:
            value = {"username": username}

            def access_token(self, force=False):
                # A just-issued login cannot be refreshed here; a retry reuses it once.
                return token_state(tokens)["access_token"]

        temporary = DirectAulaClient(PendingConnection())
        temporary.version = self.source.version
        temporary.on_version = self._save_version
        try:
            discovered = temporary.discover()
        finally:
            temporary.session.close()
        self.source.restore_version(max(self.source.version, temporary.version))
        with self.lock:
            if cancelled is not None and cancelled.is_set():
                raise AuthenticationError("Login cancelled before account discovery completed.")
            if self.connection.value and self.connection.value["username"] != username:
                raise AuthenticationError("Reconnect the same guardian account to preserve this family's private data.")
            mapped = self._mapped_children(discovered)
            self.connection.install(username, tokens, mapped)
            self._apply_children(mapped)
            self.last_sync = None
            self.errors = []
            self.controller.request_refresh()

    @observed("source.collect")
    def collect(self):
        with self.lock:
            if not self.connection.value:
                raise AuthenticationError("Connect your Aula account using Account login in the feed. Cached items remain readable.")
            mapped = self._mapped_children(self.source.discover())
            self.connection.install(self.connection.value["username"], self.connection.value["tokens"], mapped)
            self._apply_children(mapped)
            profiles = {profile["slug"]: profile for profile in self.controller.profiles.snapshot()}
            errors = []
            pictures = self._read_presence(mapped, errors)
            for child in mapped:
                slug = child["slug"]
                _LOGGER.info("event=child.collect child=%s institution_type=%s",
                             slug, profiles[slug]["institution_type"])
                avatars = getattr(self.controller, "avatars", None)
                if avatars is not None:
                    avatars.refresh(slug, list(child.get("relations", [])) + [pictures.get(slug, "")])
                if profiles[slug]["institution_type"] == "daycare":
                    _LOGGER.info("event=planning.skipped child=%s reason=daycare", slug)
                    self.controller.store.set("direct_planning:" + slug, {
                        "status": "not_applicable", "message": "EasyIQ school planning skipped for daycare.",
                    })
                    continue
                if (self.controller.store.get("direct_planning:" + slug) or {}).get("status") == "ok" and \
                        not self.controller.sync_due("planning", slug):
                    continue
                try:
                    events = self.source.easyiq_events(
                        child, weeks=max(1, math.ceil(self.controller.config.event_lookahead_days / 7) + 1))
                    if events is None:
                        _LOGGER.info("event=planning.unavailable child=%s reason=widget_not_offered cached=true", slug)
                        self.controller.store.set("direct_planning:" + slug, {
                            "status": "not_available",
                            "message": "Aula does not offer an EasyIQ planning widget for this guardian account. Cached dates are retained.",
                        })
                        continue
                    # Preserve legacy event identities, archived UIDs and existing dashboard cards.
                    for event in events:
                        event["id"] = make_event_id(event["title"], event["date"])
                    self.controller.store.child(slug)["events"] = events
                    _LOGGER.info("event=planning.collected child=%s count=%d", slug, len(events))
                    self.controller.store.set("direct_planning:" + slug, {
                        "status": "ok", "message": f"EasyIQ synced {len(events)} planning entries.",
                    })
                    self.controller.mark_synced("planning", slug)
                except (AulaApiError, AuthenticationError) as err:
                    _LOGGER.warning("event=planning.failed child=%s error_type=%s cached=true",
                                    slug, type(err).__name__)
                    self.controller.store.set("direct_planning:" + slug, {
                        "status": "error", "message": str(err),
                    })
                    errors.append(f"{child['display_name']} / EasyIQ: {err}")
            self.errors = errors
            return errors

    def _read_presence(self, mapped, errors):
        """Read every child's check-in/nap state with one Aula request; returns picture URLs by slug."""
        store = self.controller.store
        pictures = {}
        try:
            result = self.source.presence_many(mapped)
            checked = datetime.now(timezone.utc).isoformat()
            for child in mapped:
                presence = result.get(child["profile_id"])
                pictures[child["slug"]] = presence.pop("picture_url", "") if presence else ""
                # A successful missing entry means the child has no presence today.
                store.set("direct_presence:" + child["slug"], presence)
                store.set("presence_sync:" + child["slug"], {"checked_at": checked, "failed": False})
        except (AulaApiError, AuthenticationError) as err:
            for child in mapped:
                sync = store.get("presence_sync:" + child["slug"]) or {}
                store.set("presence_sync:" + child["slug"], {**sync, "failed": True})
                _LOGGER.warning("event=presence.failed child=%s error_type=%s cached=true",
                                child["slug"], type(err).__name__)
                errors.append(f"{child['display_name']} / presence: {err}")
        states = []
        for child in mapped:
            presence = store.get("direct_presence:" + child["slug"])
            if presence:
                states.append({
                    "entity_id": f"binary_sensor.easyiq_{child['slug']}_present", "child_slug": child["slug"],
                    "state": presence["status"], "attributes": {
                        "status_description": presence["status"], "exit_with": presence["exit_with"],
                    }, "last_changed": presence["date"],
                })
        self.presence_states = states
        return pictures

    def refresh_presence(self):
        """Quick check between full syncs: re-read check-in and nap state only.

        Returns True when anything visible changed. Errors are kept quiet here;
        the next full sync reports them.
        """
        with self.lock:
            if not self.connection.value:
                return False
            mapped = self.connection.value.get("children") or []
            if not mapped:
                return False
            store = self.controller.store
            before = [store.get("direct_presence:" + child["slug"]) for child in mapped]
            failed = [(store.get("presence_sync:" + child["slug"]) or {}).get("failed") for child in mapped]
            errors = []
            self._read_presence(mapped, errors)
            after = [store.get("direct_presence:" + child["slug"]) for child in mapped]
            now_failed = [(store.get("presence_sync:" + child["slug"]) or {}).get("failed") for child in mapped]
            return before != after or failed != now_failed

    def synced(self, errors):
        from .controller import _now
        from .feed import AI_WARNING
        self.errors = list(dict.fromkeys(error for error in errors if error != AI_WARNING))
        _LOGGER.info("event=source.sync_result outcome=%s error_count=%d",
                     "partial" if self.errors else "ok", len(self.errors))
        if not self.errors:
            self.last_sync = _now()
            self.controller.store.set("direct_last_sync", self.last_sync)
            self.controller.store.save()

    def snapshot(self):
        return {
            "mode": "direct", "connected": bool(self.connection.value),
            "username": self.connection.value["username"] if self.connection.value else "",
            "children": self.controller.profiles.snapshot(),
            "last_sync": self.last_sync, "errors": list(self.errors),
            "planning": {child.slug: self.controller.store.get("direct_planning:" + child.slug)
                         for child in self.controller.config.children},
            "message": self.connection.error or (
                "Own Aula login active. Integration removal is safe after a successful direct sync."
                if self.last_sync else "Approve your own Aula login, then wait for the first successful direct sync."),
            "login": self.login.snapshot(),
        }

    def close(self):
        self.login.cancel()
        if self.login.worker:
            self.login.worker.join(timeout=35)
            if self.login.worker.is_alive():
                raise RuntimeError("MitID request still running; data lock cannot be released.")
        self.source.session.close()
        self.connection.session.close()
        self.vault.close()
