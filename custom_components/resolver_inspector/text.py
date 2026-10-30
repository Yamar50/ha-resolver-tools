"""DNS candidates: editing this entity does not change the resolver file."""
from homeassistant.components.text import TextEntity
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.exceptions import HomeAssistantError
from . import SIGNAL
from .entity import ResolverEntity
from .resolver import nameservers, normalize_servers


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DNSCandidates(entry, "candidate_servers")])


class DNSCandidates(ResolverEntity, TextEntity):
    _attr_native_max = 160
    _attr_native_min = 1
    _attr_icon = "mdi:dns"

    @property
    def native_value(self):
        values = self.entry.options.get("candidate_servers")
        if values is None:
            values = nameservers(self.entry.runtime_data["inspection"].get("content", ""))
        return ", ".join(values) or None

    async def async_set_value(self, value):
        try:
            servers = normalize_servers(value)
        except ValueError as error:
            raise HomeAssistantError(str(error)) from error
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, "candidate_servers": servers})
        async_dispatcher_send(self.hass, SIGNAL)
