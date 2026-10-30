"""Read resolver metadata using the Home Assistant process's existing rights."""

import hashlib
import os
from pathlib import Path
import stat

RESOLVER_PATH = Path('/etc/resolv.conf')
MAX_BYTES = 16384


def _inspect_path(path):
    result = {
        'path': str(path),
        'effective_uid': os.geteuid(),
        'process_id': os.getpid(),
        'effective_gid': os.getegid(),
        'supplementary_groups': os.getgroups(),
        'configuration_changed': False,
        'write_attempted': False,
    }
    try:
        link = path.lstat()
        target = path.stat()
        result.update(
            is_symlink=stat.S_ISLNK(link.st_mode),
            resolved_path=str(path.resolve()),
            mode=stat.filemode(target.st_mode),
            mode_octal=f'{stat.S_IMODE(target.st_mode):04o}',
            owner_uid=target.st_uid,
            owner_gid=target.st_gid,
            size_bytes=target.st_size,
            mtime_ns=target.st_mtime_ns,
            inode=target.st_ino,
            device=target.st_dev,
            filesystem_read_only=bool(os.statvfs(path).f_flag & os.ST_RDONLY),
        )
        if os.access in os.supports_effective_ids:
            result['effective_user_can_read'] = os.access(path, os.R_OK, effective_ids=True)
            result['effective_user_can_write'] = os.access(path, os.W_OK, effective_ids=True)
        else:
            result['effective_user_can_write'] = None
            result['access_check_note'] = 'effective-ID access check unavailable'
        if not stat.S_ISREG(target.st_mode):
            result['read_skipped'] = 'not a regular file'
            return result
        with path.open('rb') as file:
            data = file.read(MAX_BYTES + 1)
        result['content_truncated'] = len(data) > MAX_BYTES
        result['content'] = data[:MAX_BYTES].decode('utf-8', errors='replace')
        result['content_sha256'] = None if result['content_truncated'] else hashlib.sha256(data).hexdigest()
    except OSError as error:
        result['error_type'] = type(error).__name__
        result['errno'] = error.errno
    return result


def inspect_resolver():
    """The public action accepts no path and performs no writes or subprocesses."""
    return _inspect_path(RESOLVER_PATH)
