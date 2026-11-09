"""Regressions for stale channels, async reload, overrides and connector lifetime."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

path = Path(__file__).parents[1] / "custom_components/resolver_inspector/runtime.py"
spec = importlib.util.spec_from_file_location("runtime_under_test", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeChannel:
    def __init__(self, servers, target=None, delay=0):
        self.servers = servers
        self.target = target if target is not None else servers
        self.delay = delay
        self.reloads = 0

    def reinit(self):
        self.reloads += 1
        asyncio.get_running_loop().call_later(self.delay, self._loaded)

    def _loaded(self):
        self.servers = self.target


def resolver(channel):
    return SimpleNamespace(_resolver=SimpleNamespace(_channel=channel))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_channel_reloads_then_only_own_connector_caches_clear(self):
        c = FakeChannel(["198.51.100.53:53"], ["192.0.2.53:53"], delay=0.05)
        r = resolver(c)
        own = SimpleNamespace(_resolver=r, clear_dns_cache=Mock(), close=Mock())
        custom = SimpleNamespace(_resolver=object(), clear_dns_cache=Mock(), close=Mock())
        result = await m.refresh_runtime(r, ["192.0.2.53"], [own, custom, own])
        self.assertEqual(result["result"], "refreshed")
        self.assertEqual(result["servers_before"], ["198.51.100.53"])
        self.assertEqual(result["servers_after"], ["192.0.2.53"])
        self.assertIs(r._resolver._channel, c)
        own.clear_dns_cache.assert_called_once()
        custom.clear_dns_cache.assert_not_called()
        own.close.assert_not_called()
        custom.close.assert_not_called()

    async def test_matching_file_still_requests_runtime_reinit(self):
        c = FakeChannel(["192.0.2.53:53"])
        result = await m.refresh_runtime(resolver(c), ["192.0.2.53"])
        self.assertEqual(c.reloads, 1)
        self.assertEqual(result["result"], "already_matches")

    async def test_explicit_override_is_not_forcibly_replaced(self):
        c = FakeChannel(["198.51.100.53:53"])
        with self.assertRaises(m.RuntimeRefreshError) as raised:
            await m.refresh_runtime(resolver(c), ["192.0.2.53"], timeout=0)
        self.assertEqual(raised.exception.report["result"], "mismatch")
        self.assertEqual(c.servers, ["198.51.100.53:53"])

    async def test_nonstandard_dns_port_does_not_falsely_verify(self):
        c = FakeChannel(["192.0.2.53:5353"])
        with self.assertRaises(m.RuntimeRefreshError) as raised:
            await m.refresh_runtime(resolver(c), ["192.0.2.53"], timeout=0)
        self.assertEqual(raised.exception.report["result"], "mismatch")

    async def test_ipv6_and_older_plain_ip_channel_formats(self):
        for value in ("[2001:db8::53]:53", "2001:db8:0:0::53"):
            with self.subTest(value=value):
                result = await m.refresh_runtime(resolver(FakeChannel([value])), ["2001:db8::53"])
                self.assertEqual(result["result"], "already_matches")

    async def test_order_is_preserved_and_verified(self):
        c = FakeChannel(["192.0.2.54:53", "192.0.2.53:53"])
        with self.assertRaises(m.RuntimeRefreshError):
            await m.refresh_runtime(resolver(c), ["192.0.2.53", "192.0.2.54"], timeout=0)

    async def test_read_only_inspection_does_not_reinit(self):
        c = FakeChannel(["192.0.2.53:53"])
        self.assertEqual(m.inspect_runtime(resolver(c))["servers"], ["192.0.2.53"])
        self.assertEqual(c.reloads, 0)

    async def test_not_initialized_does_not_create_or_claim_verified(self):
        result = await m.refresh_runtime(None, ["192.0.2.53"])
        self.assertEqual(result["result"], "not_initialized")
        self.assertFalse(result["reinit_requested"])

    async def test_unsupported_api_fails_explicitly(self):
        with self.assertRaises(m.RuntimeRefreshError) as raised:
            await m.refresh_runtime(object(), ["192.0.2.53"])
        self.assertEqual(raised.exception.report["result"], "unsupported")

    async def test_reinit_error_does_not_close_or_clear_connections(self):
        c = FakeChannel(["198.51.100.53:53"])
        c.reinit = Mock(side_effect=RuntimeError("reload error"))
        r = resolver(c)
        connector = SimpleNamespace(_resolver=r, clear_dns_cache=Mock(), close=Mock())
        with self.assertRaises(m.RuntimeRefreshError) as raised:
            await m.refresh_runtime(r, ["192.0.2.53"], [connector])
        self.assertEqual(raised.exception.report["result"], "failed")
        connector.clear_dns_cache.assert_not_called()
        connector.close.assert_not_called()

    async def test_probe_uses_supplied_running_resolver_and_reports_failures(self):
        r = SimpleNamespace(resolve=AsyncMock(side_effect=[[{"host": "192.0.2.1"}], OSError()]))
        result = await m.probe_runtime(r, ["first.example", "second.example"])
        self.assertEqual(result["result"], "failed")
        self.assertEqual([x["success"] for x in result["checks"]], [True, False])
        self.assertEqual(r.resolve.await_count, 2)

    async def test_missing_explicit_file_servers_is_not_misreported_as_verified(self):
        c = FakeChannel(["127.0.0.1:53"])
        with self.assertRaises(m.RuntimeRefreshError) as raised:
            await m.refresh_runtime(resolver(c), [])
        self.assertEqual(raised.exception.report["result"], "no_explicit_servers")
        self.assertEqual(c.reloads, 0)
