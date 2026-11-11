"""Exercise actual integration service/startup wiring with small HA test doubles.

These tests do not emulate HA schema validation or replace live HA verification.
File transactions and c-ares behavior are tested separately.
"""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch


class HAError(Exception):
    pass


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        modules = {}
        def module(name, **values):
            result = ModuleType(name)
            result.__dict__.update(values)
            modules[name] = result
        module("voluptuous", Schema=lambda value: value, Required=lambda value: value,
               Optional=lambda value: value, Match=lambda value: value, All=lambda *args: args)
        module("homeassistant", __path__=[])
        module("homeassistant.const", EVENT_HOMEASSISTANT_STARTED="started",
               Platform=SimpleNamespace(TEXT="text", BUTTON="button", SENSOR="sensor", SWITCH="switch"))
        module("homeassistant.core", SupportsResponse=SimpleNamespace(OPTIONAL="optional"))
        module("homeassistant.exceptions", HomeAssistantError=HAError)
        module("homeassistant.helpers", __path__=[])
        module("homeassistant.helpers.aiohttp_client", DATA_CONNECTOR="connectors", DATA_RESOLVER="resolver")
        module("homeassistant.helpers.event", async_track_time_interval=lambda h, fn, interval: h.timers.append((fn, interval)) or Mock())
        module("homeassistant.helpers.dispatcher", async_dispatcher_send=lambda *args: None)
        module("homeassistant.helpers.service", async_register_admin_service=lambda h, d, n, fn, **kw: h.actions.update({n: fn}))
        cls.modules = patch.dict(sys.modules, modules)
        cls.modules.start()
        source = Path(__file__).parents[1] / "custom_components/resolver_inspector"
        spec = importlib.util.spec_from_file_location("dns_lifecycle_test", source / "__init__.py",
                                                    submodule_search_locations=[str(source)])
        cls.integration = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.integration
        spec.loader.exec_module(cls.integration)

    @classmethod
    def tearDownClass(cls):
        cls.modules.stop()
        for name in list(sys.modules):
            if name.startswith("dns_lifecycle_test"):
                del sys.modules[name]

    def setUp(self):
        self.file_servers = ["192.0.2.53"]
        self.channel = SimpleNamespace(servers=["198.51.100.53:53"])
        self.channel.reinit = Mock(side_effect=lambda: setattr(self.channel, "servers", [s + ":53" for s in self.file_servers]))
        self.resolver = SimpleNamespace(_resolver=SimpleNamespace(_channel=self.channel))
        self.connector = SimpleNamespace(_resolver=self.resolver, clear_dns_cache=Mock(), close=Mock())
        self.callbacks = {}
        self.status = {"startup_reapply": True, "last_result": "applied", "saved_servers": ["192.0.2.53"]}
        self.manager = Mock()
        self.manager.status.side_effect = lambda: dict(self.status)
        self.manager.reapply_on_startup.return_value = {"result": "already_matches", "servers": ["192.0.2.53"]}
        self.manager.apply.side_effect = self.apply_file
        self.manager.restore.side_effect = self.restore_file
        self.hass = SimpleNamespace(
            data={"resolver": self.resolver, "connectors": {"default": self.connector}},
            actions={}, timers=[], is_running=False, config=SimpleNamespace(path=lambda *p: "/unused-test-backup"),
            bus=SimpleNamespace(async_listen_once=self.listen, async_fire=Mock()),
            config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock(), async_unload_platforms=AsyncMock(return_value=True)),
            services=SimpleNamespace(async_remove=Mock()))
        async def executor(fn, *args):
            return fn(*args)
        self.hass.async_add_executor_job = executor
        self.entry = SimpleNamespace(title="DNS Manager", options={"candidate_servers": ["192.0.2.53"]},
                                     async_on_unload=Mock())
        self.addCleanup(patch.stopall)
        patch.object(self.integration, "ResolverManager", return_value=self.manager).start()
        patch.object(self.integration, "inspect_resolver", side_effect=lambda: {"content": "".join("nameserver " + s + "\n" for s in self.file_servers)}).start()
        patch.object(self.integration, "mount_info", return_value={}).start()
        patch.object(self.integration, "_LOGGER").start()

    def listen(self, name, fn):
        self.callbacks[name] = fn
        return Mock()

    def apply_file(self, servers, digest):
        self.file_servers = list(servers)
        self.status.update(last_result="applied", saved_servers=list(servers))
        return {"result": "applied", "servers": list(servers)}

    def restore_file(self, digest):
        self.file_servers = ["198.51.100.53"]
        self.status.update(startup_reapply=False, last_result="restored")
        return {"result": "restored"}

    def disable_startup(self):
        self.status["startup_reapply"] = False
        self.manager.reapply_on_startup.return_value = {"result": "disabled"}

    async def setup(self):
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))

    async def call(self, name, **data):
        return await self.hass.actions[name](SimpleNamespace(data=data))

    async def test_matching_file_repairs_stale_existing_resolver_without_file_apply(self):
        await self.setup()
        self.assertEqual(self.channel.servers, ["192.0.2.53:53"])
        self.manager.apply.assert_not_called()
        self.assertEqual(self.entry.runtime_data["last_runtime_refresh"]["result"], "refreshed")
        self.connector.close.assert_not_called()

    async def test_resolver_created_during_startup_gets_second_pass(self):
        self.hass.data.pop("resolver")
        await self.setup()
        self.assertEqual(self.entry.runtime_data["last_runtime_refresh"]["result"], "not_initialized")
        self.hass.data["resolver"] = self.resolver
        await self.callbacks["started"](None)
        self.assertEqual(self.channel.servers, ["192.0.2.53:53"])

    async def test_disabled_startup_and_inspect_never_mutate_runtime(self):
        self.disable_startup()
        await self.setup()
        await self.callbacks["started"](None)
        result = await self.call("inspect")
        self.assertEqual(result["runtime_dns"]["servers"], ["198.51.100.53"])
        self.channel.reinit.assert_not_called()

    async def test_explicit_runtime_refresh_does_not_write_file(self):
        self.disable_startup()
        await self.setup()
        result = await self.call("refresh_runtime")
        self.assertEqual(result["result"], "refreshed")
        self.manager.apply.assert_not_called()
        self.manager.restore.assert_not_called()

    async def test_manual_apply_and_restore_refresh_runtime(self):
        self.disable_startup()
        await self.setup()
        result = await self.call("apply", servers=["192.0.2.54"], expected_sha256="test")
        self.assertEqual(result["runtime_refresh"]["servers_after"], ["192.0.2.54"])
        restored = await self.call("restore", expected_sha256="test")
        self.assertEqual(restored["runtime_refresh"]["servers_after"], ["198.51.100.53"])
        await self.callbacks["started"](None)
        self.assertEqual(self.channel.servers, ["198.51.100.53:53"])

    async def test_runtime_failure_surfaces_separately_after_successful_file_apply(self):
        self.disable_startup()
        await self.setup()
        self.channel.reinit.side_effect = RuntimeError("unsupported backend")
        with self.assertRaises(HAError):
            await self.call("apply", servers=["192.0.2.54"], expected_sha256="test")
        self.assertEqual(self.file_servers, ["192.0.2.54"])
        self.assertEqual(self.entry.runtime_data["status"]["last_result"], "applied")
        self.assertEqual(self.entry.runtime_data["last_runtime_refresh"]["result"], "failed")

    async def test_late_file_change_is_not_silently_adopted_at_started_event(self):
        await self.setup()
        reloads = self.channel.reinit.call_count
        self.file_servers = ["192.0.2.99"]
        await self.callbacks["started"](None)
        self.assertEqual(self.channel.reinit.call_count, reloads)
        self.assertEqual(self.entry.runtime_data["last_runtime_refresh"]["result"], "file_changed")

    async def test_unload_unregisters_new_service_and_listener_is_entry_owned(self):
        await self.setup()
        self.assertEqual(self.entry.async_on_unload.call_count, 2)
        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        self.hass.services.async_remove.assert_any_call("resolver_inspector", "refresh_runtime")

    async def test_timer_is_five_minutes_and_skips_concurrent_work(self):
        await self.setup()
        fn, interval = self.hass.timers[0]
        self.assertEqual(interval.total_seconds(), 300)
        checked = self.entry.runtime_data["monitor"]["checked_at"]
        async with self.entry.runtime_data["lock"]:
            await fn(None)
        self.assertEqual(checked, self.entry.runtime_data["monitor"]["checked_at"])
        await fn(None)
        self.assertNotEqual(checked, self.entry.runtime_data["monitor"]["checked_at"])

    async def test_configure_periodic_and_unload_services(self):
        await self.setup()
        self.manager.configure_watchdog.side_effect = lambda enabled: self.status.update(watchdog_enabled=enabled)
        await self.call("configure_watchdog", enabled=True)
        self.assertEqual(self.entry.runtime_data["monitor"]["status"], "retry_pending")  # fixture omits file hash
        await self.call("configure_watchdog", enabled=False)
        self.assertEqual(self.entry.runtime_data["monitor"]["status"], "disabled")
        await self.integration.async_unload_entry(self.hass, self.entry)
        self.hass.services.async_remove.assert_any_call("resolver_inspector", "monitor_now")
