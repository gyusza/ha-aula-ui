"""Date-specific guardian pickup plans, separate from actual check-out status."""

import threading
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .aula_api import AulaApiError


def _time(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise AulaApiError("Aula returned an invalid pickup time")
    try:
        return datetime.strptime(value, "%H:%M:%S" if len(value) == 8 else "%H:%M").strftime("%H:%M")
    except ValueError as error:
        raise AulaApiError("Aula returned an invalid pickup time") from error


class PickupPlans:
    def __init__(self, controller):
        self.controller = controller
        self.lock = threading.RLock()

    def _date(self, day):
        if not isinstance(day, str):
            raise ValueError("Supply a date as YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(day)
        except ValueError as error:
            raise ValueError("Supply a date as YYYY-MM-DD") from error
        today = datetime.now(ZoneInfo(self.controller.config.timezone)).date()
        if parsed.isoformat() != day or not today <= parsed <= today + timedelta(days=90):
            raise ValueError("Pickup date must be today or within the next 90 days")
        return parsed

    def _read(self, slug, day, institution=None):
        self._date(day)
        child = next((child for child in self.controller.config.children if child.slug == slug), None)
        if child is None:
            raise LookupError("Child not found")
        client = self.controller.source_client
        if not callable(getattr(client, "guardian_profiles", None)):
            raise AulaApiError("Pickup planning needs the add-on's own Aula login")
        ids = sorted({str(relation["id"]) for profile in client.guardian_profiles()
                      for relation in profile.get("children") or []
                      if str(relation.get("profileId")) == str(child.aula_child_id)
                      and str(relation.get("id", "")).isdigit()})
        if not ids:
            raise PermissionError("No guardian pickup identity is available for this child")
        configs = client.get("?" + urlencode([
            ("method", "presence.getPresenceConfigurationByChildIds"),
            *(("childIds[]", identifier) for identifier in ids),
        ])).get("data")
        if not isinstance(configs, list):
            raise AulaApiError("Aula returned invalid pickup permissions")
        permissions = {}
        for entry in configs:
            if not isinstance(entry, dict) or not isinstance(entry.get("presenceConfiguration"), dict):
                raise AulaApiError("Aula returned invalid pickup permissions")
            config = entry["presenceConfiguration"]
            modules = {}
            for dashboard in config.get("dashboardModuleSettings") or []:
                if not isinstance(dashboard, dict):
                    raise AulaApiError("Aula returned invalid guardian pickup permissions")
                if dashboard.get("presenceDashboardContext") == "guardian_dashboard":
                    for module in dashboard.get("presenceModules") or []:
                        if not isinstance(module, dict):
                            raise AulaApiError("Aula returned invalid guardian pickup permissions")
                        modules[module.get("moduleType")] = module.get("permission")
            permissions[str(entry.get("uniStudentId"))] = (
                config.get("pickup") is True
                and modules.get("pickup_times") == "editable"
                and modules.get("pickup_type") == "editable")
        data = client.get("?" + urlencode([
            ("method", "presence.getPresenceTemplates"),
            *(("filterInstitutionProfileIds[]", identifier) for identifier in ids),
            ("fromDate", day), ("toDate", day),
        ])).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("presenceWeekTemplates"), list):
            raise AulaApiError("Aula returned invalid pickup templates")
        rows = {}
        for week in data["presenceWeekTemplates"]:
            if not isinstance(week, dict) or not isinstance(week.get("institutionProfile"), dict) \
                    or not isinstance(week.get("dayTemplates"), list):
                raise AulaApiError("Aula returned invalid pickup templates")
            identifier = str(week["institutionProfile"].get("id"))
            if identifier not in ids or institution and identifier != institution:
                continue
            for template in week["dayTemplates"]:
                if not isinstance(template, dict) or not isinstance(template.get("byDate"), str):
                    raise AulaApiError("Aula returned invalid pickup date")
                if template["byDate"][:10] == day:
                    if identifier in rows and rows[identifier]["template"] != template:
                        raise AulaApiError("Aula returned ambiguous pickup templates")
                    rows[identifier] = {"id": identifier, "template": template,
                                        "can_edit": permissions.get(identifier, False)}
        if not rows:
            raise AulaApiError("Aula has no pickup planning template for this child and date")
        return child, list(rows.values())

    def _public(self, child, day, rows):
        return {"child": child.slug, "name": child.name, "date": day,
                "timezone": self.controller.config.timezone,
                "plans": [{"institution_profile_id": row["id"], "can_edit": row["can_edit"],
                           "reason": "" if row["can_edit"] else "Aula does not allow guardian editing of both pickup time and person.",
                           "is_on_vacation": bool(row["template"].get("isOnVacation")),
                           "is_closed": bool(row["template"].get("isClosed")),
                           "pickup_time": _time(row["template"].get("exitTime")),
                           "pickup_person": row["template"].get("exitWith"),
                           "activity_type": row["template"].get("activityType"),
                           "arrival_time": _time(row["template"].get("entryTime")),
                           "note": row["template"].get("comment")}
                          for row in rows],
                "notice": "This is a planned pickup, not an actual check-out. Specify the pickup person's name; do not infer who 'I' means."}

    def get(self, slug, day):
        with self.lock:
            child, rows = self._read(slug, day)
            result = self._public(child, day, rows)
            names = self.controller.source_client.get("?" + urlencode([
                ("method", "presence.getPickupResponsibles"),
                *(("uniStudentIds[]", row["id"]) for row in rows),
            ])).get("data")
            if not isinstance(names, list):
                raise AulaApiError("Aula returned invalid pickup people")
            result["pickup_people"] = names
            return result

    def set(self, slug, day, pickup_time, person, confirmed=False, institution=None):
        self._date(day)
        if confirmed is not True:
            raise ValueError("Confirm the child, date, pickup time and person's name with the user before saving")
        if not isinstance(pickup_time, str) or len(pickup_time) != 5:
            raise ValueError("Pickup time must be HH:MM in the child's local timezone")
        try:
            parsed_time = datetime.strptime(pickup_time, "%H:%M").time()
        except ValueError as error:
            raise ValueError("Pickup time must be HH:MM") from error
        if parsed_time.strftime("%H:%M") != pickup_time:
            raise ValueError("Pickup time must be HH:MM")
        if not isinstance(person, str) or not 2 <= len(person.strip()) <= 100 \
                or any(ord(char) < 32 for char in person) or person.strip().casefold() in {"i", "me", "myself"}:
            raise ValueError("Supply an explicit pickup person's name, not 'I' or 'me'")
        person = person.strip()
        if institution is not None and (not isinstance(institution, str) or not institution.isdigit()):
            raise ValueError("Supply an institution_profile_id from get_pickup_plan")
        with self.lock:
            child, rows = self._read(slug, day, institution)
            editable = [row for row in rows if row["can_edit"]]
            if len(editable) != 1:
                if not editable:
                    raise PermissionError("Aula does not allow editing pickup time and person for this child")
                raise ValueError("Multiple institutions allow pickup planning; choose institution_profile_id from get_pickup_plan")
            row = editable[0]
            template = row["template"]
            if template.get("comment") is not None and not isinstance(template["comment"], str):
                raise AulaApiError("Aula returned an unreadable note; no pickup change was made")
            if template.get("isOnVacation") or template.get("isClosed"):
                raise ValueError("The child is absent or the institution is closed on this date")
            arrival = _time(template.get("entryTime"))
            if arrival and pickup_time <= arrival:
                raise ValueError("Pickup must be after the existing arrival time")
            now = datetime.now(ZoneInfo(self.controller.config.timezone))
            if datetime.combine(self._date(day), parsed_time, tzinfo=now.tzinfo) <= now:
                raise ValueError("Pickup time must be in the future")
            payload = {
                "institutionProfileId": int(row["id"]), "byDate": day,
                "presenceActivity": {"activityType": 0, "pickup": {
                    "entryTime": arrival, "exitTime": pickup_time, "exitWith": person}},
                "comment": template.get("comment"), "repeatPattern": "never", "expiresAt": None,
            }
            spare = template.get("spareTimeActivity")
            if spare is not None:
                if not isinstance(spare, dict) or not all(key in spare for key in ("startTime", "endTime", "comment")):
                    raise AulaApiError("Aula returned an unreadable spare-time activity; no pickup change was made")
                payload["spareTimeActivity"] = {"startDate": _time(spare["startTime"]),
                                              "endDate": _time(spare["endTime"]), "comment": spare["comment"]}
            if template.get("activityType") == 0 and _time(template.get("exitTime")) == pickup_time \
                    and template.get("exitWith") == person:
                return {**self._public(child, day, [row]), "confirmed": True, "changed": False}
            response = self.controller.source_client.post("?method=presence.updatePresenceTemplate", payload)
            if not isinstance(response, dict) or not isinstance(response.get("status"), dict) \
                    or response["status"].get("code") != 0 or response.get("data") is False:
                raise AulaApiError("Aula did not confirm the pickup request. Check Aula before retrying.")
            _, after = self._read(slug, day, row["id"])
            saved = after[0]["template"]
            if saved.get("activityType") != 0 or _time(saved.get("exitTime")) != pickup_time \
                    or saved.get("exitWith") != person or _time(saved.get("entryTime")) != arrival \
                    or saved.get("comment") != template.get("comment") or saved.get("spareTimeActivity") != spare:
                raise AulaApiError("Aula has not confirmed the complete pickup plan. Check Aula before retrying.")
            self.controller.request_refresh()
            return {**self._public(child, day, after), "confirmed": True, "changed": True}
