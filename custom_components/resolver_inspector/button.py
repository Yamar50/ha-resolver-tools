"""Explicit apply, original restore, and refresh actions."""
from homeassistant.components.button import ButtonEntity
from . import DOMAIN
from .entity import ResolverEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ResolverButton(entry, name) for name in ("apply", "restore", "inspect")])


class ResolverButton(ResolverEntity, ButtonEntity):
    def __init__(self, entry, name):
        super().__init__(entry, name)
        self.action = name

    async def async_press(self):
        data = {}
        if self.action in ("apply", "restore"):
            result = await self.hass.services.async_call(DOMAIN, "inspect", {}, blocking=True,
                                                        return_response=True, context=self._context)
            data["expected_sha256"] = result["content_sha256"]
        await self.hass.services.async_call(DOMAIN, self.action, data, blocking=True, context=self._context)
