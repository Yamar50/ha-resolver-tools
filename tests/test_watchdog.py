"""Real temporary-file transactions with controlled DNS failures and ticks."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1] / "custom_components/resolver_inspector"
package = ModuleType("watchdog_test_package")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
from watchdog_test_package.resolver import ResolverManager, digest
from watchdog_test_package import resolver as resolver_module
from watchdog_test_package.watchdog import DNSWatchdog


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "resolv.conf"
        self.path.write_text("nameserver 198.51.100.53\n")
        self.manager = ResolverManager(Path(self.temp.name) / "backup.json", self.path)
        self.addCleanup(patch.stopall)
        self.probe = patch.object(resolver_module, "probe_servers", return_value=[]).start()
        self.system = patch.object(resolver_module, "system_probe", return_value=[]).start()
        self.manager.apply(["192.0.2.53"], digest(self.path.read_bytes()))
        self.manager.configure_watchdog(True)
        self.runtime = {"status": "available", "servers": ["192.0.2.53"], "ports": [53]}
        self.events = []
        self.sync_count = 0
        self.monitor = DNSWatchdog(self.inspect, self.apply, self.sync, self.events.append)
        self.probe.reset_mock()
        self.system.reset_mock()

    async def inspect(self):
        return {"inspection": {"content": self.path.read_text(), "content_sha256": digest(self.path.read_bytes())},
                "status": self.manager.status(), "runtime_dns": dict(self.runtime)}

    async def apply(self, servers, sha):
        return self.manager.apply(servers, sha)

    async def sync(self, expected):
        self.sync_count += 1
        self.runtime.update(status="available", servers=expected, ports=[53] * len(expected))

    async def test_healthy_ticks_do_not_write_probe_or_reset_caches(self):
        before = self.path.stat().st_mtime_ns
        for _ in range(3):
            self.assertEqual((await self.monitor.check())["status"], "healthy")
        self.assertEqual(before, self.path.stat().st_mtime_ns)
        self.probe.assert_not_called()
        self.system.assert_not_called()
        self.assertEqual(self.sync_count, 0)
        self.assertEqual(self.events, [])

    async def test_network_unreachable_retries_next_tick_and_reports_recovery(self):
        self.path.write_text("nameserver 198.51.100.53\n")
        before = self.path.read_bytes()
        self.probe.side_effect = OSError(101, "Network unreachable")
        self.assertEqual((await self.monitor.check())["status"], "retry_pending")
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((await self.monitor.check())["consecutive_failures"], 2)
        self.probe.side_effect = None
        self.assertEqual((await self.monitor.check())["status"], "healthy")
        self.assertEqual(self.path.read_text(), "nameserver 192.0.2.53\n")
        self.assertEqual([e["kind"] for e in self.events], ["problem", "retry_pending", "retry_pending", "recovered"])
        await self.monitor.check()
        self.assertEqual(len(self.events), 4)

    async def test_runtime_only_mismatch_keeps_file_and_reports_only_after_readback(self):
        before = self.path.stat().st_mtime_ns
        self.runtime["servers"] = ["198.51.100.53"]
        await self.monitor.check()
        self.assertEqual(before, self.path.stat().st_mtime_ns)
        self.assertEqual(self.sync_count, 1)
        self.probe.assert_not_called()
        self.assertEqual([e["kind"] for e in self.events], ["problem", "recovered"])

    async def test_runtime_refresh_failure_keeps_applied_file_and_retries_runtime(self):
        self.path.write_text("nameserver 198.51.100.53\n")
        async def fail(expected):
            raise RuntimeError("runtime unavailable")
        self.monitor.sync_runtime = fail
        self.assertEqual((await self.monitor.check())["status"], "retry_pending")
        self.assertEqual(self.path.read_text(), "nameserver 192.0.2.53\n")
        self.runtime["servers"] = ["198.51.100.53"]
        self.monitor.sync_runtime = self.sync
        self.probe.reset_mock()
        self.assertEqual((await self.monitor.check())["status"], "healthy")
        self.probe.assert_not_called()

    async def test_false_refresh_success_is_not_reported_as_recovery(self):
        self.runtime["ports"] = [5353]
        async def no_change(expected):
            return {"result": "refreshed"}
        self.monitor.sync_runtime = no_change
        self.assertEqual((await self.monitor.check())["status"], "retry_pending")
        self.assertNotIn("recovered", [e["kind"] for e in self.events])

    async def test_confirmed_rollback_can_retry_but_uncertain_write_cannot(self):
        self.path.write_text("nameserver 198.51.100.53\n")
        self.system.side_effect = OSError("Temporary lookup failure")
        await self.monitor.check()
        self.assertEqual(self.manager.status()["last_result"], "failed_rolled_back")
        self.system.side_effect = None
        self.assertEqual((await self.monitor.check())["status"], "healthy")
        record = json.loads(self.manager.backup_path.read_text())
        for status in ("apply_pending", "failed_rollback_required", "failed_check_file"):
            record["status"] = status
            self.manager.backup_path.write_text(json.dumps(record))
            self.assertEqual((await self.monitor.check())["status"], "manual_action_required")

    async def test_restore_and_disable_prevent_future_writes(self):
        self.manager.configure_startup(True)
        self.manager.restore(digest(self.path.read_bytes()))
        self.assertFalse(self.manager.status()["watchdog_enabled"])
        self.assertEqual((await self.monitor.check())["status"], "disabled")
        self.assertEqual(self.path.read_text(), "nameserver 198.51.100.53\n")

    async def test_reload_keeps_optin_target_and_original_backup(self):
        original = self.manager.status()["original_sha256"]
        self.manager = ResolverManager(self.manager.backup_path, self.path)
        self.assertTrue(self.manager.status()["watchdog_enabled"])
        self.path.write_text("nameserver 198.51.100.53\n")
        await self.monitor.check()
        self.assertEqual(original, self.manager.status()["original_sha256"])

    async def test_stale_inspection_refuses_overwriting_concurrent_change(self):
        self.path.write_text("nameserver 198.51.100.53\n")
        async def concurrent(servers, sha):
            self.path.write_text("nameserver 203.0.113.53\n")
            return await self.apply(servers, sha)
        self.monitor.apply = concurrent
        self.assertEqual((await self.monitor.check())["status"], "retry_pending")
        self.assertEqual(self.path.read_text(), "nameserver 203.0.113.53\n")

    async def test_read_error_keeps_timer_callable_for_next_attempt(self):
        original = self.monitor.inspect
        async def broken():
            raise OSError("Temporarily unavailable")
        self.monitor.inspect = broken
        self.assertEqual((await self.monitor.check())["status"], "retry_pending")
        self.monitor.inspect = original
        self.assertEqual((await self.monitor.check())["status"], "healthy")

    async def test_initial_observation_defers_retry_until_first_tick(self):
        self.path.write_text("nameserver 198.51.100.53\n")
        self.assertEqual((await self.monitor.check(repair=False))["status"], "retry_pending")
        self.probe.assert_not_called()
        self.assertEqual((await self.monitor.check())["status"], "healthy")

    async def test_new_install_requires_explicit_optin(self):
        self.manager.configure_watchdog(False)
        self.path.write_text("nameserver 198.51.100.53\n")
        self.assertEqual((await self.monitor.check())["status"], "disabled")
        self.probe.assert_not_called()
