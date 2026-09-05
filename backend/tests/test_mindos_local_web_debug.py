"""阶段 1：未配置盒子时的本机 Web 调试 Gate。"""

from __future__ import annotations

import unittest

from mindos.local_web_debug import (
    ACCESS_MODE_LOCAL_DEBUG,
    ACCESS_MODE_TICKET_REQUIRED,
    access_context,
    is_loopback_host,
)


class LocalWebDebugAccessTests(unittest.TestCase):
    def test_default_is_fail_closed(self):
        result = access_context(bind_host="127.0.0.1", environ={})
        self.assertEqual(result["mode"], ACCESS_MODE_TICKET_REQUIRED)
        self.assertEqual(result["reason"], "runtime_not_development")

    def test_explicit_development_loopback_configuration_allows_local_debug(self):
        result = access_context(
            bind_host="127.0.0.1",
            environ={
                "MINDOS_RUNTIME_ENV": "development",
                "MINDOS_LOCAL_WEB_DEBUG_ACCESS": "true",
            },
        )
        self.assertEqual(result["mode"], ACCESS_MODE_LOCAL_DEBUG)
        self.assertTrue(result["localDebug"])
        self.assertEqual(result["scope"], "mindos:local-debug")

    def test_debug_gate_rejects_non_loopback_bind(self):
        result = access_context(
            bind_host="0.0.0.0",
            environ={
                "MINDOS_RUNTIME_ENV": "development",
                "MINDOS_LOCAL_WEB_DEBUG_ACCESS": "1",
            },
        )
        self.assertEqual(result["mode"], ACCESS_MODE_TICKET_REQUIRED)
        self.assertEqual(result["reason"], "server_not_loopback")

    def test_debug_gate_must_be_explicitly_enabled(self):
        result = access_context(
            bind_host="::1",
            environ={"MINDOS_RUNTIME_ENV": "development"},
        )
        self.assertEqual(result["mode"], ACCESS_MODE_TICKET_REQUIRED)
        self.assertEqual(result["reason"], "local_debug_disabled")

    def test_loopback_parser_does_not_trust_hostnames_or_lan_addresses(self):
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("192.168.1.8"))


if __name__ == "__main__":
    unittest.main()
