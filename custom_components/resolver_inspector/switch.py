"""Administrator-controlled, opt-in startup DNS reapplication."""

from homeassistant.components.switch import SwitchEntity

from . import DOMAIN
from .entity import ResolverEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([StartupReapply(entry, "startup_reapply")])


class StartupReapply(ResolverEntity, SwitchEntity):
    _attr_icon = "mdi:restart"

    @property
    def is_on(self):
        return self.entry.runtime_data["status"].get("startup_reapply", False)

    async def async_turn_on(self, **kwargs):
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_enabled(False)

    async def _set_enabled(self, enabled):
        await self.hass.services.async_call(
            DOMAIN, "configure_startup", {"enabled": enabled},
            blocking=True, context=self._context,
        )
