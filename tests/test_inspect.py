import importlib.util
from pathlib import Path
import tempfile
import unittest

module_path = Path(__file__).parents[1] / 'custom_components/resolver_inspector/inspect.py'
spec = importlib.util.spec_from_file_location('resolver_inspection_under_test', module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResolverInspectionTests(unittest.TestCase):
    def test_inspection_does_not_change_bytes_mode_or_mtime(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'resolv.conf'
            content = b'nameserver 192.0.2.53\nnameserver 198.51.100.53\n'
            path.write_bytes(content)
            path.chmod(0o600)
            before = path.stat()
            result = module._inspect_path(path)
            after = path.stat()
            self.assertEqual(result['content'], content.decode())
            self.assertEqual(result['mode_octal'], '0600')
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual((before.st_mode, before.st_mtime_ns), (after.st_mode, after.st_mtime_ns))
            self.assertFalse(result['write_attempted'])

    def test_symlink_reports_target_permissions_without_replacing_link(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'managed.conf'
            target.write_text('nameserver 192.0.2.53\n')
            target.chmod(0o640)
            link = Path(root) / 'resolv.conf'
            link.symlink_to(target)
            result = module._inspect_path(link)
            self.assertTrue(result['is_symlink'])
            self.assertEqual(result['resolved_path'], str(target.resolve()))
            self.assertEqual(result['mode_octal'], '0640')
            self.assertTrue(link.is_symlink())

    def test_missing_file_is_not_created(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'absent'
            result = module._inspect_path(path)
            self.assertEqual(result['error_type'], 'FileNotFoundError')
            self.assertFalse(path.exists())

    def test_oversized_contents_are_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'resolv.conf'
            path.write_bytes(b'x' * 32768)
            result = module._inspect_path(path)
            self.assertEqual(len(result['content']), 16384)
            self.assertTrue(result['content_truncated'])
            self.assertIsNone(result['content_sha256'])


if __name__ == '__main__':
    unittest.main()
