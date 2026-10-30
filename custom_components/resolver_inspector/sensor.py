"""Observed file state, separate from editable candidates."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from .entity import ResolverEntity
from .resolver import nameservers


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ResolverSensor(entry, key) for key in ("current_servers", "last_result")])


class ResolverSensor(ResolverEntity, SensorEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key, EntityCategory.DIAGNOSTIC)
        self.key = key

    @property
    def native_value(self):
        runtime = self.entry.runtime_data
        if self.key == "current_servers":
            return ", ".join(nameservers(runtime["inspection"].get("content", ""))) or None
        return runtime["status"].get("last_result")

    @property
    def extra_state_attributes(self):
        runtime = self.entry.runtime_data
        return {"startup_reapply": False, "file_sha256": runtime["inspection"].get("content_sha256"),
                "backup_available": runtime["status"].get("backup_available", False)}
