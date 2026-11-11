"""Administrator-controlled, opt-in startup DNS reapplication."""

from homeassistant.components.switch import SwitchEntity

from . import DOMAIN
from .entity import ResolverEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([StartupReapply(entry, "startup_reapply"), AutomaticRepair(entry, "watchdog_enabled"),
                        HostsRepair(entry, "hosts_watchdog_enabled")])


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


class AutomaticRepair(StartupReapply):
    _attr_icon = "mdi:shield-refresh"

    @property
    def is_on(self):
        return self.entry.runtime_data["status"].get("watchdog_enabled", False)

    async def _set_enabled(self, enabled):
        await self.hass.services.async_call(
            DOMAIN, "configure_watchdog", {"enabled": enabled},
            blocking=True, context=self._context,
        )


class HostsRepair(StartupReapply):
    _attr_icon = "mdi:home-check"

    @property
    def is_on(self):
        return self.entry.runtime_data["hosts"].get("enabled", False)

    async def _set_enabled(self, enabled):
        await self.hass.services.async_call(
            DOMAIN, "configure_hosts_watchdog", {"enabled": enabled},
            blocking=True, context=self._context,
        )
