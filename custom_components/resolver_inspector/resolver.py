"""Fixed-path DNS changes with a durable original backup and explicit rollback."""

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import tempfile
from datetime import datetime, timezone

MAX_BYTES = 16384
PROBE_NAMES = ("api.switch-bot.com", "www.home-assistant.io")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalize_servers(values):
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 3:
        raise ValueError("Specify one to three DNS IP addresses")
    result = []
    for value in values:
        address = ipaddress.ip_address(str(value).strip())
        if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local:
            raise ValueError("Use a reachable unicast DNS IP address")
        text = str(address)
        if text in result:
            raise ValueError("Duplicate DNS server")
        result.append(text)
    return result


def nameservers(content):
    return re.findall(r"^\s*nameserver\s+(\S+)", content, re.MULTILINE)


def render_config(original, servers):
    text = original.decode("utf-8", errors="strict")
    kept = [line for line in text.splitlines(keepends=True)
            if not re.match(r"^\s*nameserver(?:\s|$)", line)]
    result = "".join(kept)
    if result and not result.endswith("\n"):
        result += "\n"
    result += "".join(f"nameserver {server}\n" for server in normalize_servers(servers))
    data = result.encode()
    if len(data) > MAX_BYTES:
        raise ValueError("Resolver file is too large")
    return data


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("Resolver must be a regular, non-symlink file")
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Resolver file is too large")
    return data


def _write_fd(fd, data):
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("Short resolver write")
        view = view[count:]
    os.ftruncate(fd, len(data))
    os.fsync(fd)


def write_existing(path, expected, replacement):
    """Keep the inode/mount/ownership. Restore old bytes if the write fails."""
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Resolver is not a regular file")
        before = os.read(fd, MAX_BYTES + 1)
        if len(before) > MAX_BYTES or digest(before) != expected:
            raise ValueError("Resolver changed since inspection; inspect again")
        try:
            _write_fd(fd, replacement)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, MAX_BYTES + 1) != replacement:
                raise OSError("Resolver readback mismatch")
        except Exception:
            _write_fd(fd, before)
            raise
    finally:
        os.close(fd)


def direct_probe(server, hostname):
    """One bounded A lookup to a literal server; no shell or external package."""
    address = ipaddress.ip_address(server)
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in hostname.split(".")) + b"\0\0\1\0\1"
    ident = secrets.randbelow(65536)
    packet = struct.pack("!6H", ident, 0x100, 1, 0, 0, 0) + question
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.connect((server, 53))
        sock.send(packet)
        answer = sock.recv(4096)
    if len(answer) < 12:
        raise ValueError("Short DNS reply")
    reply_id, flags, qd, an, _, _ = struct.unpack("!6H", answer[:12])
    if reply_id != ident or not flags & 0x8000 or flags & 0x020f or qd != 1 or an == 0:
        raise ValueError("DNS lookup failed or needs TCP")
    if answer[12:12 + len(question)].lower() != question.lower():
        raise ValueError("Mismatched DNS question")
    return {"server": server, "name": hostname, "response_code": 0, "answer_count": an}


def probe_servers(servers):
    return [direct_probe(server, name) for server in servers for name in PROBE_NAMES]


def system_probe():
    result = []
    for name in PROBE_NAMES:
        values = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        addresses = sorted({value[4][0] for value in values})
        if not addresses:
            raise ValueError("System resolver returned no addresses")
        result.append({"name": name, "addresses": addresses})
    return result


def mount_info(path=Path("/etc/resolv.conf")):
    """Report the most specific Linux mount containing the fixed resolver path."""
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return {"available": False}
    candidates = []
    for line in lines:
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        if not separator or len(left) < 6 or len(right) < 3:
            continue
        point = left[4].replace("\\040", " ").replace("\\134", "\\")
        if str(path) == point or str(path).startswith(point.rstrip("/") + "/"):
            candidates.append({"available": True, "mount_point": point,
                               "mount_root": left[3], "mount_options": left[5],
                               "filesystem_type": right[0], "mount_source": right[1],
                               "super_options": right[2]})
    return max(candidates, key=lambda item: len(item["mount_point"]), default={"available": False})


class ResolverManager:
    def __init__(self, backup_path, resolver_path=Path("/etc/resolv.conf")):
        self.backup_path = Path(backup_path)
        self.path = Path(resolver_path)

    def _load(self):
        if not self.backup_path.exists():
            return None
        with self.backup_path.open("r") as file:
            data = file.read(65537)
        if len(data) > 65536:
            raise ValueError("Backup is too large")
        record = json.loads(data)
        original = base64.b64decode(record["original_b64"], validate=True)
        if len(original) > MAX_BYTES or digest(original) != record["original_sha256"]:
            raise ValueError("Backup integrity check failed")
        return record

    def _save(self, record):
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="resolver-backup-", dir=self.backup_path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(record, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.backup_path)
            directory = os.open(self.backup_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def status(self):
        record = self._load()
        if not record:
            return {"backup_available": False, "last_result": "never_applied", "startup_reapply": False}
        return {"backup_available": True, "last_result": record.get("status"),
                "original_sha256": record["original_sha256"],
                "last_applied_sha256": record.get("applied_sha256"),
                "updated_at": record.get("updated_at"), "startup_reapply": False}

    def apply(self, servers, expected_sha256):
        servers = normalize_servers(servers)
        before = read_regular(self.path)
        if digest(before) != expected_sha256:
            raise ValueError("Resolver changed since inspection; inspect again")
        replacement = render_config(before, servers)
        preflight = probe_servers(servers)
        record = self._load() or {"schema_version": 1, "original_b64": base64.b64encode(before).decode(),
                                  "original_sha256": digest(before)}
        record.update(status="apply_pending", applied_sha256=digest(replacement),
                      updated_at=datetime.now(timezone.utc).isoformat())
        self._save(record)  # Durable original before touching the resolver.
        changed = False
        try:
            write_existing(self.path, expected_sha256, replacement)
            changed = True
            resolved = system_probe()
            record["status"] = "applied"
            self._save(record)
        except Exception as error:
            if changed:
                try:
                    write_existing(self.path, digest(replacement), before)
                    record["status"] = "failed_rolled_back"
                except Exception:
                    record["status"] = "failed_rollback_required"
            else:
                record["status"] = "failed_check_file"
            self._save(record)
            raise ValueError(record["status"]) from error
        return {"result": "applied", "servers": servers, "sha256": digest(replacement),
                "direct_dns_checks": preflight, "system_dns_checks": resolved,
                "startup_reapply": False}

    def restore(self, expected_sha256):
        record = self._load()
        if not record:
            raise ValueError("No original backup exists")
        original = base64.b64decode(record["original_b64"], validate=True)
        write_existing(self.path, expected_sha256, original)
        record.update(status="restored", updated_at=datetime.now(timezone.utc).isoformat())
        self._save(record)
        return {"result": "restored", "sha256": digest(original), "startup_reapply": False}
