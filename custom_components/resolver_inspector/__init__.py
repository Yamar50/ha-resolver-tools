"""On-demand resolver inspection and explicit administrator-controlled DNS changes."""

import asyncio
import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service

from .inspect import inspect_resolver
from .resolver import ResolverManager, mount_info, nameservers, normalize_servers, probe_servers, system_probe

DOMAIN = "resolver_inspector"
PLATFORMS = [Platform.TEXT, Platform.BUTTON, Platform.SENSOR]
SIGNAL = DOMAIN + "_updated"


async def async_setup_entry(hass, entry):
    if entry.title == "Resolver Inspector":
        hass.config_entries.async_update_entry(entry, title="DNS Manager")
    manager = ResolverManager(hass.config.path(".storage", "resolver_inspector_backup.json"))
    lock = asyncio.Lock()
    runtime = {"manager": manager, "lock": lock, "inspection": {}, "status": {}}
    entry.runtime_data = runtime

    async def refresh():
        runtime["inspection"] = await hass.async_add_executor_job(inspect_resolver)
        runtime["status"] = await hass.async_add_executor_job(manager.status)
        async_dispatcher_send(hass, SIGNAL)

    async def inspect_action(call):
        async with lock:
            await refresh()
            return {**runtime["inspection"], "mount": await hass.async_add_executor_job(mount_info),
                    "management": runtime["status"]}

    async def check_action(call):
        async with lock:
            await refresh()
            servers = nameservers(runtime["inspection"].get("content", ""))
            return {"direct_dns_checks": await hass.async_add_executor_job(probe_servers, servers),
                    "system_dns_checks": await hass.async_add_executor_job(system_probe)}

    async def apply_action(call):
        async with lock:
            servers = call.data.get("servers") or entry.options.get("candidate_servers")
            if not servers:
                raise HomeAssistantError("Enter DNS IP addresses first")
            try:
                result = await hass.async_add_executor_job(manager.apply, servers, call.data["expected_sha256"])
            except (OSError, ValueError) as error:
                raise HomeAssistantError(str(error)) from error
            finally:
                await refresh()
            return result

    async def restore_action(call):
        async with lock:
            try:
                return await hass.async_add_executor_job(manager.restore, call.data["expected_sha256"])
            except (OSError, ValueError) as error:
                raise HomeAssistantError(str(error)) from error
            finally:
                await refresh()

    empty = vol.Schema({})
    digest_schema = {vol.Required("expected_sha256"): vol.Match(r"^[a-f0-9]{64}$")}
    actions = {
        "inspect": (inspect_action, empty),
        "check": (check_action, empty),
        "apply": (apply_action, vol.Schema({**digest_schema, vol.Optional("servers"): vol.All([str], normalize_servers)})),
        "restore": (restore_action, vol.Schema(digest_schema)),
    }
    for name, (action, schema) in actions.items():
        async_register_admin_service(hass, DOMAIN, name, action, schema=schema,
                                     supports_response=SupportsResponse.OPTIONAL)
    await refresh()  # Read only. Never apply saved candidates at startup.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    for name in ("inspect", "check", "apply", "restore"):
        hass.services.async_remove(DOMAIN, name)
    return True
