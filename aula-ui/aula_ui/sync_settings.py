"""Sync intervals chosen in the app's Settings → Sync page.

Stored in ``/data/sync_settings.json``. Minimums protect the family's Aula
account from rate limiting; the request budget in ``direct_source`` still
applies on top of these values.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .direct_source import DirectAulaClient

_LOGGER = logging.getLogger(__name__)

# key: (default, minimum, maximum); intervals in minutes unless stated.
NUMBERS = {
    "full_sync_minutes": (15, 5, 1440),
    "quick_check_seconds": (60, 30, 900),
    "posts_minutes": (15, 5, 1440),
    "calendar_minutes": (30, 10, 1440),
    "albums_minutes": (30, 10, 1440),
    "messages_from_hour": (6, 0, 23),
    "messages_to_hour": (23, 1, 24),
    "presence_from_hour": (6, 0, 23),
    "presence_to_hour": (19, 1, 24),
}
FLAGS = {"quick_messages": True, "quick_presence": True, "presence_weekdays_only": True}
DEFAULTS: dict[str, Any] = {**{key: value[0] for key, value in NUMBERS.items()}, **FLAGS}


class SyncSettings:
    def __init__(self, data_dir: str, legacy_poll_interval: int | None = None) -> None:
        self.path = Path(data_dir) / "sync_settings.json"
        self.lock = threading.RLock()
        self._saved: dict[str, Any] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                self._saved = self._validate(loaded) if isinstance(loaded, dict) else {}
            except (OSError, ValueError) as err:
                _LOGGER.warning("Sync settings unreadable, using defaults: %s", type(err).__name__)
        elif legacy_poll_interval and legacy_poll_interval != NUMBERS["full_sync_minutes"][0] * 60:
            # One-time carry-over of the retired poll_interval add-on option.
            low, high = NUMBERS["full_sync_minutes"][1:]
            self._saved = {"full_sync_minutes": min(high, max(low, round(legacy_poll_interval / 60)))}

    def values(self) -> dict[str, Any]:
        with self.lock:
            return {**DEFAULTS, **self._saved}

    def __getitem__(self, key: str) -> Any:
        return self.values()[key]

    @property
    def full_sync_seconds(self) -> int:
        return int(self["full_sync_minutes"]) * 60

    @property
    def quick_seconds(self) -> int:
        return int(self["quick_check_seconds"])

    def refresh_seconds(self, kind: str) -> int:
        key = {"calendar": "calendar_minutes", "planning": "calendar_minutes",
               "posts": "posts_minutes", "albums": "albums_minutes"}[kind]
        return int(self[key]) * 60

    @staticmethod
    def _validate(value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown sync setting: {sorted(unknown)[0]}")
        clean: dict[str, Any] = {}
        for key, raw in value.items():
            if key in FLAGS:
                if not isinstance(raw, bool):
                    raise ValueError(f"{key} must be true or false")
                clean[key] = raw
                continue
            low, high = NUMBERS[key][1:]
            if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
                raise ValueError(f"{key} must be a whole number from {low} to {high}")
            clean[key] = raw
        return clean

    def update(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("Sync settings must be an object")
        clean = self._validate(value)
        with self.lock:
            merged = {**self.values(), **clean}
            for prefix in ("messages", "presence"):
                if merged[f"{prefix}_from_hour"] >= merged[f"{prefix}_to_hour"]:
                    raise ValueError(f"The {prefix} quick-check window must start before it ends")
            saved = {key: merged[key] for key in DEFAULTS if merged[key] != DEFAULTS[key]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False) as handle:
                json.dump(saved, handle)
                temporary = handle.name
            os.replace(temporary, self.path)
            self._saved = saved
            return self.values()

    def snapshot(self) -> dict[str, Any]:
        values = self.values()
        return {
            "values": values, "defaults": dict(DEFAULTS),
            "limits": {key: {"min": low, "max": high} for key, (_, low, high) in NUMBERS.items()},
            "estimate": self.estimate(values),
            "budget": DirectAulaClient.BACKGROUND_PER_HOUR,
        }

    @staticmethod
    def estimate(values: dict[str, Any], children: int = 2) -> int:
        """Rough peak Aula requests per hour during the quick-check window."""
        full = 60 / values["full_sync_minutes"]
        quick_per_hour = 3600 / values["quick_check_seconds"]
        per_full = 1 + 2  # presence + message list pages
        slow = (60 / values["posts_minutes"] * 2
                + 60 / values["calendar_minutes"] * children * 2
                + 60 / values["albums_minutes"] * 2)
        quick = quick_per_hour * (int(values["quick_messages"]) + int(values["quick_presence"]))
        return round(full * per_full + slow + quick)
