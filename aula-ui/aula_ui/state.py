"""Durable add-on state.

States written through the Home Assistant REST API are *not* restored after a
Home Assistant restart, so every generated value is mirrored into
``/data/state.json`` and re-published on every poll cycle.
"""

from __future__ import annotations

import json
import logging
from .diagnostics import TRACE, observed
import os
import tempfile
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

STATE_VERSION = 1


class StateStore:
    """Small JSON-backed key/value store persisted on the add-on data volume."""

    def __init__(self, data_dir: str, filename: str = "state.json") -> None:
        self._path = os.path.join(data_dir, filename)
        self._data: dict[str, Any] = {"version": STATE_VERSION, "children": {}}
        # The poll loop, web handlers and rate-limit callbacks all save; serialise writers.
        self._save_lock = threading.Lock()
        self.load()

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> None:
        if not os.path.exists(self._path):
            _LOGGER.info("No persisted state at %s, starting fresh", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.error("Could not read %s (%s); starting fresh", self._path, err)
            return
        if isinstance(data, dict):
            data.setdefault("version", STATE_VERSION)
            data.setdefault("children", {})
            self._data = data
            _LOGGER.info("Restored persisted state from %s", self._path)

    @observed("state.save", level=TRACE)
    def save(self) -> None:
        directory = os.path.dirname(self._path) or "."
        with self._save_lock:
            text = None
            for _ in range(3):
                try:
                    # Serialise first: another thread may mutate the state meanwhile.
                    # Compact: this multi-MB file is rewritten after every AI step.
                    text = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
                    break
                except RuntimeError:
                    continue
            if text is None:
                _LOGGER.error("Could not persist state to %s: it kept changing during save", self._path)
                return
            temp_path = None
            try:
                os.makedirs(directory, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    "w", dir=directory, delete=False, encoding="utf-8"
                ) as handle:
                    temp_path = handle.name
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self._path)
                temp_path = None
            except OSError as err:
                _LOGGER.error("Could not persist state to %s: %s", self._path, err)
            finally:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

    def child(self, slug: str) -> dict[str, Any]:
        """Return (creating if needed) the mutable state bucket for a child."""
        children = self._data.setdefault("children", {})
        return children.setdefault(
            slug,
            {
                "last_message": None,
                "overview": None,
                "is_translated": False,
                "generated_at": None,
                "source": None,
                "history": [],
                "events": [],
            },
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def push_history(self, slug: str, message: dict[str, Any], limit: int = 25) -> list[dict[str, Any]]:
        """Prepend ``message`` to the child's thread history (newest first)."""
        bucket = self.child(slug)
        history = [m for m in bucket.get("history", []) if m.get("id") != message.get("id")]
        history.insert(0, message)
        bucket["history"] = history[:limit]
        return bucket["history"]
