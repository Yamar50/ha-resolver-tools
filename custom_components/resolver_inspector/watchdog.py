"""Compare saved/file/runtime DNS and repair once per scheduled check."""

from datetime import datetime, timezone
import ipaddress

from .resolver import nameservers, normalize_servers

INTERVAL_SECONDS = 300
STATES = ["disabled", "pending", "healthy", "retry_pending", "manual_action_required"]


def addresses(content):
    return [str(ipaddress.ip_address(s)) for s in nameservers(content)]


def matches(snapshot, expected):
    inspection, runtime = snapshot["inspection"], snapshot["runtime_dns"]
    try:
        file_matches = bool(inspection.get("content_sha256")) and addresses(inspection.get("content", "")) == expected
    except ValueError:
        file_matches = False
    runtime_matches = (runtime.get("status") == "available"
                       and runtime.get("servers") == expected
                       and runtime.get("ports") == [53] * len(expected))
    return file_matches, runtime_matches


class DNSWatchdog:
    """Caller holds the integration lock, including manual apply/restore actions."""

    def __init__(self, inspect, apply, sync_runtime, emit):
        self.inspect = inspect
        self.apply = apply
        self.sync_runtime = sync_runtime
        self.emit = emit
        self.problem = False
        self.state = {"status": "pending", "interval_seconds": INTERVAL_SECONDS,
                      "checked_at": None, "last_recovered_at": None,
                      "consecutive_failures": 0, "error": None}

    def event(self, kind, **details):
        self.emit({"kind": kind, **self.state, **details})

    async def check(self, *, repair=True):
        self.state.update(checked_at=datetime.now(timezone.utc).isoformat(), error=None)
        try:
            snapshot = await self.inspect()
            status = snapshot["status"]
            if not status.get("watchdog_enabled", False):
                self.state.update(status="disabled", consecutive_failures=0)
                self.problem = False
                return dict(self.state)
            expected = normalize_servers(status.get("saved_servers"))
            self.state["expected_servers"] = expected
            # Retry a confirmed rollback, never an interrupted/uncertain write.
            if status.get("last_result") not in ("applied", "failed_rolled_back"):
                self.state.update(status="manual_action_required", error="Previous DNS write requires manual inspection")
                if not self.problem:
                    self.problem = True
                    self.event("problem")
                return dict(self.state)
            file_matches, runtime_matches = matches(snapshot, expected)
            if not (file_matches and runtime_matches):
                self.state["status"] = "retry_pending"
                if not self.problem:
                    self.problem = True
                    self.event("problem", file_matches=file_matches, runtime_matches=runtime_matches)
                if not repair:
                    return dict(self.state)
                if not file_matches:
                    digest = snapshot["inspection"].get("content_sha256")
                    if not digest:
                        raise ValueError("Resolver file cannot be inspected safely")
                    await self.apply(expected, digest)
                if not file_matches or not runtime_matches:
                    await self.sync_runtime(expected)
                snapshot = await self.inspect()
                if matches(snapshot, expected) != (True, True):
                    raise ValueError("Saved, file and running DNS do not match after repair")
            self.state.update(status="healthy", consecutive_failures=0)
            if self.problem:
                self.problem = False
                self.state["last_recovered_at"] = datetime.now(timezone.utc).isoformat()
                self.event("recovered")
        except Exception as error:
            # A failed attempt never spawns an immediate retry loop. The next
            # timer/manual check will re-read policy and target before retrying.
            self.problem = True
            self.state.update(status="retry_pending", error=f"{type(error).__name__}: {error}",
                              consecutive_failures=self.state["consecutive_failures"] + 1)
            self.event("retry_pending")
        return dict(self.state)
