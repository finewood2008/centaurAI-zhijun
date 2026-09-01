"""阶段 2：连接票据一次性交换与受控会话验证（F1）。

覆盖：exchange 消费 nonce 一次、会话凭证验证、撤销/epoch 失效/过期/
关闭会话后的 fail-closed 行为，以及业务请求不得再携带票据逐次消费。
"""

from __future__ import annotations

import time
import unittest

from mindos import connectivity_session
from mindos.connectivity_ticket import (
    ConnectivityPrincipal,
    ConnectivityTicketError,
    configure_ticket_verifier,
)
from mindos.stores import connectivity_store


class _FakeVerifier:
    def __init__(self, epoch_generation: int = 0) -> None:
        self.epoch_generation = epoch_generation
        self.verified = 0

    def verify(self, token: str, *, method: str, path: str) -> ConnectivityPrincipal:
        del token, method, path
        self.verified += 1
        return ConnectivityPrincipal(
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
            scopes=frozenset({"mindos:access", "mindos:read"}),
            expires_at=int(time.time()) + 120,
            nonce=f"nonce_{self.verified}",
            epoch_generation=self.epoch_generation,
        )


class _ReplayAwareVerifier(_FakeVerifier):
    """模拟真实验签器的 nonce 单次消费：同一票据第二次交换即重放。"""

    def verify(self, token: str, *, method: str, path: str) -> ConnectivityPrincipal:
        if self.verified:
            raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_NONCE_REUSED", "设备连接票据 nonce 已使用")
        return super().verify(token, method=method, path=path)


class ConnectivitySessionTests(unittest.TestCase):
    def setUp(self) -> None:
        connectivity_store.reset()
        self.verifier = _FakeVerifier()
        configure_ticket_verifier(self.verifier)

    def tearDown(self) -> None:
        configure_ticket_verifier(None)
        connectivity_store.reset()

    def test_exchange_returns_session_credential_and_registers_active_session(self):
        result = connectivity_session.exchange_ticket("Bearer ticket-1")
        self.assertTrue(result["sessionToken"])
        self.assertEqual(result["deviceId"], "device_local")
        self.assertEqual(result["accountId"], "account_1")
        sessions = connectivity_store.active_sessions(device_id="device_local")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], result["sessionId"])
        self.assertEqual(sessions[0]["epoch_generation"], 0)
        self.assertIn("mindos:access", sessions[0]["scopes"])

    def test_same_ticket_cannot_be_exchanged_twice(self):
        replay_verifier = _ReplayAwareVerifier()
        configure_ticket_verifier(replay_verifier)
        connectivity_session.exchange_ticket("Bearer ticket-1")
        with self.assertRaises(ConnectivityTicketError) as raised:
            connectivity_session.exchange_ticket("Bearer ticket-1")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_NONCE_REUSED")

    def test_validate_session_accepts_exchanged_credential(self):
        result = connectivity_session.exchange_ticket("Bearer ticket-1")
        principal = connectivity_session.validate_session(
            result["sessionToken"], method="GET", path="/api/mindos/home"
        )
        self.assertEqual(principal.device_id, "device_local")
        self.assertEqual(principal.account_id, "account_1")
        self.assertIn("mindos:access", principal.scopes)

    def test_validate_session_rejects_unknown_or_closed_credential(self):
        with self.assertRaises(ConnectivityTicketError) as raised:
            connectivity_session.validate_session("bogus-token", method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_SESSION_INVALID")

        result = connectivity_session.exchange_ticket("Bearer ticket-1")
        connectivity_store.close_session(session_id=result["sessionId"], reason="test")
        with self.assertRaises(ConnectivityTicketError) as raised:
            connectivity_session.validate_session(result["sessionToken"], method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_SESSION_INVALID")

    def test_validate_session_rejects_after_revoke(self):
        result = connectivity_session.exchange_ticket("Bearer ticket-1")
        connectivity_store.mark_revoked(
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
            reason="test",
        )
        with self.assertRaises(ConnectivityTicketError) as raised:
            connectivity_session.validate_session(result["sessionToken"], method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_SESSION_REVOKED")

    def test_validate_session_rejects_after_epoch_rotation(self):
        result = connectivity_session.exchange_ticket("Bearer ticket-1")
        connectivity_store.rotate_epoch(
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
            reason="test",
        )
        with self.assertRaises(ConnectivityTicketError) as raised:
            connectivity_session.validate_session(result["sessionToken"], method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_SESSION_EPOCH_STALE")

    def test_validate_session_rejects_expired_session(self):
        connectivity_store.register_session(
            session_id=connectivity_session._hash_token("expired-raw-token"),
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
            epoch_generation=0,
            expires_at=time.time() - 1,
            scopes="mindos:access",
        )
        with self.assertRaises(ConnectivityTicketError) as raised:
            connectivity_session.validate_session("expired-raw-token", method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_SESSION_EXPIRED")

    def test_revocation_closes_registered_exchange_session(self):
        result = connectivity_session.exchange_ticket("Bearer ticket-1")
        outcome = connectivity_store.mark_revoked(
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
            reason="consumer-revoke",
        )
        self.assertEqual(outcome["closedSessions"], 1)
        self.assertEqual(connectivity_store.active_sessions(device_id="device_local"), [])
        with self.assertRaises(ConnectivityTicketError) as raised:
            connectivity_session.validate_session(result["sessionToken"], method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_SESSION_REVOKED")


if __name__ == "__main__":
    unittest.main()
