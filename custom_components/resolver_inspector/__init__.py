"""On-demand resolver inspection and explicit administrator-controlled DNS changes."""

import asyncio
import ipaddress
import logging
import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import DATA_CONNECTOR, DATA_RESOLVER
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service

from .inspect import inspect_resolver
from .resolver import PROBE_NAMES, ResolverManager, mount_info, nameservers, normalize_servers, probe_servers, system_probe
from .runtime import RuntimeRefreshError, inspect_runtime, probe_runtime, refresh_runtime

DOMAIN = "resolver_inspector"
PLATFORMS = [Platform.TEXT, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
SIGNAL = DOMAIN + "_updated"
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry):
    if entry.title == "Resolver Inspector":
        hass.config_entries.async_update_entry(entry, title="DNS Manager")
    manager = ResolverManager(hass.config.path(".storage", "resolver_inspector_backup.json"))
    lock = asyncio.Lock()
    runtime = {"manager": manager, "lock": lock, "inspection": {}, "status": {},
               "runtime_dns": {}, "last_runtime_refresh": {"result": "not_requested"}}
    entry.runtime_data = runtime

    async def refresh():
        runtime["inspection"] = await hass.async_add_executor_job(inspect_resolver)
        runtime["status"] = await hass.async_add_executor_job(manager.status)
        runtime["runtime_dns"] = inspect_runtime(hass.data.get(DATA_RESOLVER))
        async_dispatcher_send(hass, SIGNAL)

    async def sync_runtime(expected=None):
        await refresh()
        current = nameservers(runtime["inspection"].get("content", ""))
        if expected is not None and [str(ipaddress.ip_address(s)) for s in current] != expected:
            runtime["last_runtime_refresh"] = {"result": "file_changed", "expected_servers": expected}
            await refresh()
            raise HomeAssistantError("DNS file changed before runtime refresh; inspect again")
        try:
            result = await refresh_runtime(hass.data.get(DATA_RESOLVER), current,
                                           tuple(hass.data.get(DATA_CONNECTOR, {}).values()))
            runtime["last_runtime_refresh"] = result
            return result
        except RuntimeRefreshError as error:
            runtime["last_runtime_refresh"] = error.report
            raise HomeAssistantError(str(error)) from error
        finally:
            await refresh()

    async def inspect_action(call):
        async with lock:
            await refresh()
            return {**runtime["inspection"], "mount": await hass.async_add_executor_job(mount_info),
                    "management": runtime["status"], "runtime_dns": runtime["runtime_dns"],
                    "last_runtime_refresh": runtime["last_runtime_refresh"]}

    async def check_action(call):
        async with lock:
            await refresh()
            servers = nameservers(runtime["inspection"].get("content", ""))
            return {"direct_dns_checks": await hass.async_add_executor_job(probe_servers, servers),
                    "system_dns_checks": await hass.async_add_executor_job(system_probe),
                    "runtime_dns_checks": await probe_runtime(hass.data.get(DATA_RESOLVER), PROBE_NAMES)}

    async def apply_action(call):
        async with lock:
            servers = call.data.get("servers") or entry.options.get("candidate_servers")
            if not servers:
                raise HomeAssistantError("Enter DNS IP addresses first")
            try:
                result = await hass.async_add_executor_job(manager.apply, servers, call.data["expected_sha256"])
                result["runtime_refresh"] = await sync_runtime(result["servers"])
            except (OSError, ValueError) as error:
                raise HomeAssistantError(str(error)) from error
            finally:
                await refresh()
            return result

    async def restore_action(call):
        async with lock:
            try:
                result = await hass.async_add_executor_job(manager.restore, call.data["expected_sha256"])
                result["runtime_refresh"] = await sync_runtime()
                return result
            except (OSError, ValueError) as error:
                raise HomeAssistantError(str(error)) from error
            finally:
                await refresh()

    async def refresh_runtime_action(call):
        async with lock:
            return await sync_runtime()

    async def configure_startup_action(call):
        async with lock:
            try:
                return await hass.async_add_executor_job(manager.configure_startup, call.data["enabled"])
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
        "configure_startup": (configure_startup_action, vol.Schema({vol.Required("enabled"): bool})),
        "refresh_runtime": (refresh_runtime_action, empty),
    }
    for name, (action, schema) in actions.items():
        async_register_admin_service(hass, DOMAIN, name, action, schema=schema,
                                     supports_response=SupportsResponse.OPTIONAL)
    # Also runs on integration reload. No background polling or draft replay.
    async with lock:
        try:
            result = await hass.async_add_executor_job(manager.reapply_on_startup)
            if result["result"] == "reapplied":
                _LOGGER.info("Reapplied saved DNS during integration startup")
            if result["result"] in ("reapplied", "already_matches"):
                await sync_runtime(result["servers"])
        except (OSError, ValueError, HomeAssistantError):
            # Keep the controls available for inspection and explicit recovery.
            _LOGGER.exception("Could not reapply saved DNS during integration startup")
        await refresh()

    async def started(event):
        # Other integrations may create their resolver during concurrent setup.
        # Check once more when HA starts; this is not a periodic repair loop.
        async with lock:
            await refresh()
            status = runtime["status"]
            if not status.get("startup_reapply") or status.get("last_result") != "applied":
                return
            try:
                await sync_runtime(status["saved_servers"])
            except (OSError, ValueError, HomeAssistantError):
                _LOGGER.exception("Could not refresh HA runtime DNS after startup")

    if not hass.is_running:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, started))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    for name in ("inspect", "check", "apply", "restore", "configure_startup", "refresh_runtime"):
        hass.services.async_remove(DOMAIN, name)
    return True
