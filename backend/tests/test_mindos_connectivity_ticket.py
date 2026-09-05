"""阶段 2：MindOS 连接票据适配层的 fail-closed 合同。

覆盖：Bearer 提取、JWKS 验签、设备绑定、scope、TTL，以及阶段 2 新增的
ACL 禁用、撤销、连接 epoch 失效与 nonce 重放防护。
"""

from __future__ import annotations

from types import SimpleNamespace
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from mindos.connectivity_ticket import (
    ConnectivityPrincipal,
    ConnectivityTicketError,
    JwtConnectivityTicketVerifier,
    JwtTicketVerifierConfig,
    authenticate_connectivity_ticket,
    configure_ticket_verifier,
    configure_ticket_verifier_from_environment,
)
from mindos.stores import connectivity_store

TEST_CONFIG = JwtTicketVerifierConfig(
    jwks_url="https://consumer.example.test/.well-known/jwks.json",
    issuer="https://consumer.example.test",
    audience="mindos-device-service",
    device_id="device_local",
    required_scope="mindos:access",
    algorithms=("RS256",),
    max_ttl_seconds=300,
)


class _FakeVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def verify(self, token: str, *, method: str, path: str) -> ConnectivityPrincipal:
        self.calls.append((token, method, path))
        return ConnectivityPrincipal(
            account_id="acct_test",
            client_id="client_test",
            device_id="device_test",
            scopes=frozenset({"mindos:read"}),
            expires_at=2_000_000_000,
            nonce="nonce_test",
        )


class _FakeJwksClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self.key)


