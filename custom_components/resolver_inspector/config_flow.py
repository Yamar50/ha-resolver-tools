"""Enable one instance; setup does not change resolver settings."""

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow

DOMAIN = 'resolver_inspector'


class ResolverInspectorFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason='single_instance_allowed')
        if user_input is not None:
            return self.async_create_entry(title='DNS Manager', data={})
        return self.async_show_form(step_id='user', data_schema=vol.Schema({}))
