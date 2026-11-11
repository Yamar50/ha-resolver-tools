"""Observed file state, separate from editable candidates."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from .entity import ResolverEntity
from .resolver import nameservers
from .watchdog import STATES


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ResolverSensor(entry, key) for key in ("current_servers", "runtime_servers", "last_result", "monitor_status", "hosts_monitor_status")])


class ResolverSensor(ResolverEntity, SensorEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key, EntityCategory.DIAGNOSTIC)
        self.key = key
        if key in ("monitor_status", "hosts_monitor_status"):
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = STATES

    @property
    def native_value(self):
        runtime = self.entry.runtime_data
        if self.key == "current_servers":
            return ", ".join(nameservers(runtime["inspection"].get("content", ""))) or None
        if self.key == "runtime_servers":
            return ", ".join(runtime["runtime_dns"].get("servers", [])) or None
        if self.key == "monitor_status":
            return runtime["monitor"]["status"]
        if self.key == "hosts_monitor_status":
            return runtime["hosts_monitor"]["status"]
        return runtime["status"].get("last_result")

    @property
    def extra_state_attributes(self):
        runtime = self.entry.runtime_data
        if self.key == "monitor_status":
            return {k: v for k, v in runtime["monitor"].items() if k != "status"}
        if self.key == "hosts_monitor_status":
            return {**{k: v for k, v in runtime["hosts_monitor"].items() if k != "status"},
                    **{k: v for k, v in runtime["hosts"].items() if k not in ("content", "path", "error")}}
        return {"startup_reapply": runtime["status"].get("startup_reapply", False),
                "saved_servers": runtime["status"].get("saved_servers", []),
                "startup_result": runtime["status"].get("startup_result"),
                "startup_checked_at": runtime["status"].get("startup_checked_at"),
                "file_sha256": runtime["inspection"].get("content_sha256"),
                "backup_available": runtime["status"].get("backup_available", False),
                "runtime_dns_status": runtime["runtime_dns"].get("status"),
                "runtime_dns_servers": runtime["runtime_dns"].get("servers", []),
                "runtime_refresh_result": runtime["last_runtime_refresh"].get("result"),
                "runtime_refresh_checked_at": runtime["last_runtime_refresh"].get("checked_at")}
