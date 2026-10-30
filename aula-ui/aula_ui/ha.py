"""Thin Home Assistant REST API client.

Works both through the Supervisor proxy (``http://supervisor/core/api`` with
``SUPERVISOR_TOKEN``) and against a plain Home Assistant URL with a long-lived
access token.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from .diagnostics import TRACE, observed, protect

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class HAError(Exception):
    """Raised when Home Assistant cannot be reached or rejects a request."""


class HomeAssistantClient:
    """Minimal synchronous Home Assistant REST client."""

    def __init__(self, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        protect(token)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def close(self) -> None:
        self._session.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    @observed("ha.request", level=TRACE)
    def _request(self, method: str, path: str, timeout: int | None = None, **kwargs: Any) -> requests.Response:
        url = self._url(path)
        try:
            response = self._session.request(
                method, url, timeout=timeout or self._timeout, **kwargs
            )
        except requests.RequestException as err:
            raise HAError(f"{method} request failed ({type(err).__name__})") from err
        _LOGGER.log(logging.WARNING if response.status_code >= 400 else TRACE,
                    "event=ha.response method=%s path=%s status=%d",
                    method, path.split("?", 1)[0], response.status_code)
        if response.status_code >= 400:
            raise HAError(
                f"{method} request returned {response.status_code}"
            )
        return response

    def wait_until_ready(self, attempts: int = 30, delay: int = 10) -> None:
        """Block until the Home Assistant API answers, or raise."""
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._request("GET", "/")
                _LOGGER.info("Connected to Home Assistant at %s", self._base_url)
                return
            except HAError as err:
                last_error = err
                _LOGGER.warning(
                    "Home Assistant not ready (attempt %s/%s): %s", attempt, attempts, err
                )
                time.sleep(delay)
        raise HAError(f"Home Assistant never became ready: {last_error}")

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """Return the full state object for ``entity_id`` or ``None``."""
        try:
            response = self._request("GET", f"/states/{entity_id}")
        except HAError as err:
            if "returned 404" in str(err):
                return None
            raise
        return response.json()

    def list_states(self) -> list[dict[str, Any]]:
        """Return every state object known to Home Assistant."""
        return self._request("GET", "/states").json()

    def list_services(self) -> dict[str, list[str]]:
        """Return ``{domain: [service, ...]}`` for every registered action."""
        return {
            entry["domain"]: sorted(entry.get("services", {}))
            for entry in self._request("GET", "/services").json()
            if isinstance(entry, dict) and isinstance(entry.get("domain"), str)
        }

    def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        return_response: bool = False,
        timeout: int | None = None,
    ) -> Any:
        """Call a Home Assistant service, optionally collecting its response."""
        path = f"/services/{domain}/{service}"
        if return_response:
            path += "?return_response"
        response = self._request("POST", path, json=data or {}, timeout=timeout)
        try:
            return response.json()
        except ValueError:
            return None

    def get_calendar_events(
        self,
        entity_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        lookahead_days: int = 21,
    ) -> list[dict[str, Any]]:
        """Return calendar events in the given window."""
        start = start or datetime.now(timezone.utc)
        end = end or (start + timedelta(days=lookahead_days))
        params = {
            "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            response = self._request("GET", f"/calendars/{entity_id}", params=params)
        except HAError as err:
            _LOGGER.warning("Could not read calendar %s: %s", entity_id, err)
            return []
        data = response.json()
        return data if isinstance(data, list) else []
