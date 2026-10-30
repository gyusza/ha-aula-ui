import copy
import hashlib
import logging
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, time as time_cls, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl

import requests

from aula_ui.aula_api import AulaApiError, AulaBudgetExceeded, AulaRateLimited
from aula_ui.events import classify_event_type
from aula_ui.messages import strip_html
from vendor.easyiq.calendar_shapes import (
    _event_end_datetime,
    _event_item_type,
    _event_start_datetime,
    _event_title_text,
    _extract_calendar_event_list,
)
from .account_auth import AuthenticationError, BoundedSession
from .avatars import picture_url
from .diagnostics import TRACE, observed

_LOGGER = logging.getLogger(__name__)

AULA_API = "https://www.aula.dk/api/v"
FIRST_API_VERSION = 22
MAX_API_VERSION = 30
EASYIQ_CALENDAR = "https://skoleportal.easyiqcloud.dk/Calendar/CalendarGetWeekplanEvents"


class DirectAulaClient:
    def __init__(self, connection, session=None):
        self.connection = connection
        self.session = session or BoundedSession()
        self.version = FIRST_API_VERSION
        self.profiles = []
        self.context = {}
        self.calendar_variants = {}
        self._guard = threading.Lock()
        self._local = threading.local()
        self._sent = deque()
        self._last_sent = 0.0
        self._cache = {}
        self._paused_until = 0.0
        self._limits = {}
        self._budget_warned = False
        self.on_rate_state = None
        self.on_version = None

    def call_service(self, domain, service, data, **kwargs):
        if domain != "aula" or service != "api_call":
            raise ValueError("Direct Aula transport only supports aula.api_call.")
        return {"service_response": self.get(data["uri"])}

    def get(self, uri):
        return self._request(uri)

    def post(self, uri, data):
        if not self.context:
            self._select_guardian_context()
        return self._request(uri, data)

    def _select_guardian_context(self):
        context = self.get("?method=profiles.getProfileContext&portalrole=guardian")["data"]
        if not isinstance(context, dict):
            raise AulaApiError("Aula did not return a guardian context for calendar access.")
        self.context = context

    def _post_headers(self, url):
        headers = {"Content-Type": "application/json"}
        jar = getattr(self.session, "cookies", None)
        if jar is not None:
            cookie_header = requests.cookies.get_cookie_header(jar, requests.Request("POST", url).prepare())
            cookies = SimpleCookie()
            cookies.load(cookie_header or "")
            if "Csrfp-Token" in cookies:
                headers["csrfp-token"] = cookies["Csrfp-Token"].value
        return headers

    # Guardrails against Aula's rate limiting (HTTP 429). Aula throttles single
    # endpoints (e.g. the calendar) while others keep working, so a 429 briefly
    # pauses everything but escalates only for the endpoint that was refused.
    GLOBAL_PAUSE = 60
    RATE_LIMIT_PAUSE = 15 * 60          # first endpoint pause; doubles on each consecutive 429
    RATE_LIMIT_MAX_PAUSE = 6 * 3600
    MIN_INTERVAL = 1.0                  # seconds between any two Aula requests
    BACKGROUND_PER_HOUR = 240           # automatic sync requests per rolling hour
    TOTAL_PER_HOUR = 360                # all requests, including ones you trigger in the app
    PROFILE_CACHE_SECONDS = 3600
    CACHED_METHODS = frozenset({"profiles.getProfilesByLogin", "profiles.getProfileContext"})

    def rate_limited(self, method=None):
        return time.time() < self._paused_until or (
            method is not None and time.time() < self._limits.get(method, {}).get("until", 0))

    def rate_state(self):
        return {"paused_until": self._paused_until,
                "methods": {method: dict(limit) for method, limit in self._limits.items()}}

    def restore_rate_state(self, state):
        if not isinstance(state, dict):
            return
        try:
            self._paused_until = float(state.get("paused_until") or 0)
            self._limits = {
                str(method): {"until": float(limit.get("until") or 0), "strikes": max(0, int(limit.get("strikes") or 0))}
                for method, limit in (state.get("methods") or {}).items() if isinstance(limit, dict)
            }
        except (TypeError, ValueError, AttributeError):
            self._paused_until, self._limits = 0.0, {}

    def restore_version(self, version):
        """Start at the API version learned earlier instead of re-probing retired ones."""
        if isinstance(version, int) and not isinstance(version, bool) and \
                FIRST_API_VERSION <= version <= MAX_API_VERSION:
            self.version = version

    def _version_learned(self):
        if callable(self.on_version):
            try:
                self.on_version(self.version)
            except Exception as err:  # noqa: BLE001 - remembering the version must never break a request
                _LOGGER.warning("event=aula.version_save_failed error_type=%s", type(err).__name__)

    def _rate_changed(self):
        if callable(self.on_rate_state):
            try:
                self.on_rate_state(self.rate_state())
            except Exception as err:  # noqa: BLE001 - persisting the pause must never break a request
                _LOGGER.warning("event=aula.rate_state_save_failed error_type=%s", type(err).__name__)

    @contextmanager
    def background(self):
        """Mark requests made inside this block as automatic sync traffic."""
        previous = getattr(self._local, "background", False)
        self._local.background = True
        try:
            yield
        finally:
            self._local.background = previous

    def _rate_limit_error(self, method=None):
        until = max(self._paused_until, self._limits.get(method, {}).get("until", 0))
        resume = datetime.now() + timedelta(seconds=max(0, until - time.time()))
        return AulaRateLimited(
            f"Aula is limiting requests (HTTP 429). Cached content is shown; syncing resumes around {resume:%H:%M}.")

    def background_left(self):
        """Automatic-sync requests still available in the rolling hour."""
        with self._guard:
            now = time.monotonic()
            recent = [entry for entry in self._sent if now - entry[0] < 3600]
            return max(0, min(self.BACKGROUND_PER_HOUR - sum(1 for entry in recent if entry[1]),
                              self.TOTAL_PER_HOUR - len(recent)))

    def _admit(self, method):
        """Enforce the hourly budget and spacing before contacting Aula."""
        background = getattr(self._local, "background", False)
        with self._guard:
            now = time.monotonic()
            while self._sent and now - self._sent[0][0] >= 3600:
                self._sent.popleft()
            used = [entry for entry in self._sent if entry[1]] if background else self._sent
            limit = self.BACKGROUND_PER_HOUR if background else self.TOTAL_PER_HOUR
            if len(self._sent) >= self.TOTAL_PER_HOUR or len(used) >= limit:
                oldest = (used or self._sent)[0][0]
                resume = datetime.now() + timedelta(seconds=max(0, 3600 - (now - oldest)))
                # Every refused call would repeat this; warn once per exhausted period.
                _LOGGER.log(logging.DEBUG if self._budget_warned else logging.WARNING,
                            "event=aula.budget_exhausted method=%s background=%s used=%d",
                            method, background, len(used))
                self._budget_warned = True
                raise AulaBudgetExceeded(
                    "Aula request budget reached (protecting against rate limits). Cached content is shown; "
                    f"syncing resumes around {resume:%H:%M}.")
            self._budget_warned = False
            wait = self._last_sent + self.MIN_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_sent = now
            self._sent.append((now, background))

    @observed("aula.request", level=TRACE)
    def _request(self, uri, data=None):
        query = parse_qsl(uri.lstrip("?"), keep_blank_values=True)
        if any(key == "access_token" for key, _ in query):
            raise AulaApiError("Do not supply an access token in a query.")
        refreshed = False
        probed = False
        method = next((value for key, value in query if key == "method"), "unknown")
        method = method if re.fullmatch(r"[A-Za-z0-9_.]{1,100}", method) else "invalid"
        if self.rate_limited(method):
            raise self._rate_limit_error(method)
        cache_key = None
        if data is None and method in self.CACHED_METHODS:
            cache_key = (uri, (getattr(self.connection, "value", None) or {}).get("username"))
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < self.PROFILE_CACHE_SECONDS:
                return copy.deepcopy(cached[1])
        try:
            for _ in range(6):
                self._admit(method)
                started = time.monotonic()
                request = self.session.get if data is None else self.session.post
                version = self.version
                url = AULA_API + str(version)
                response = request(
                    url,
                    params=query + [("access_token", self.connection.access_token())],
                    timeout=(10, 30),
                    **({"json": data, "headers": self._post_headers(url)} if data is not None else {}),
                )
                # 410 is Aula retiring an API version: expected while probing, not a failure.
                _LOGGER.log(logging.WARNING if response.status_code >= 400 and response.status_code != 410
                            else logging.DEBUG,
                            "event=aula.response method=%s http_method=%s version=%d status=%d duration_ms=%d",
                            method, "GET" if data is None else "POST", version,
                            response.status_code, round((time.monotonic() - started) * 1000))
                if response.status_code == 410 and version < MAX_API_VERSION:
                    with self._guard:
                        # Another thread may already have moved past this version.
                        self.version = max(self.version, version + 1)
                    _LOGGER.info("event=aula.version_retry next_version=%d", self.version)
                    probed = True
                    continue
                if response.status_code == 429:
                    try:
                        retry_after = int(response.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        retry_after = 0
                    now = time.time()
                    limit = self._limits.setdefault(method, {"until": 0.0, "strikes": 0})
                    limit["strikes"] += 1
                    escalated = self.RATE_LIMIT_PAUSE * 2 ** min(limit["strikes"] - 1, 10)
                    pause = min(max(retry_after, escalated), self.RATE_LIMIT_MAX_PAUSE)
                    limit["until"] = now + pause
                    self._paused_until = max(self._paused_until, now + min(
                        max(retry_after, self.GLOBAL_PAUSE), self.RATE_LIMIT_MAX_PAUSE))
                    _LOGGER.warning("event=aula.rate_limited method=%s strike=%d pause_seconds=%d "
                                    "global_pause_seconds=%d", method, limit["strikes"], round(pause),
                                    round(self._paused_until - now))
                    self._rate_changed()
                    raise self._rate_limit_error(method)
                if response.status_code == 403 and data is not None:
                    raise AulaApiError(
                        "Aula denied calendar access (HTTP 403) after guardian session setup. "
                        "Check guardian calendar permissions or reconnect the account. "
                        "Previously synced dates are retained."
                    )
                if response.status_code in (401, 403) and not refreshed:
                    _LOGGER.info("event=aula.auth_retry method=%s", method)
                    self.connection.access_token(force=True)
                    refreshed = True
                    if data is not None:
                        self._select_guardian_context()
                    continue
                if response.status_code != 200:
                    raise AulaApiError(f"Aula request failed (HTTP {response.status_code}).")
                payload = response.json()
                if not isinstance(payload, dict) or "data" not in payload:
                    raise AulaApiError("Aula returned an invalid response.")
                status = payload.get("status", {})
                if not isinstance(status, dict) or status.get("code", 0) != 0:
                    raise AulaApiError("Aula rejected this API request.")
                if self._limits.pop(method, None) is not None:
                    _LOGGER.info("event=aula.rate_limit_cleared method=%s", method)
                    self._rate_changed()
                if cache_key is not None:
                    self._cache[cache_key] = (time.monotonic(), copy.deepcopy(payload))
                if probed:
                    self._version_learned()
                return payload
            raise AulaApiError("No supported Aula API version or valid login.")
        except (requests.RequestException, ValueError):
            raise AulaApiError("Aula network request or JSON response failed. Retry later.") from None
        except AuthenticationError as err:
            raise AulaApiError(str(err)) from None

    @observed("aula.discover")
    def discover(self):
        profiles = self.get("?method=profiles.getProfilesByLogin")["data"].get("profiles")
        if not isinstance(profiles, list):
            raise AulaApiError("Aula did not return guardian profiles.")
        context = self.get("?method=profiles.getProfileContext&portalrole=guardian")["data"]
        if not isinstance(context, dict):
            raise AulaApiError("Aula did not return a guardian context.")
        children = {}
        for profile in profiles:
            if not isinstance(profile, dict):
                raise AulaApiError("Invalid Aula guardian profile.")
            for child in profile.get("children", []):
                profile_id = str(child.get("profileId") or "")
                if not profile_id.isdigit() or not child.get("name") or not child.get("id") or not child.get("userId"):
                    raise AulaApiError("A child has incomplete Aula identifiers; cannot safely attribute content.")
                entry = children.setdefault(profile_id, {
                    "profile_id": profile_id, "name": str(child["name"]), "relations": [],
                })
                if child not in entry["relations"]:
                    entry["relations"].append(dict(child))
        if not children:
            raise AulaApiError("No children found. Choose a guardian identity with access to children.")
        self.profiles = profiles
        self.context = context
        return list(children.values())

    def presence(self, child):
        return self.presence_many([child])[child["profile_id"]]

    @observed("aula.presence")
    def presence_many(self, children):
        """Read every child's presence with one request; ``None`` means no presence today."""
        wanted = {child["profile_id"]: {str(relation["id"]) for relation in child["relations"]}
                  for child in children}
        ids = set().union(*wanted.values()) if wanted else set()
        entries = self.get("?method=presence.getDailyOverview&" + "&".join(f"childIds[]={identifier}" for identifier in sorted(ids)))["data"]
        if not isinstance(entries, list):
            raise AulaApiError("Aula presence response is not a list.")
        result = {}
        for profile_id, child_ids in wanted.items():
            matches = [entry for entry in entries
                       if str((entry.get("institutionProfile") or {}).get("id")) in child_ids]
            result[profile_id] = self._presence(matches[0]) if matches else None
        return result

    @staticmethod
    def _presence(entry):
        names = {0: "Not arrived", 1: "Sick", 2: "Holiday", 3: "Present", 4: "On a trip", 5: "Napping", 8: "Picked up"}
        status = entry.get("status")
        sleep_intervals = entry.get("sleepIntervals")
        if sleep_intervals is None:
            sleep_intervals = []
        if not isinstance(sleep_intervals, list) or any(
            not isinstance(interval, dict) or not isinstance(interval.get("startTime"), str)
            or interval.get("endTime") is not None and not isinstance(interval["endTime"], str)
            for interval in sleep_intervals
        ):
            raise AulaApiError("Aula returned invalid nap intervals.")
        return {
            "picture_url": picture_url(entry),
            "status": names.get(status, f"Unknown presence status ({status})"),
            "present": status in (3, 4, 5), "status_code": status,
            "exit_with": strip_html(entry.get("exitWith") or entry.get("comment") or ""),
            "date": entry.get("checkOutTime") or entry.get("checkInTime") or "",
            "sleep_intervals": [
                {"start": interval["startTime"], "end": interval.get("endTime") or ""}
                for interval in sleep_intervals
            ],
            **{key: str(entry.get(source) or "") for key, source in (
                ("check_in_time", "checkInTime"), ("check_out_time", "checkOutTime"),
                ("entry_time", "entryTime"), ("exit_time", "exitTime"),
            )},
        }

    @observed("aula.calendar")
    def native_events(self, child, lookahead_days=21):
        """Fetch Aula's own calendar independently of optional EasyIQ widgets."""
        identifiers = sorted({int(relation["id"]) for relation in child["relations"]})
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        events = {}
        # Aula bounds calendar requests; fetch small overlapping-safe windows.
        for offset in range(0, max(1, lookahead_days), 7):
            first = start + timedelta(days=offset)
            last = start + timedelta(days=min(offset + 7, lookahead_days))
            payload = self.post("?method=calendar.getEventsByProfileIdsAndResourceIds", {
                "instProfileIds": identifiers, "resourceIds": [],
                "start": first.strftime("%Y-%m-%d %H:%M:%S.0000+0000"),
                "end": last.strftime("%Y-%m-%d %H:%M:%S.0000+0000"),
            })["data"]
            if not isinstance(payload, list):
                raise AulaApiError("Aula returned an invalid native calendar response.")
            for raw in payload:
                if not isinstance(raw, dict):
                    raise AulaApiError("Aula returned an invalid native calendar event.")
                event = self._native_event(raw)
                events[event["id"]] = event
        return sorted(events.values(), key=lambda event: event["timestamp"])

    @staticmethod
    def _native_event(raw):
        title = strip_html(raw.get("title") or raw.get("summary") or "")
        start = raw.get("startDateTime") or raw.get("start")
        end = raw.get("endDateTime") or raw.get("end") or ""
        if not title or not isinstance(start, str):
            raise AulaApiError("Aula returned a calendar event without a title or start.")
        try:
            first = datetime.fromisoformat(start.replace("Z", "+00:00"))
            last = None
            if end:
                last = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise AulaApiError("Aula returned a calendar event with an invalid date.") from None
        all_day = raw.get("allDay") is True or raw.get("isAllDay") is True or len(start) == 10
        if all_day and last:
            # Aula can end an all-day event on the final included day (same
            # date or 23:59). RFC 5545 instead requires an exclusive end date.
            last_date = last.date()
            if last_date == first.date() or last.time() != time_cls.min:
                last_date += timedelta(days=1)
            end = last_date.isoformat()
        owner = raw.get("creator") or raw.get("ownerProfile") or raw.get("organizer") or {}
        author = owner.get("fullName") or owner.get("name") or "" if isinstance(owner, dict) else str(owner)
        resource = raw.get("primaryResource") or {}
        location = raw.get("location") or (resource.get("name") if isinstance(resource, dict) else resource) or ""
        if isinstance(location, dict):
            location = location.get("name") or location.get("title") or ""
        description = raw.get("description") or raw.get("content") or ""
        if isinstance(description, dict):
            description = description.get("html") or description.get("text") or ""
        description = strip_html(description)
        identity = f"{raw.get('id') or title}\n{start}"
        return {
            "id": "aula_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            "title": title, "description": description,
            "date": first.date().isoformat(), "time": None if all_day else first.strftime("%H:%M"),
            "timestamp": start[:10] if all_day else start,
            "end": end, "location": strip_html(location),
            "type": classify_event_type(title, description), "origin": "Calendar",
            "calendar_kind": "lesson" if isinstance(raw.get("lesson"), dict) and raw["lesson"].get("lessonId")
            else str(raw.get("type") or ""),
            "calendar_source": "aula", "sender": author or "Aula calendar",
        }

    @observed("easyiq.calendar")
    def easyiq_events(self, child, weeks=3):
        deadline = time.monotonic() + 120
        page = self.context.get("pageConfiguration")
        widgets = page.get("widgetConfigurations") if isinstance(page, dict) else None
        if not isinstance(widgets, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("widget"), dict)
            or not item["widget"].get("widgetId") for item in widgets
        ):
            raise AulaApiError("Aula did not return valid widget availability information.")
        available = {str(item["widget"]["widgetId"]) for item in widgets}
        widget_id = next((value for value in ("0001", "0128", "00142", "0142") if value in available), None)
        if widget_id is None:
            return None
        token = self.get(f"?method=aulaToken.getAulaToken&widgetId={widget_id}")["data"]
        if not isinstance(token, str) or not token:
            raise AulaApiError("EasyIQ widget token is missing.")
        guardian = self.context.get("institutionProfile", {})
        logins = list(dict.fromkeys(str(value) for value in [
            self.connection.value["username"], self.context.get("userId"), guardian.get("id"),
        ] if value))
        codes = sorted({str(relation["institutionCode"]) for relation in child["relations"]})
        headers = {
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "Referer": "https://skoleportal.easyiqcloud.dk/UgeplanWidget",
            "X-Requested-With": "XMLHttpRequest", "x-userprofile": "guardian",
            "x-institutionfilter": ",".join(codes),
        }
        events = {}
        for relation in child["relations"]:
            pid, uid = str(relation["id"]), str(relation["userId"])
            cached = self.calendar_variants.get(pid)
            variants = list(dict.fromkeys(
                ([cached] if cached else []) +
                [(login, login_id, child_id) for login in logins
                 for login_id, child_id in [(pid, uid), (uid, uid), (pid, pid), (login, uid)]]))
            for week in range(weeks):
                successful = False
                for login, login_id, child_id in variants:
                    if time.monotonic() >= deadline:
                        raise AulaApiError("EasyIQ calendar requests timed out; keeping the last successful dates.")
                    try:
                        response = self.session.get(EASYIQ_CALENDAR, headers={
                            **headers, "x-login": login, "x-child": child_id, "x-childfilter": child_id,
                        }, params={
                            "loginId": login_id,
                            "date": (datetime.now(timezone.utc) + timedelta(weeks=week)).isoformat(),
                            "activityFilter": "-1", "courseFilter": "-1", "textFilter": "", "ownWeekPlan": "false",
                        }, timeout=(10, min(30, max(0.1, deadline - time.monotonic()))))
                        if response.status_code != 200:
                            continue
                        payload = response.json()
                        if not self._calendar_shape(payload):
                            raise AulaApiError("EasyIQ returned an unrecognised calendar response.")
                        successful = True
                        raw = _extract_calendar_event_list(payload, normalize=False)
                        for event in raw:
                            normalized = self._event(event)
                            events[normalized["id"]] = normalized
                        if raw or (login, login_id, child_id) == cached:
                            self.calendar_variants[pid] = (login, login_id, child_id)
                            cached = (login, login_id, child_id)
                            variants = [cached] + [variant for variant in variants if variant != cached]
                            break
                    except (requests.RequestException, ValueError):
                        raise AulaApiError("EasyIQ network request or JSON response failed.") from None
                if not successful:
                    raise AulaApiError("EasyIQ rejected the calendar request. Existing dates remain cached.")
        return list(events.values())

    @staticmethod
    def _calendar_shape(payload):
        if isinstance(payload, list):
            return all(isinstance(entry, dict) for entry in payload)
        if isinstance(payload, dict):
            return any(DirectAulaClient._calendar_shape(payload[key]) for key in
                       ("events", "calendarEvents", "items", "data", "value", "result", "results") if key in payload)
        return False

    @staticmethod
    def _event(raw):
        start = _event_start_datetime(raw)
        end = _event_end_datetime(raw)
        title = _event_title_text(raw)
        if not start or not title:
            raise AulaApiError("EasyIQ returned an event without a valid start or title.")
        description = strip_html(raw.get("description") or raw.get("activities") or "")
        kind = _event_item_type(raw)
        timestamp = start.isoformat()
        is_all_day = raw.get("allDay") is True
        return {
            "id": hashlib.sha256((str(raw.get("id", "")) + title + timestamp).encode()).hexdigest()[:24],
            "title": title, "description": description, "timestamp": start.date().isoformat() if is_all_day else timestamp,
            "date": start.date().isoformat(), "time": None if is_all_day else start.strftime("%H:%M"),
            "end": (end.date().isoformat() if is_all_day else end.isoformat()) if end else "",
            "location": strip_html(raw.get("location") or ""),
            "sender": "EasyIQ",
            "type": "homework" if kind == 4 else classify_event_type(title, description),
            "origin": "Homework" if kind == 4 else "Weekplan",
        }
