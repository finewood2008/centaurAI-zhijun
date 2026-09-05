"""阶段 2：MindOS 按真实 device_id 隔离的运行时上下文。"""

from __future__ import annotations

import unittest

from mindos.connectivity_ticket import ConnectivityPrincipal
from mindos.device_context import DeviceContextRegistry, get_device_registry, namespace_for


def _principal(account_id: str, client_id: str, device_id: str) -> ConnectivityPrincipal:
    return ConnectivityPrincipal(
        account_id=account_id,
        client_id=client_id,
        device_id=device_id,
        scopes=frozenset({"mindos:access"}),
        expires_at=2_000_000_000,
        nonce="nonce",
        epoch_generation=0,
    )


class DeviceContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = DeviceContextRegistry()

    def tearDown(self) -> None:
        self.registry.reset()
        get_device_registry().reset()

    def test_distinct_contexts_per_device(self):
        ctx_a = self.registry.get_or_create(_principal("acct_1", "client_1", "device_a"))
        ctx_b = self.registry.get_or_create(_principal("acct_1", "client_2", "device_b"))
        self.assertIsNot(ctx_a, ctx_b)
        self.assertEqual(ctx_a.device_id, "device_a")
        self.assertEqual(ctx_b.device_id, "device_b")
        self.assertNotEqual(ctx_a.cache_namespace("material"), ctx_b.cache_namespace("material"))

    def test_reuse_returns_same_context_and_touches(self):
        first = self.registry.get_or_create(_principal("acct_1", "client_1", "device_a"))
        second = self.registry.get_or_create(_principal("acct_1", "client_1", "device_a"))
        self.assertIs(first, second)
        self.assertEqual(self.registry.list()[0]["activeSessions"], [])

    def test_account_switch_invalidates_old_context(self):
        ctx = self.registry.get_or_create(_principal("acct_1", "client_1", "device_a"))
        ctx.session_ids.add("session_1")
        gen_before = ctx.cache_generation

        replaced = self.registry.get_or_create(_principal("acct_2", "client_9", "device_a"))

        self.assertIs(replaced, ctx)
        self.assertEqual(replaced.account_id, "acct_2")
        self.assertGreater(replaced.cache_generation, gen_before)
        self.assertEqual(replaced.session_ids, set())

    def test_invalidate_bumps_generation_and_clears_sessions(self):
        ctx = self.registry.get_or_create(_principal("acct_1", "client_1", "device_a"))
        ctx.session_ids.add("session_1")
        gen_before = ctx.cache_generation
        namespace_before = ctx.cache_namespace("qa")

        self.registry.invalidate("device_a")

        self.assertGreater(ctx.cache_generation, gen_before)
        self.assertEqual(ctx.session_ids, set())
        self.assertNotEqual(ctx.cache_namespace("qa"), namespace_before)

    def test_release_removes_context(self):
        self.registry.get_or_create(_principal("acct_1", "client_1", "device_a"))
        self.assertTrue(self.registry.release("device_a"))
        self.assertIsNone(self.registry.get("device_a"))

    def test_static_namespace_helper(self):
        self.assertEqual(namespace_for("device_a", "tasks"), "device:device_a:tasks")


if __name__ == "__main__":
    unittest.main()
