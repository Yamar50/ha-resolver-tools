import base64
import importlib.util
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).parents[1] / 'custom_components/resolver_inspector/resolver.py'
spec = importlib.util.spec_from_file_location('resolver_manager_under_test', path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ResolverChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'resolv.conf'
        self.original = b'# keep\nsearch example.test\noptions timeout:2\nnameserver 198.51.100.53\n'
        self.file.write_bytes(self.original)
        self.file.chmod(0o640)
        self.backup = self.root / '.storage/backup.json'
        self.manager = m.ResolverManager(self.backup, self.file)
        self.addCleanup(patch.stopall)
        patch.object(m, 'probe_servers', return_value=[{'server': '192.0.2.53'}]).start()
        patch.object(m, 'system_probe', return_value=[{'addresses': ['192.0.2.1']}]).start()

    def apply(self):
        return self.manager.apply(['192.0.2.53'], m.digest(self.file.read_bytes()))

    def test_apply_preserves_inode_mode_and_other_directives(self):
        before = self.file.stat()
        result = self.apply()
        self.assertEqual(result['result'], 'applied')
        self.assertEqual(self.file.read_bytes(), self.original.replace(b'198.51.100.53', b'192.0.2.53'))
        after = self.file.stat()
        self.assertEqual((before.st_ino, before.st_mode, before.st_uid, before.st_gid),
                         (after.st_ino, after.st_mode, after.st_uid, after.st_gid))
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)

    def test_second_apply_preserves_first_original_and_exact_restore(self):
        self.apply()
        self.manager.apply(['203.0.113.53'], m.digest(self.file.read_bytes()))
        record = json.loads(self.backup.read_text())
        self.assertEqual(base64.b64decode(record['original_b64']), self.original)
        self.manager.restore(m.digest(self.file.read_bytes()))
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_new_manager_never_reapplies_candidate_or_original(self):
        self.apply()
        self.file.write_bytes(b'nameserver 203.0.113.9\n')
        before = self.file.stat()
        restarted = m.ResolverManager(self.backup, self.file)
        status = restarted.status()
        self.assertFalse(status['startup_reapply'])
        self.assertEqual(self.file.read_bytes(), b'nameserver 203.0.113.9\n')
        self.assertEqual(before.st_mtime_ns, self.file.stat().st_mtime_ns)

    def test_stale_hash_refuses_changes(self):
        with self.assertRaises(ValueError):
            self.manager.apply(['192.0.2.53'], '0' * 64)
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_invalid_server_refuses_without_writes(self):
        for values in [['bad'], ['127.0.0.1'], ['224.0.0.1'], [], ['192.0.2.53'] * 2, ['192.0.2.53\nnameserver 8.8.8.8']]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.manager.apply(values, m.digest(self.original))
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_network_preflight_failure_does_not_touch_resolver(self):
        with patch.object(m, 'probe_servers', side_effect=TimeoutError), self.assertRaises(TimeoutError):
            self.apply()
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertFalse(self.backup.exists())

    def test_backup_failure_does_not_touch_resolver(self):
        with patch.object(self.manager, '_save', side_effect=OSError), self.assertRaises(OSError):
            self.apply()
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_resolution_failure_rolls_back_previous_bytes(self):
        with patch.object(m, 'system_probe', side_effect=OSError), self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertEqual(self.manager.status()['last_result'], 'failed_rolled_back')

    def test_partial_write_failure_restores_original(self):
        real_write = os.write
        calls = 0
        def interrupted(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:8])
            if calls == 2:
                raise OSError('synthetic write failure')
            return real_write(fd, data)
        with patch.object(m.os, 'write', side_effect=interrupted), self.assertRaises(OSError):
            m.write_existing(self.file, m.digest(self.original), b'nameserver 192.0.2.53\n')
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_symlink_refused(self):
        link = self.root / 'link'
        link.symlink_to(self.file)
        with self.assertRaises(OSError):
            m.ResolverManager(self.backup, link).apply(['192.0.2.53'], m.digest(self.original))
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_corrupt_backup_refuses_write(self):
        self.apply()
        current = self.file.read_bytes()
        record = json.loads(self.backup.read_text())
        record['original_sha256'] = '0' * 64
        self.backup.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            self.manager.restore(m.digest(current))
        self.assertEqual(self.file.read_bytes(), current)


if __name__ == '__main__':
    unittest.main()
