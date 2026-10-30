"""Install the bundled no-cache HA calendar endpoint into the config mount."""

import logging
import shutil
from pathlib import Path
from .ha import HAError

_LOGGER = logging.getLogger(__name__)


def install_bridge():
    root = Path("/homeassistant")
    if not root.is_dir():
        _LOGGER.warning("HA config mount missing; calendar endpoint cannot be installed")
        return
    source = Path(__file__).with_name("ha_calendar")
    destination = root / "custom_components" / "aula_calendar"
    destination.mkdir(parents=True, exist_ok=True)
    changed = False
    for filename in ("__init__.py", "config_flow.py", "manifest.json", "strings.json"):
        existing = destination / filename
        if not existing.exists() or existing.read_bytes() != (source / filename).read_bytes():
            shutil.copyfile(source / filename, existing)
            changed = True
    if changed:
        _LOGGER.info("Installed Aula Feed Calendar integration; restart HA and add the integration once")


def ensure_bridge(client):
    if not Path("/homeassistant").is_dir():
        return "HA config mount missing; calendar endpoint is unavailable."
    try:
        entries = client._request("GET", "/config/config_entries/entry").json()
        if not isinstance(entries, list):
            raise ValueError("config entries response is not a list")
        existing = next((entry for entry in entries
                         if isinstance(entry, dict) and entry.get("domain") == "aula_calendar"), None)
        if existing:
            if existing.get("state") != "loaded":
                return "Aula Feed Calendar integration is not loaded. Check HA integration settings."
            return None
        result = client._request("POST", "/config/config_entries/flow", json={
            "handler": "aula_calendar", "show_advanced_options": False,
        }).json()
        if not isinstance(result, dict) or result.get("type") != "create_entry":
            return "Add the Aula Feed Calendar integration in Home Assistant settings."
        return None
    except (HAError, ValueError) as err:
        # ValueError covers non-JSON or unexpectedly shaped HA responses.
        _LOGGER.warning("Calendar endpoint setup unavailable: %s", err)
        return "Restart Home Assistant once to load the bundled Aula Feed Calendar integration, then refresh the feed."
