"""Refresh HA's existing shared resolver without replacing sessions or servers.

Only c-ares' system-configuration reload is requested. Explicit application
server overrides are preserved by c-ares and reported as a mismatch, never
overwritten here. HA/aiohttp internals are capability-checked at the boundary.
"""

import asyncio
from datetime import datetime, timezone
import ipaddress
import socket


class RuntimeRefreshError(RuntimeError):
    """A file change may have succeeded while the runtime refresh did not."""

    def __init__(self, report):
        self.report = report
        super().__init__(f"HA DNS runtime refresh: {report['result']}")


def _endpoint(value):
    """Parse both pycares 4 IPs and pycares 5 IP:port/[IPv6]:port."""
    value = str(value)
    try:
        return str(ipaddress.ip_address(value)), 53
    except ValueError:
        pass
    if value.startswith("["):
        address, separator, port = value[1:].partition("]:")
    else:
        address, separator, port = value.rpartition(":")
    if not separator or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Unsupported runtime DNS endpoint")
    return str(ipaddress.ip_address(address)), int(port)


def inspect_runtime(resolver):
    """Read the existing channel only; never create a resolver or change it."""
    if resolver is None:
        return {"status": "not_initialized", "servers": []}
    channel = getattr(getattr(resolver, "_resolver", None), "_channel", None)
    if channel is None or not callable(getattr(channel, "reinit", None)):
        return {"status": "unsupported", "servers": [],
                "resolver_class": type(resolver).__name__}
    try:
        endpoints = [_endpoint(value) for value in channel.servers]
    except Exception as error:
        return {"status": "unavailable", "servers": [], "error": type(error).__name__}
    return {"status": "available", "servers": [ip for ip, _ in endpoints],
            "ports": [port for _, port in endpoints],
            "resolver_class": type(resolver).__name__}


async def refresh_runtime(resolver, servers, connectors=(), *, timeout=3.0):
    """Re-read system DNS and verify the existing channel before clearing caches."""
    expected = [str(ipaddress.ip_address(value)) for value in servers]
    before = inspect_runtime(resolver)
    report = {"result": "pending", "expected_servers": expected,
              "servers_before": before["servers"], "servers_after": [],
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "reinit_requested": False, "connectors_cleared": 0}
    if before["status"] == "not_initialized":
        # A future resolver will initialize from the updated file. This is not
        # reported as verification of a resolver which does not yet exist.
        return {**report, "result": "not_initialized"}
    if before["status"] != "available" or not expected:
        report["result"] = "unsupported" if expected else "no_explicit_servers"
        raise RuntimeRefreshError(report)
    channel = resolver._resolver._channel
    try:
        channel.reinit()  # c-ares safely requeues queries; preserves custom overrides.
        report["reinit_requested"] = True
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            current = inspect_runtime(resolver)
            report["servers_after"] = current["servers"]
            if (current["status"] == "available" and current["servers"] == expected
                    and current["ports"] == [53] * len(expected)):
                break
            if asyncio.get_running_loop().time() >= deadline:
                report["result"] = "mismatch"
                raise RuntimeRefreshError(report)
            await asyncio.sleep(0.05)
        seen = set()
        for connector in connectors:
            if id(connector) in seen or getattr(connector, "_resolver", None) is not resolver:
                continue
            seen.add(id(connector))
            connector.clear_dns_cache()  # Does not close open connections/sessions.
            report["connectors_cleared"] += 1
    except RuntimeRefreshError:
        raise
    except Exception as error:
        report.update(result="failed", error=type(error).__name__)
        raise RuntimeRefreshError(report) from error
    report["result"] = "already_matches" if before["servers"] == expected and before["ports"] == [53] * len(expected) else "refreshed"
    return report


async def probe_runtime(resolver, names):
    """Resolve through HA's real shared resolver, without making HTTP requests."""
    if resolver is None:
        return {"result": "not_initialized", "checks": []}
    results = []
    for name in names:
        try:
            async with asyncio.timeout(5):
                answers = await resolver.resolve(name, 0, socket.AF_UNSPEC)
            results.append({"name": name, "success": bool(answers),
                            "answer_count": len(answers)})
        except Exception as error:
            results.append({"name": name, "success": False, "error": type(error).__name__})
    return {"result": "passed" if all(r["success"] for r in results) else "failed",
            "checks": results}
