"""Fixed-path localhost repair, preserving unrelated hosts entries."""

import base64
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile

from .resolver import MAX_BYTES, digest, read_regular, write_existing

LOCALHOST_ADDRESSES = {"127.0.0.1", "::1"}
LOCALHOST_TOKEN = re.compile(r"(?<!\S)localhost(?!\S)", re.IGNORECASE)


def check_system_localhost():
    addresses = sorted({str(ipaddress.ip_address(row[4][0]))
                        for row in socket.getaddrinfo("localhost", 0, type=socket.SOCK_STREAM)})
    return {"success": set(addresses) == LOCALHOST_ADDRESSES, "addresses": addresses}


def localhost_addresses(data):
    addresses = []
    for line in data.decode("utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) > 1 and any(name.lower() == "localhost" for name in fields[1:]):
            addresses.append(str(ipaddress.ip_address(fields[0])))
    return addresses


def repair_content(data):
    """Keep non-localhost aliases/comments; replace only localhost mappings."""
    if set(localhost_addresses(data)) == LOCALHOST_ADDRESSES:
        return data
    kept = []
    for line in data.decode("utf-8").splitlines(keepends=True):
        main, marker, comment = line.partition("#")
        fields = main.split()
        if len(fields) < 2 or not any(name.lower() == "localhost" for name in fields[1:]):
            kept.append(line)
            continue
        # Parsing above refuses malformed addresses instead of guessing at them.
        remaining = LOCALHOST_TOKEN.sub("", main)
        if len(remaining.split()) > 1:
            kept.append(remaining + marker + comment)
        elif marker:
            kept.append(marker + comment)
    text = "".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    result = (text + "127.0.0.1 localhost\n::1 localhost\n").encode()
    if len(result) > MAX_BYTES:
        raise ValueError("Hosts file would exceed 16 KiB")
    return result


class HostsManager:
    def __init__(self, backup_path, hosts_path=Path("/etc/hosts")):
        self.backup_path = Path(backup_path)
        self.path = Path(hosts_path)

    def _load(self):
        if not self.backup_path.exists():
            return None
        raw = self.backup_path.read_bytes()
        if len(raw) > 65536:
            raise ValueError("Hosts backup is too large")
        record = json.loads(raw)
        original = base64.b64decode(record["original_b64"], validate=True)
        if len(original) > MAX_BYTES or digest(original) != record["original_sha256"]:
            raise ValueError("Hosts backup integrity check failed")
        return record

    def _save(self, record):
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="hosts-backup-", dir=self.backup_path.parent)
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

    def _record(self, before):
        return self._load() or {"schema_version": 1,
                               "original_b64": base64.b64encode(before).decode(),
                               "original_sha256": digest(before),
                               "status": "observed", "enabled": False}

    def inspect(self):
        record = self._load() or {}
        data = read_regular(self.path)
        addresses = localhost_addresses(data)
        return {"path": str(self.path), "content": data.decode("utf-8"),
                "content_sha256": digest(data), "size_bytes": len(data),
                "localhost_addresses": addresses,
                "matches": set(addresses) == LOCALHOST_ADDRESSES,
                "backup_available": bool(record), "enabled": record.get("enabled", False),
                "last_result": record.get("status", "never_applied"),
                "original_sha256": record.get("original_sha256")}

    def configure(self, enabled):
        record = self._load()
        if not enabled and not record:
            return
        if enabled:
            before = read_regular(self.path)
            repair_content(before)  # Refuse malformed/oversized content before opting in.
            record = record or self._record(before)
            if record["status"] not in ("observed", "applied", "restored", "failed_rolled_back"):
                raise ValueError("Previous hosts write requires manual inspection")
        record["enabled"] = enabled
        self._save(record)

    def repair(self, expected_sha256):
        before = read_regular(self.path)
        if digest(before) != expected_sha256:
            raise ValueError("Hosts changed since inspection; inspect again")
        replacement = repair_content(before)
        record = self._record(before)
        record.update(status="apply_pending", updated_at=datetime.now(timezone.utc).isoformat())
        self._save(record)
        try:
            if replacement != before:
                write_existing(self.path, expected_sha256, replacement)
            if read_regular(self.path) != replacement:
                raise ValueError("Hosts readback mismatch")
        except Exception:
            try:
                current = read_regular(self.path)
                if current == replacement and current != before:
                    write_existing(self.path, digest(current), before)
                record["status"] = "failed_rolled_back" if read_regular(self.path) == before else "failed_check_file"
            except Exception:
                record["status"] = "failed_rollback_required"
            self._save(record)
            raise
        record["status"] = "applied"
        self._save(record)
        return {"result": "repaired" if replacement != before else "already_matches",
                "sha256": digest(replacement), "localhost_addresses": sorted(LOCALHOST_ADDRESSES)}

    def restore(self, expected_sha256):
        record = self._load()
        if not record:
            raise ValueError("No original hosts backup exists")
        if digest(read_regular(self.path)) != expected_sha256:
            raise ValueError("Hosts changed since inspection; inspect again")
        record.update(enabled=False, status="restore_pending")
        self._save(record)
        original = base64.b64decode(record["original_b64"], validate=True)
        write_existing(self.path, expected_sha256, original)
        record.update(status="restored", updated_at=datetime.now(timezone.utc).isoformat())
        self._save(record)
        return {"result": "restored", "sha256": digest(original), "enabled": False}


class HostsWatchdog:
    """Check the fixed localhost mappings; no network lookups during monitoring."""

    def __init__(self, inspect, repair, emit):
        self.inspect, self.repair, self.emit = inspect, repair, emit
        self.problem = False
        self.state = {"status": "pending", "interval_seconds": 300,
                      "checked_at": None, "last_recovered_at": None,
                      "consecutive_failures": 0, "error": None}

    def event(self, kind):
        self.emit({"resource": "hosts", "kind": kind, **self.state})

    async def check(self):
        self.state.update(checked_at=datetime.now(timezone.utc).isoformat(), error=None)
        try:
            snapshot = await self.inspect()
            if snapshot.get("error"):
                raise ValueError(snapshot["error"])
            if not snapshot.get("enabled"):
                self.state.update(status="disabled", consecutive_failures=0)
                self.problem = False
                return dict(self.state)
            if snapshot["last_result"] not in ("observed", "applied", "restored", "failed_rolled_back"):
                self.state.update(status="manual_action_required", error="Previous hosts write requires manual inspection")
                if not self.problem:
                    self.problem = True
                    self.event("problem")
                return dict(self.state)
            if not snapshot["matches"]:
                self.state["status"] = "retry_pending"
                if not self.problem:
                    self.problem = True
                    self.event("problem")
                await self.repair(snapshot["content_sha256"])
                snapshot = await self.inspect()
                if not snapshot.get("matches"):
                    raise ValueError("Localhost mappings still differ after repair")
            self.state.update(status="healthy", consecutive_failures=0)
            if self.problem:
                self.problem = False
                self.state["last_recovered_at"] = datetime.now(timezone.utc).isoformat()
                self.event("recovered")
        except Exception as error:
            self.problem = True
            self.state.update(status="retry_pending", error=f"{type(error).__name__}: {error}",
                              consecutive_failures=self.state["consecutive_failures"] + 1)
            self.event("retry_pending")
        return dict(self.state)
