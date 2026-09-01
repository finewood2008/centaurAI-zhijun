"""阶段 2：Consumer Connectivity 状态存储（nonce 重放、撤销、epoch、会话、ACL）。"""

from __future__ import annotations

import time
import unittest

from mindos.stores import connectivity_store

TUPLE = dict(account_id="account_1", client_id="client_1", device_id="device_local")


class ConnectivityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        connectivity_store.reset()

    def tearDown(self) -> None:
        connectivity_store.reset()

    def test_nonce_is_single_use_and_pruned(self):
        self.assertTrue(connectivity_store.consume_nonce(nonce="n1", expires_at=time.time() + 100, **TUPLE))
        self.assertFalse(connectivity_store.consume_nonce(nonce="n1", expires_at=time.time() + 100, **TUPLE))
        self.assertTrue(connectivity_store.consume_nonce(nonce="n2", expires_at=time.time() + 100, **TUPLE))
        self.assertEqual(connectivity_store.prune_expired_nonces(now=time.time() + 101), 2)

    def test_same_nonce_across_different_device_is_independent(self):
        other = dict(account_id="account_1", client_id="client_1", device_id="device_other")
        self.assertTrue(connectivity_store.consume_nonce(nonce="shared_nonce", expires_at=time.time() + 100, **TUPLE))
        self.assertTrue(connectivity_store.consume_nonce(nonce="shared_nonce", expires_at=time.time() + 100, **other))

    def test_revocation_records_timestamp_and_bumps_epoch(self):
        self.assertIsNone(connectivity_store.is_revoked(**TUPLE))
        result = connectivity_store.mark_revoked(reason="test", **TUPLE)
        self.assertEqual(result["newEpoch"], 1)
        self.assertIsNotNone(connectivity_store.is_revoked(**TUPLE))
        self.assertEqual(connectivity_store.current_epoch(**TUPLE), 1)

    def test_rotate_epoch_increments_and_is_idempotent(self):
        self.assertEqual(connectivity_store.rotate_epoch(reason="r1", **TUPLE)["newEpoch"], 1)
        self.assertEqual(connectivity_store.rotate_epoch(reason="r2", **TUPLE)["newEpoch"], 2)
        self.assertEqual(connectivity_store.current_epoch(**TUPLE), 2)

    def test_revocation_closes_active_sessions(self):
        connectivity_store.register_session(session_id="s1", epoch_generation=1, expires_at=time.time() + 600, **TUPLE)
        connectivity_store.register_session(session_id="s2", epoch_generation=1, expires_at=time.time() + 600, **TUPLE)
        self.assertEqual(len(connectivity_store.active_sessions(**TUPLE)), 2)

        result = connectivity_store.mark_revoked(reason="test", **TUPLE)
        self.assertEqual(result["closedSessions"], 2)
        self.assertEqual(connectivity_store.active_sessions(**TUPLE), [])

    def test_close_session_is_idempotent(self):
        connectivity_store.register_session(session_id="s1", epoch_generation=1, expires_at=time.time() + 600, **TUPLE)
        self.assertTrue(connectivity_store.close_session(session_id="s1", reason="done"))
        self.assertFalse(connectivity_store.close_session(session_id="s1", reason="again"))

    def test_stale_sessions_closed_after_epoch_rotation(self):
        connectivity_store.rotate_epoch(reason="rotate", **TUPLE)
        connectivity_store.register_session(session_id="old", epoch_generation=0, expires_at=time.time() + 600, **TUPLE)
        self.assertEqual(connectivity_store.close_stale_sessions(now=time.time()), 1)
        self.assertEqual(connectivity_store.active_sessions(**TUPLE), [])

    def test_expired_sessions_closed_by_sweep(self):
        connectivity_store.register_session(session_id="expired", epoch_generation=1, expires_at=time.time() - 10, **TUPLE)
        self.assertEqual(connectivity_store.close_stale_sessions(now=time.time()), 1)

    def test_acl_global_and_device_deny(self):
        self.assertFalse(connectivity_store.is_device_denied(device_id="device_local"))
        connectivity_store.set_acl(scope="device:device_local", denied=True, reason="deny-device")
        self.assertTrue(connectivity_store.is_device_denied(device_id="device_local"))
        self.assertFalse(connectivity_store.is_device_denied(device_id="device_other"))

        connectivity_store.set_acl(scope="global", denied=True, reason="deny-all")
        self.assertTrue(connectivity_store.is_device_denied(device_id="device_other"))

        connectivity_store.set_acl(scope="device:device_local", denied=False, reason="allow-device")
        self.assertTrue(connectivity_store.is_device_denied(device_id="device_local"))

    def test_snapshot_contains_state_without_secrets(self):
        connectivity_store.mark_revoked(reason="test", **TUPLE)
        connectivity_store.set_acl(scope="device:device_local", denied=True, reason="deny")
        snapshot = connectivity_store.snapshot()
        self.assertEqual(len(snapshot["revocations"]), 1)
        self.assertEqual(len(snapshot["epochs"]), 1)
        self.assertEqual(snapshot["revocations"][0]["client_id"], "client_1")
        self.assertEqual(snapshot["acl"][0]["scope"], "device:device_local")


if __name__ == "__main__":
    unittest.main()
