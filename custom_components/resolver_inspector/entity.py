"""Shared local resolver device and event-driven updates."""
from homeassistant.const import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from . import DOMAIN, SIGNAL


class ResolverEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry, key, category=EntityCategory.CONFIG):
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_entity_category = category
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)},
                                  "name": "Home Assistant DNS", "manufacturer": "DNS Manager"}

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))
