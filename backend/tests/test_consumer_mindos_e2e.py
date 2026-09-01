"""阶段 2：Mock Consumer API ↔ MindOS 端到端（验签 / 撤销翻译 / nonce 重放）。

流程：登录注册 → 认领设备 → 签发连接票据 → MindOS JWKS 验签 →
撤销 Client → 撤销事件翻译到本机 store → 旧票据拒绝 → 新 Client 恢复。
"""

from __future__ import annotations

import base64
import unittest
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from consumer_api.app import API_PREFIX, create_app
from consumer_api.store import ConsumerState
from mindos import consumer_adapter
from mindos.connectivity_ticket import (
    ConnectivityTicketError,
    JwtConnectivityTicketVerifier,
    JwtTicketVerifierConfig,
)
from mindos.device_context import get_device_registry
from mindos.stores import connectivity_store

PHONE = "13900000001"
CODE = "123456"
DEVICE_ID = "device_local"


def _b64u_to_int(value: str) -> int:
    padded = value + "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded), "big")


class _FakeJwksClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self.key)


class ConsumerMindosE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        connectivity_store.reset()
        get_device_registry().reset()
        self.state = ConsumerState()
        self.mock = TestClient(create_app(self.state))
        self.mock.post(f"{API_PREFIX}/__mock/devices", json={"deviceId": DEVICE_ID, "name": "客厅盒子"})
        self.login = self.mock.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": CODE, "clientName": "PC"},
        ).json()
        self.headers = {"Authorization": f"Bearer {self.login['accessToken']}"}
        self.mock.post(
            f"{API_PREFIX}/devices/{DEVICE_ID}/claim",
            json={"idempotencyKey": "claim-1"},
            headers=self.headers,
        )

        jwks = consumer_adapter.fetch_jwks(
            jwks_getter=lambda: self.mock.get("/.well-known/jwks.json").json()
        )
        jwk = jwks["keys"][0]
        public_key = rsa.RSAPublicNumbers(
            e=_b64u_to_int(jwk["e"]),
            n=_b64u_to_int(jwk["n"]),
        ).public_key()
        self.verifier = JwtConnectivityTicketVerifier(
            JwtTicketVerifierConfig(
                jwks_url="https://consumer.example.test/.well-known/jwks.json",
                issuer="https://consumer.example.test",
                audience="mindos-device-service",
                device_id=DEVICE_ID,
                required_scope="mindos:access",
                algorithms=("RS256",),
                max_ttl_seconds=300,
            ),
            _FakeJwksClient(public_key),
        )

    def tearDown(self) -> None:
        connectivity_store.reset()
        get_device_registry().reset()

    def _session(self, key: str) -> dict:
        return self.mock.post(
            f"{API_PREFIX}/connectivity/sessions",
            json={"deviceId": DEVICE_ID, "idempotencyKey": key},
            headers=self.headers,
        ).json()

    def test_mock_issued_ticket_is_verified_by_mindos(self):
        session = self._session("sess-1")
        principal = self.verifier.verify(session["ticket"], method="GET", path="/api/mindos/home")
        self.assertEqual(principal.device_id, DEVICE_ID)
        self.assertEqual(principal.account_id, self.login["accountId"])
        self.assertEqual(principal.client_id, self.login["clientId"])
        self.assertEqual(principal.epoch_generation, 0)
        self.assertEqual(principal.nonce, session["nonce"])

    def test_ticket_replay_is_rejected(self):
        session = self._session("sess-replay")
        self.verifier.verify(session["ticket"], method="GET", path="/api/mindos/home")
        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(session["ticket"], method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_NONCE_REUSED")

    def test_revoke_translated_and_old_ticket_rejected_until_new_client(self):
        session = self._session("sess-revoke")
        self.verifier.verify(session["ticket"], method="GET", path="/api/mindos/home")

        revoke = self.mock.post(
            f"{API_PREFIX}/auth/clients/{self.login['clientId']}/revoke",
            headers=self.headers,
        ).json()
        self.assertEqual(revoke["newEpoch"], 1)

        result = consumer_adapter.sync_revocations(
            since=0,
            revocations_getter=lambda s: self.mock.get(f"{API_PREFIX}/__mock/revocations?since={s}").json(),
        )
        self.assertEqual(result["applied"], 1)
        self.assertGreaterEqual(result["newSince"], 1)

        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(session["ticket"], method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_REVOKED")

        self.login2 = self.mock.post(
            f"{API_PREFIX}/auth/login",
            json={"phone": PHONE, "code": CODE, "clientName": "App"},
        ).json()
        self.headers2 = {"Authorization": f"Bearer {self.login2['accessToken']}"}
        session2 = self.mock.post(
            f"{API_PREFIX}/connectivity/sessions",
            json={"deviceId": DEVICE_ID, "idempotencyKey": "sess-new"},
            headers=self.headers2,
        ).json()
        principal = self.verifier.verify(session2["ticket"], method="GET", path="/api/mindos/home")
        self.assertEqual(principal.client_id, self.login2["clientId"])
        self.assertEqual(principal.epoch_generation, 0)

    def test_revocations_sync_is_idempotent(self):
        self._session("sess-idem")
        self.mock.post(
            f"{API_PREFIX}/auth/clients/{self.login['clientId']}/revoke",
            headers=self.headers,
        )
        first = consumer_adapter.sync_revocations(
            since=0,
            revocations_getter=lambda s: self.mock.get(f"{API_PREFIX}/__mock/revocations?since={s}").json(),
        )
        second = consumer_adapter.sync_revocations(
            since=first["newSince"],
            revocations_getter=lambda s: self.mock.get(f"{API_PREFIX}/__mock/revocations?since={s}").json(),
        )
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["applied"], 0)

    def test_sync_cursor_persisted_and_redelivered_event_is_deduplicated(self):
        """F6：即使 since 重置，同一事件 seq 也不会重复 mark_revoked（不重复递增 epoch）。"""
        self._session("sess-dedupe")
        self.mock.post(
            f"{API_PREFIX}/auth/clients/{self.login['clientId']}/revoke",
            headers=self.headers,
        )
        payload = self.mock.get(f"{API_PREFIX}/__mock/revocations?since=0").json()
        self.assertEqual(len(payload["revocations"]), 1)
        epoch_before = connectivity_store.current_epoch(
            account_id=self.login["accountId"],
            client_id=self.login["clientId"],
            device_id=DEVICE_ID,
        )

        def same_payload(_since: int) -> dict:
            return payload

        first = consumer_adapter.sync_revocations(revocations_getter=same_payload)
        self.assertEqual(first["applied"], 1)
        self.assertGreater(first["cursor"], 0)
        self.assertEqual(
            connectivity_store.get_sync_cursor(consumer_adapter.REVOCATION_CURSOR_KEY),
            first["cursor"],
        )

        second = consumer_adapter.sync_revocations(revocations_getter=same_payload)
        self.assertEqual(second["applied"], 0)
        self.assertEqual(
            connectivity_store.current_epoch(
                account_id=self.login["accountId"],
                client_id=self.login["clientId"],
                device_id=DEVICE_ID,
            ),
            epoch_before + 1,
        )


if __name__ == "__main__":
    unittest.main()
