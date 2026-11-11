"""Hosts transactions and monitor retries against real temporary files."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1] / "custom_components/resolver_inspector"
package = ModuleType("hosts_test_package")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
from hosts_test_package.hosts import HostsManager, HostsWatchdog, repair_content
from hosts_test_package import hosts as module
from hosts_test_package.resolver import digest


class HostsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "hosts"
        self.path.write_bytes(b"")
        self.path.chmod(0o640)
        self.manager = HostsManager(Path(self.temp.name) / "backup.json", self.path)
        self.events = []
        async def inspect(): return self.manager.inspect()
        async def repair(sha): return self.manager.repair(sha)
        self.monitor = HostsWatchdog(inspect, repair, self.events.append)

    async def test_empty_file_optin_repairs_and_restores_exact_original(self):
        before = self.path.stat()
        self.manager.configure(True)
        result = await self.monitor.check()
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(self.path.read_bytes(), b"127.0.0.1 localhost\n::1 localhost\n")
        self.assertEqual(self.path.stat().st_ino, before.st_ino)
        self.assertEqual(self.path.stat().st_mode, before.st_mode)
        self.assertEqual(self.manager.backup_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([e["kind"] for e in self.events], ["problem", "recovered"])
        self.assertTrue(all(e["resource"] == "hosts" for e in self.events))
        self.manager.restore(digest(self.path.read_bytes()))
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual((await self.monitor.check())["status"], "disabled")
        self.assertFalse(self.manager.inspect()["enabled"])

    async def test_no_optin_never_writes_and_healthy_ticks_preserve_mtime(self):
        self.assertEqual((await self.monitor.check())["status"], "disabled")
        self.assertFalse(self.manager.backup_path.exists())
        self.manager.configure(True)
        await self.monitor.check()
        old = self.path.stat().st_mtime_ns
        count = len(self.events)
        for _ in range(3): await self.monitor.check()
        self.assertEqual(self.path.stat().st_mtime_ns, old)
        self.assertEqual(len(self.events), count)

    def test_aliases_comments_and_unrelated_lines_are_retained(self):
        original = (b"# local hosts\r\n192.0.2.9 other Localhost alias # comment\r\n"
                    b"127.0.0.1 localhost legacy\n::1 ip6-localhost localhost\n"
                    b"192.0.2.7\tprinter # keep byte-for-byte\n")
        result = repair_content(original)
        self.assertIn(b"192.0.2.9 other  alias # comment\r\n", result)
        self.assertIn(b"127.0.0.1  legacy\n", result)
        self.assertIn(b"::1 ip6-localhost \n", result)
        self.assertIn(b"192.0.2.7\tprinter # keep byte-for-byte\n", result)
        self.assertEqual(result.count(b" localhost\n"), 2)
        self.path.write_bytes(original)
        self.manager.repair(digest(original))
        self.manager.restore(digest(result))
        self.assertEqual(self.path.read_bytes(), original)

    def test_correct_existing_ipv6_spelling_and_duplicates_are_noop(self):
        original = b"127.0.0.1 LOCALHOST alias\n0:0:0:0:0:0:0:1 localhost\n127.0.0.1 localhost\n"
        self.assertEqual(repair_content(original), original)

    def test_bad_encoding_invalid_localhost_address_and_oversize_refused(self):
        for content in (b"\xff", b"not-an-ip localhost\n", b"#" + b"a" * 16383):
            self.path.write_bytes(content)
            with self.assertRaises((ValueError, UnicodeError)):
                self.manager.configure(True)
            self.assertEqual(self.path.read_bytes(), content)
            self.assertFalse(self.manager.backup_path.exists())

    def test_symlink_and_stale_hash_refuse_writes(self):
        self.path.write_text("# concurrent\n")
        with self.assertRaises(ValueError): self.manager.repair(digest(b""))
        self.assertEqual(self.path.read_text(), "# concurrent\n")
        target = self.path.with_name("target")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(OSError): self.manager.repair(digest(target.read_bytes()))
        self.assertEqual(target.read_text(), "# concurrent\n")

    async def test_readonly_failure_retries_once_per_tick_without_losing_original(self):
        self.manager.configure(True)
        original = self.manager.inspect()["original_sha256"]
        with patch.object(module, "write_existing", side_effect=PermissionError("read-only")) as write:
            self.assertEqual((await self.monitor.check())["status"], "retry_pending")
            self.assertEqual(write.call_count, 1)
            self.assertEqual((await self.monitor.check())["consecutive_failures"], 2)
            self.assertEqual(write.call_count, 2)
        self.assertEqual((await self.monitor.check())["status"], "healthy")
        self.assertEqual(self.manager.inspect()["original_sha256"], original)

    async def test_external_file_replacement_after_restart_is_repaired(self):
        self.manager.configure(True)
        await self.monitor.check()
        self.path.unlink()
        self.path.write_bytes(b"# replacement\n192.0.2.9 appliance\n")
        self.manager = HostsManager(self.manager.backup_path, self.path)
        self.assertEqual((await self.monitor.check())["status"], "healthy")
        self.assertIn(b"192.0.2.9 appliance\n", self.path.read_bytes())
        self.assertEqual(self.manager.inspect()["original_sha256"], digest(b""))

    async def test_interrupted_or_uncertain_write_requires_manual_action(self):
        self.manager.configure(True)
        record = json.loads(self.manager.backup_path.read_text())
        for status in ("apply_pending", "failed_check_file", "failed_rollback_required"):
            record["status"] = status
            self.manager.backup_path.write_text(json.dumps(record))
            self.assertEqual((await self.monitor.check())["status"], "manual_action_required")
            self.assertEqual(self.path.read_bytes(), b"")

    def test_corrupted_backup_cannot_overwrite_current_file(self):
        self.manager.configure(True)
        record = json.loads(self.manager.backup_path.read_text())
        record["original_b64"] = base64.b64encode(b"corrupt").decode()
        self.manager.backup_path.write_text(json.dumps(record))
        with self.assertRaises(ValueError): self.manager.repair(digest(b""))
        self.assertEqual(self.path.read_bytes(), b"")

    async def test_false_repair_success_never_emits_recovered(self):
        self.manager.configure(True)
        async def unchanged(sha): return {"result": "repaired"}
        self.monitor.repair = unchanged
        self.assertEqual((await self.monitor.check())["status"], "retry_pending")
        self.assertNotIn("recovered", [e["kind"] for e in self.events])

    async def test_concurrent_change_is_not_overwritten(self):
        self.manager.configure(True)
        async def raced(sha):
            self.path.write_bytes(b"192.0.2.9 concurrent\n")
            self.manager.repair(sha)
        self.monitor.repair = raced
        self.assertEqual((await self.monitor.check())["status"], "retry_pending")
        self.assertEqual(self.path.read_bytes(), b"192.0.2.9 concurrent\n")
