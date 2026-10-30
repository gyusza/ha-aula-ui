"""Single-instance, credential-free setup for the calendar endpoint."""

from homeassistant import config_entries


class CalendarConfigFlow(config_entries.ConfigFlow, domain="aula_calendar"):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_create_entry(title="Aula Feed Calendar", data={})
