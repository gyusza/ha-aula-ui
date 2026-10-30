"""Current presence snapshots, separate from feed content and AI work."""

import logging
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)
# Snapshots are rebuilt on every feed publish; report each malformed value once.
_reported_invalid = set()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _sleep_intervals(intervals, now, zone):
    local_date = now.astimezone(zone).date()

    def parse(value):
        if isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", value):
            return datetime.combine(local_date, time.fromisoformat(value), tzinfo=zone)
        return _timestamp(value)

    result = []
    for interval in intervals:
        start = parse(interval.get("start"))
        end = parse(interval.get("end")) if interval.get("end") else None
        if not start or interval.get("end") and (not end or end < start):
            key = (str(interval.get("start")), str(interval.get("end")))
            if key not in _reported_invalid:
                if len(_reported_invalid) >= 256:
                    _reported_invalid.clear()
                _reported_invalid.add(key)
                _LOGGER.warning("event=presence.nap_invalid reason=invalid_time")
            continue
        # Allow Aula's clock to run slightly ahead of ours, as for checked_at below.
        latest = now + timedelta(seconds=60)
        if (start.astimezone(zone).date() != local_date or start > latest
                or end and (end > latest or end.astimezone(zone).date() != local_date)):
            # Expected around midnight: Aula still lists the previous day's naps as
            # bare times, which would otherwise read as today and in the future.
            _LOGGER.debug("event=presence.nap_skipped reason=outdated_time")
            continue
        result.append({"start": start.isoformat(), "end": end.isoformat() if end else None})
    return sorted(result, key=lambda interval: interval["start"])


def snapshot(controller, now=None):
    now = now or datetime.now(timezone.utc)
    result = []
    for profile in controller.profiles.snapshot():
        slug = profile["slug"]
        zone = ZoneInfo(profile["timezone"])
        if controller.account:
            data = controller.store.get("direct_presence:" + slug) or {}
            sync = controller.store.get("presence_sync:" + slug) or {}
            checked = _timestamp(sync.get("checked_at"))
            failed = sync.get("failed", False)
        else:
            child = next(child for child in controller.config.children if child.slug == slug)
            entity = (child.weekplan_sensor or "").replace("sensor.easyiq_", "").removesuffix("_weekplan")
            state = next((state for state in getattr(controller, "source_states", [])
                          if state.get("child_slug") == slug or (
                              entity and state.get("entity_id") == f"binary_sensor.easyiq_{entity}_present")), {})
            attrs = state.get("attributes") or {}
            data = {**attrs, "status": attrs.get("status_description") or state.get("state")}
            checked = _timestamp(state.get("last_updated") or state.get("last_changed"))
            failed = state.get("state") in ("unknown", "unavailable")
        midnight = datetime.combine(now.astimezone(zone).date() + timedelta(days=1),
                                    datetime.min.time(), tzinfo=zone)
        expires = min(checked + timedelta(seconds=max(1800, controller.sync_settings.full_sync_seconds * 2)),
                      midnight) if checked else None
        source_time = _timestamp(data.get("date"))
        fresh = bool(checked and expires and checked <= now + timedelta(seconds=60)
                     and now < expires and checked.astimezone(zone).date() == now.astimezone(zone).date()
                     and (not source_time or source_time.astimezone(zone).date() == now.astimezone(zone).date()))
        status = data.get("status") or "Unknown"
        translations = {
            "IKKE KOMMET": "Not arrived", "KOMMET/TIL STEDE": "Present",
            "TIL STEDE": "Present", "SYG": "Sick", "FERIE/FRI": "Holiday",
            "PÅ TUR": "On a trip", "SOVER": "Napping",
            "HENTET/GÅET": "Picked up", "GÅET": "Gone home",
        }
        status = translations.get(str(status).upper(), status)
        known = status in ("Not arrived", "Sick", "Holiday", "Present", "On a trip", "Napping", "Picked up", "Gone home")
        available = fresh and not failed and known
        result.append({
            "child_slug": slug, "name": profile["display_name"], "timezone": profile["timezone"],
            "status": status if available else "Unknown",
            "state": ("present" if status in ("Present", "On a trip", "Napping") else "away") if available else "unknown",
            "reason": "" if available else ("Refresh failed" if failed else
                      "Status out of date" if checked and not fresh else "No presence reported"),
            "updated_at": checked.isoformat() if checked else None,
            "valid_until": expires.isoformat() if available else None,
            "check_in_time": data.get("check_in_time", "") if available else "",
            "check_out_time": data.get("check_out_time", "") if available else "",
            "sleep_intervals": _sleep_intervals(data.get("sleep_intervals") or [], now, zone) if available else [],
        })
    return result
