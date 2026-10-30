"""Administrator-only, on-demand resolver inspection."""

import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.helpers.service import async_register_admin_service

from .inspect import inspect_resolver

DOMAIN = 'resolver_inspector'


async def async_setup_entry(hass, entry):
    async def inspect_action(call):
        return await hass.async_add_executor_job(inspect_resolver)

    async_register_admin_service(
        hass, DOMAIN, 'inspect', inspect_action,
        schema=vol.Schema({}), supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_unload_entry(hass, entry):
    hass.services.async_remove(DOMAIN, 'inspect')
    return True
