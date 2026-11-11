"""On-demand resolver inspection and explicit administrator-controlled DNS changes."""

import asyncio
import ipaddress
import logging
from datetime import timedelta
import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import DATA_CONNECTOR, DATA_RESOLVER
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service

from .inspect import inspect_resolver
from .hosts import HostsManager, HostsWatchdog, check_system_localhost
from .resolver import PROBE_NAMES, ResolverManager, mount_info, nameservers, normalize_servers, probe_servers, system_probe
from .runtime import RuntimeRefreshError, inspect_runtime, probe_runtime, probe_localhost, refresh_runtime
from .watchdog import DNSWatchdog, INTERVAL_SECONDS

DOMAIN = "resolver_inspector"
PLATFORMS = [Platform.TEXT, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
SIGNAL = DOMAIN + "_updated"
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry):
    if entry.title == "Resolver Inspector":
        hass.config_entries.async_update_entry(entry, title="DNS Manager")
    manager = ResolverManager(hass.config.path(".storage", "resolver_inspector_backup.json"))
    hosts_manager = HostsManager(hass.config.path(".storage", "resolver_inspector_hosts_backup.json"))
    lock = asyncio.Lock()
    runtime = {"manager": manager, "lock": lock, "inspection": {}, "status": {},
               "runtime_dns": {}, "hosts": {}, "last_runtime_refresh": {"result": "not_requested"}}
    entry.runtime_data = runtime

    async def refresh_hosts():
        try:
            runtime["hosts"] = await hass.async_add_executor_job(hosts_manager.inspect)
        except Exception as error:
            # Hosts failures must not stop the independent DNS repair path.
            runtime["hosts"] = {"error": f"{type(error).__name__}: {error}"}
        return runtime["hosts"]

    async def refresh():
        runtime["inspection"] = await hass.async_add_executor_job(inspect_resolver)
        runtime["status"] = await hass.async_add_executor_job(manager.status)
        runtime["runtime_dns"] = inspect_runtime(hass.data.get(DATA_RESOLVER))
        await refresh_hosts()
        async_dispatcher_send(hass, SIGNAL)
        return runtime

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
                    "last_runtime_refresh": runtime["last_runtime_refresh"],
                    "monitor": dict(watchdog.state), "hosts": runtime["hosts"],
                    "hosts_monitor": dict(hosts_watchdog.state)}

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
                await watchdog.check(repair=False)
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
                await watchdog.check(repair=False)
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

    async def apply_saved(servers, digest):
        return await hass.async_add_executor_job(manager.apply, servers, digest)

    def emit_status(event):
        event = {"resource": "dns", **event}
        hass.bus.async_fire(DOMAIN + "_status", event)
        label = "HOSTS_MONITOR" if event["resource"] == "hosts" else "DNS_MONITOR"
        if event["kind"] == "recovered":
            _LOGGER.info("%s recovered: monitored settings match", label)
        else:
            _LOGGER.warning("%s %s: %s", label, event["kind"], event.get("error") or "Settings mismatch")

    async def repair_hosts(digest):
        result = await hass.async_add_executor_job(hosts_manager.repair, digest)
        # A previous localhost HTTP lookup may be cached on HA connectors.
        if result["result"] == "repaired":
            resolver = hass.data.get(DATA_RESOLVER)
            for connector in hass.data.get(DATA_CONNECTOR, {}).values():
                if resolver is not None and getattr(connector, "_resolver", None) is resolver:
                    connector.clear_dns_cache()
        return result

    watchdog = DNSWatchdog(refresh, apply_saved, sync_runtime, emit_status)
    runtime["monitor"] = watchdog.state
    hosts_watchdog = HostsWatchdog(refresh_hosts, repair_hosts, emit_status)
    runtime["hosts_monitor"] = hosts_watchdog.state

    async def hosts_inspect_action(call):
        async with lock:
            result = await refresh_hosts()
            return {**result, "monitor": dict(hosts_watchdog.state),
                    "mount": await hass.async_add_executor_job(mount_info, hosts_manager.path)}

    async def check_localhost_action(call):
        async with lock:
            snapshot = await refresh_hosts()
            if not snapshot.get("matches"):
                raise HomeAssistantError("Repair localhost mappings before running lookup diagnostics")
            return {"system": await hass.async_add_executor_job(check_system_localhost),
                    "runtime": await probe_localhost(hass.data.get(DATA_RESOLVER))}

    async def hosts_change(call, restore=False):
        async with lock:
            try:
                if restore:
                    result = await hass.async_add_executor_job(hosts_manager.restore, call.data["expected_sha256"])
                else:
                    result = await repair_hosts(call.data["expected_sha256"])
                await hosts_watchdog.check()
                return result
            except (OSError, ValueError) as error:
                raise HomeAssistantError(str(error)) from error
            finally:
                await refresh_hosts()
                async_dispatcher_send(hass, SIGNAL)

    async def repair_localhost_action(call):
        return await hosts_change(call)

    async def restore_hosts_action(call):
        return await hosts_change(call, restore=True)

    async def configure_hosts_action(call):
        async with lock:
            try:
                await hass.async_add_executor_job(hosts_manager.configure, call.data["enabled"])
                return await hosts_watchdog.check()
            except (OSError, ValueError) as error:
                raise HomeAssistantError(str(error)) from error
            finally:
                await refresh_hosts()
                async_dispatcher_send(hass, SIGNAL)

    async def monitor_now_action(call):
        async with lock:
            result = await watchdog.check()
            result["hosts"] = await hosts_watchdog.check()
            async_dispatcher_send(hass, SIGNAL)
            return result

    async def configure_watchdog_action(call):
        async with lock:
            try:
                await hass.async_add_executor_job(manager.configure_watchdog, call.data["enabled"])
            except (OSError, ValueError) as error:
                raise HomeAssistantError(str(error)) from error
            result = await watchdog.check()
            async_dispatcher_send(hass, SIGNAL)
            return result

    async def periodic(now):
        # Do not queue checks behind a slow/manual operation. The next tick retries.
        if lock.locked():
            return
        await monitor_now_action(None)

    empty = vol.Schema({})
    digest_schema = {vol.Required("expected_sha256"): vol.Match(r"^[a-f0-9]{64}$")}
    actions = {
        "inspect": (inspect_action, empty),
        "check": (check_action, empty),
        "apply": (apply_action, vol.Schema({**digest_schema, vol.Optional("servers"): vol.All([str], normalize_servers)})),
        "restore": (restore_action, vol.Schema(digest_schema)),
        "configure_startup": (configure_startup_action, vol.Schema({vol.Required("enabled"): bool})),
        "refresh_runtime": (refresh_runtime_action, empty),
        "monitor_now": (monitor_now_action, empty),
        "configure_watchdog": (configure_watchdog_action, vol.Schema({vol.Required("enabled"): bool})),
        "inspect_hosts": (hosts_inspect_action, empty),
        "check_localhost": (check_localhost_action, empty),
        "repair_localhost": (repair_localhost_action, vol.Schema(digest_schema)),
        "restore_hosts": (restore_hosts_action, vol.Schema(digest_schema)),
        "configure_hosts_watchdog": (configure_hosts_action, vol.Schema({vol.Required("enabled"): bool})),
    }
    for name, (action, schema) in actions.items():
        async_register_admin_service(hass, DOMAIN, name, action, schema=schema,
                                     supports_response=SupportsResponse.OPTIONAL)
    # The existing startup policy remains independent from periodic repair.
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
    entry.async_on_unload(async_track_time_interval(hass, periodic, timedelta(seconds=INTERVAL_SECONDS)))
    async with lock:
        await hosts_watchdog.check()
        await watchdog.check(repair=False)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    for name in ("inspect", "check", "apply", "restore", "configure_startup", "refresh_runtime", "configure_watchdog", "monitor_now",
                 "inspect_hosts", "check_localhost", "repair_localhost", "restore_hosts", "configure_hosts_watchdog"):
        hass.services.async_remove(DOMAIN, name)
    return True
