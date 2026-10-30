"""Serve the add-on's secret ICS file without HA static-file cache headers."""

from pathlib import Path
import re

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.http import KEY_HASS

DOMAIN = "aula_calendar"
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class CalendarView(HomeAssistantView):
    url = "/api/aula_calendar/{token}.ics"
    name = "api:aula_calendar"
    requires_auth = False

    async def get(self, request, token):
        if not TOKEN.fullmatch(token):
            raise web.HTTPNotFound()
        hass = request.app[KEY_HASS]
        path = Path(hass.config.path("www", "aula-feed", token + ".ics"))
        try:
            content = await hass.async_add_executor_job(path.read_bytes)
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        return web.Response(body=content, content_type="text/calendar", charset="utf-8",
                            headers={"Cache-Control": "no-store, max-age=0",
                                     "X-Content-Type-Options": "nosniff",
                                     "Referrer-Policy": "no-referrer"})


async def async_setup_entry(hass, entry):
    if DOMAIN not in hass.data:
        hass.http.register_view(CalendarView)
        hass.data[DOMAIN] = True
    return True
