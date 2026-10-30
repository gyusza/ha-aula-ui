"""Explicit, today-only guardian sickness reports using Aula's Come/Go API."""

import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .aula_api import AulaApiError


NOTICE = ("This informs daycare/SFO staff through Aula Come/Go. "
          "You may still need to notify the school teacher separately. "
          "Aula clears sickness for the next day; report again tomorrow if needed.")


class SickDays:
    def __init__(self, controller):
        self.controller = controller
        self.lock = threading.RLock()
        self.cached = None
        self.checked = 0.0

    def _today(self, now=None):
        zone = ZoneInfo(self.controller.config.timezone)
        now = (now or datetime.now(timezone.utc)).astimezone(zone)
        return now.date().isoformat(), datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone).isoformat()

    def _identities(self):
        account = self.controller.account
        connection = account.connection.value if account else None
        if not connection:
            raise AulaApiError("Reporting sickness needs the add-on's own Aula login")
        known = connection.get("children") or []
        result = {}
        for child in self.controller.config.children:
            entry = next((entry for entry in known if str(entry["profile_id"]) == child.aula_child_id), None)
            ids = sorted({str(relation["id"]) for relation in (entry or {}).get("relations", [])
                          if str(relation.get("profileId")) == child.aula_child_id
                          and str(relation.get("id", "")).isdigit()})
            result[child.slug] = ids
        return result

    def snapshot(self, fresh=False, now=None):
        with self.lock:
            today, midnight = self._today(now)
            if (not fresh and self.cached and self.cached["date"] == today
                    and time.monotonic() - self.checked < 60):
                return self.cached
            identities = self._identities()
            ids = sorted({identifier for child_ids in identities.values() for identifier in child_ids})
            if not ids:
                raise AulaApiError("No matching Aula child identities are available")
            client = self.controller.source_client
            states = client.get("?" + urlencode([
                ("method", "presence.getPresenceStates"),
                *(("institutionProfileIds[]", identifier) for identifier in ids),
            ])).get("data")
            configs = client.get("?" + urlencode([
                ("method", "presence.getPresenceConfigurationByChildIds"),
                *(("childIds[]", identifier) for identifier in ids),
            ])).get("data")
            if not isinstance(states, list) or not isinstance(configs, list):
                raise AulaApiError("Aula returned invalid sickness permissions or states")
            editable = set()
            for entry in configs:
                if not isinstance(entry, dict):
                    raise AulaApiError("Aula returned invalid sickness permissions")
                config = entry.get("presenceConfiguration") or {}
                if not isinstance(config, dict):
                    raise AulaApiError("Aula returned invalid sickness permissions")
                for dashboard in config.get("dashboardModuleSettings") or []:
                    if not isinstance(dashboard, dict):
                        raise AulaApiError("Aula returned invalid sickness permissions")
                    if dashboard.get("presenceDashboardContext") != "guardian_dashboard":
                        continue
                    if any(isinstance(module, dict) and module.get("moduleType") == "report_sick"
                           and module.get("permission") == "editable"
                           for module in dashboard.get("presenceModules") or []):
                        editable.add(str(entry.get("uniStudentId")))
            by_id = {}
            for state in states:
                if not isinstance(state, dict) or not isinstance(state.get("uniStudent") or {}, dict):
                    raise AulaApiError("Aula returned invalid sickness states")
                identifier = str((state.get("uniStudent") or {}).get("id") or state.get("uniStudentId"))
                if identifier in ids:
                    by_id[identifier] = state.get("state")
            names = {p["slug"]: p["display_name"] for p in self.controller.profiles.snapshot()}
            children = []
            for child in self.controller.config.children:
                eligible = [identifier for identifier in identities[child.slug]
                            if identifier in editable and identifier in by_id]
                sick = any(by_id[identifier] == 1 for identifier in eligible)
                children.append({
                    "slug": child.slug, "name": names.get(child.slug, child.name),
                    "can_report": bool(eligible), "sick": sick,
                    "reason": "" if eligible else "Aula does not offer guardian sickness reporting for this child.",
                })
            self.cached = {"date": today, "valid_until": midnight, "children": children, "notice": NOTICE}
            self.checked = time.monotonic()
            self._eligible = {slug: [identifier for identifier in child_ids
                                    if identifier in editable and identifier in by_id]
                              for slug, child_ids in identities.items()}
            self._states = by_id
            return self.cached

    def report(self, slug, day, sick=True):
        if not isinstance(slug, str) or not isinstance(day, str) or not isinstance(sick, bool):
            raise ValueError("Supply a child, today's date and a sickness boolean")
        with self.lock:
            today, _ = self._today()
            if day != today:
                raise ValueError("The date has changed. Reload and report sickness for today.")
            if slug not in {child.slug for child in self.controller.config.children}:
                raise LookupError("Child not found")
            before = self.snapshot(fresh=True)
            child = next(child for child in before["children"] if child["slug"] == slug)
            if not child["can_report"]:
                raise PermissionError(child["reason"])
            if self._today()[0] != day:
                raise ValueError("The date has changed. Reload and report sickness for today.")
            targets = [identifier for identifier in self._eligible[slug]
                       if (self._states[identifier] != 1 if sick else self._states[identifier] == 1)]
            if targets:
                # Never reuse a yesterday request after permissions/network calls crossed midnight.
                if self._today()[0] != day:
                    raise ValueError("The date has changed. Reload and report sickness for today.")
                payload = self.controller.source_client.post(
                    "?method=presence.updateStatusByInstitutionProfileIds",
                    {"institutionProfileIds": [int(identifier) for identifier in targets], "status": 1 if sick else 0},
                )
                self.cached = None
                if (not isinstance(payload, dict) or (payload.get("status") or {}).get("code", 0) != 0
                        or payload.get("data") is False):
                    raise AulaApiError("Aula did not accept the sickness change")
                after = self.snapshot(fresh=True)
                confirmed = next(child for child in after["children"] if child["slug"] == slug)
                verified = (confirmed["sick"] and all(self._states.get(identifier) == 1 for identifier in targets)
                            if sick else all(self._states.get(identifier) == 0 for identifier in targets))
                if after["date"] != day or not verified:
                    raise AulaApiError("Aula has not confirmed the sickness change. Check Aula before retrying.")
            if sick:
                self._record(slug, child["name"], day)
            else:
                self.controller.calendar.record({"id": f"sick:{slug}:{day}"}, [])
            self.controller.store.save()
            self.controller.publish_feed()
            return self.snapshot()

    def _record(self, slug, name, day):
        source_id = f"sick:{slug}:{day}"
        title = f"{name} sick"
        evidence = f"Sickness for {name} on {day}, confirmed by Aula."
        source = {"id": source_id, "title": title, "date": day, "child_slugs": [slug],
                  "original_text": evidence, "sender": "Parent via Aula"}
        self.controller.calendar.record(source, [{
            "title": title, "start": day,
            "end": (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat(),
            "all_day": True, "location": "", "evidence": evidence,
            "source_id": source_id, "source_title": title, "source_date": day,
            "source_author": "Parent via Aula", "child_slugs": [slug], "calendar_kind": "sickness",
            "source_content_type": "Calendar", "manually_confirmed": True,
            "description": NOTICE, "date_rationale": "Parent reported sickness for this date only.",
        }])