class ConnectivityTicketTests(unittest.TestCase):
    def setUp(self) -> None:
        connectivity_store.reset()
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.verifier = JwtConnectivityTicketVerifier(TEST_CONFIG, _FakeJwksClient(self.private_key.public_key()))
        self.now = int(time.time())

    def tearDown(self) -> None:
        configure_ticket_verifier(None)
        connectivity_store.reset()

    def _token(self, *, nonce: str, account_id: str = "account_1", client_id: str = "client_1",
               device_id: str = "device_local", scope: str = "mindos:access",
               epoch_generation: int = 0, exp_offset: int = 120, **overrides) -> str:
        claims = {
            "iss": "https://consumer.example.test",
            "aud": "mindos-device-service",
            "account_id": account_id,
            "client_id": client_id,
            "device_id": device_id,
            "scope": scope,
            "nonce": nonce,
            "epoch_generation": epoch_generation,
            "connect_before": self.now + 60,
            "iat": self.now,
            "nbf": self.now,
            "exp": self.now + exp_offset,
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "test-key"})

    def test_missing_ticket_is_rejected(self):
        with self.assertRaises(ConnectivityTicketError) as raised:
            authenticate_connectivity_ticket(None, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_REQUIRED")

    def test_malformed_ticket_is_rejected(self):
        with self.assertRaises(ConnectivityTicketError) as raised:
            authenticate_connectivity_ticket("Token abc", method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_INVALID")

    def test_ticket_cannot_pass_before_verifier_is_configured(self):
        with self.assertRaises(ConnectivityTicketError) as raised:
            authenticate_connectivity_ticket("Bearer hidden-ticket", method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_VERIFIER_UNAVAILABLE")

    def test_only_the_verifier_receives_bearer_value_and_request_metadata(self):
        verifier = _FakeVerifier()
        configure_ticket_verifier(verifier)

        principal = authenticate_connectivity_ticket(
            "Bearer ticket-for-verifier-only",
            method="POST",
            path="/api/mindos/qa/answer",
        )

        self.assertEqual(principal.account_id, "acct_test")
        self.assertEqual(principal.device_id, "device_test")
        self.assertEqual(verifier.calls, [("ticket-for-verifier-only", "POST", "/api/mindos/qa/answer")])

    def test_jwt_verifier_accepts_ticket_bound_to_local_device_and_scope(self):
        token = self._token(nonce="nonce_accept_1", epoch_generation=0)

        principal = self.verifier.verify(token, method="GET", path="/api/mindos/home")

        self.assertEqual(principal.account_id, "account_1")
        self.assertEqual(principal.client_id, "client_1")
        self.assertEqual(principal.device_id, "device_local")
        self.assertIn("mindos:access", principal.scopes)
        self.assertEqual(principal.epoch_generation, 0)

    def test_jwt_verifier_rejects_device_mismatch_scope_denial_and_excessive_ttl(self):
        cases = [
            ({"device_id": "device_other"}, "CONNECTIVITY_TICKET_DEVICE_MISMATCH"),
            ({"scope": "mindos:read"}, "CONNECTIVITY_TICKET_SCOPE_DENIED"),
            ({"exp_offset": 301}, "CONNECTIVITY_TICKET_INVALID"),
        ]
        for index, (override, expected_code) in enumerate(cases):
            token = self._token(nonce=f"nonce_neg_{index}", **override)
            with self.subTest(expected_code=expected_code), self.assertRaises(ConnectivityTicketError) as raised:
                self.verifier.verify(token, method="GET", path="/api/mindos/home")
            self.assertEqual(raised.exception.code, expected_code)

    def test_jwt_verifier_requires_epoch_generation_claim(self):
        token = self._token(nonce="nonce_no_epoch_1", epoch_generation=0)
        token = jwt.encode(
            {
                "iss": "https://consumer.example.test",
                "aud": "mindos-device-service",
                "account_id": "account_1",
                "client_id": "client_1",
                "device_id": "device_local",
                "scope": "mindos:access",
                "nonce": "nonce_no_epoch_1",
                "connect_before": self.now + 60,
                "iat": self.now,
                "nbf": self.now,
                "exp": self.now + 120,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(token, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_INVALID")

    def test_jwt_verifier_requires_connect_before_claim(self):
        token = self._token(nonce="nonce_no_connect_before_1", epoch_generation=0)
        token = jwt.encode(
            {
                k: v for k, v in {
                    "iss": "https://consumer.example.test",
                    "aud": "mindos-device-service",
                    "account_id": "account_1",
                    "client_id": "client_1",
                    "device_id": "device_local",
                    "scope": "mindos:access",
                    "nonce": "nonce_no_connect_before_1",
                    "epoch_generation": 0,
                    "iat": self.now,
                    "nbf": self.now,
                    "exp": self.now + 120,
                }.items()
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(token, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_INVALID")

    def test_jwt_verifier_rejects_closed_connect_window(self):
        token = self._token(
            nonce="nonce_window_closed_1",
            iat=self.now - 60,
            nbf=self.now - 60,
            connect_before=self.now - 5,
        )
        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(token, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_CONNECT_WINDOW_CLOSED")

    def test_jwt_verifier_rejects_nonce_replay(self):
        token = self._token(nonce="nonce_replay_1")
        self.verifier.verify(token, method="GET", path="/api/mindos/home")

        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(token, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_NONCE_REUSED")

    def test_jwt_verifier_rejects_revoked_ticket(self):
        connectivity_store.mark_revoked(
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
            reason="test-revoke",
        )
        token = self._token(nonce="nonce_after_revoke_1")
        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(token, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_REVOKED")

    def test_jwt_verifier_rejects_stale_epoch(self):
        connectivity_store.rotate_epoch(
            account_id="account_1",
            client_id="client_1",
            device_id="device_local",
            reason="test-rotate",
        )
        token = self._token(nonce="nonce_stale_epoch_1", epoch_generation=0)
        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(token, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_EPOCH_STALE")

    def test_jwt_verifier_rejects_device_denied_by_acl(self):
        connectivity_store.set_acl(scope="device:device_local", denied=True, reason="test-deny")
        token = self._token(nonce="nonce_denied_1")
        with self.assertRaises(ConnectivityTicketError) as raised:
            self.verifier.verify(token, method="GET", path="/api/mindos/home")
        self.assertEqual(raised.exception.code, "CONNECTIVITY_TICKET_DEVICE_DENIED")

    def test_environment_config_remains_closed_until_all_trust_inputs_are_present(self):
        self.assertEqual(configure_ticket_verifier_from_environment({}), "unconfigured")
        self.assertEqual(configure_ticket_verifier_from_environment({
            "MINDOS_CONNECTIVITY_JWKS_URL": "http://insecure.example.test/jwks",
            "MINDOS_CONNECTIVITY_ISSUER": "issuer",
            "MINDOS_CONNECTIVITY_AUDIENCE": "audience",
            "MINDOS_DEVICE_ID": "device_local",
            "MINDOS_CONNECTIVITY_REQUIRED_SCOPE": "mindos:access",
        }), "unconfigured")


if __name__ == "__main__":
    unittest.main()
