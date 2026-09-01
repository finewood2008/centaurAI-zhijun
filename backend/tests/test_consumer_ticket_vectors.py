"""阶段 2：Consumer API/JWKS 票据测试向量 conformance 测试。

读取 scripts/consumer_ticket_vectors.py 生成的确定性向量，用夹具私钥在
运行时以当前时间签名后送入 JwtConnectivityTicketVerifier，断言每个向量
的期望结果（accept 或具体错误码）。向量覆盖：过期、错设备、错 audience、
缺 epoch、TTL 超限、scope 不足、epoch 失效、撤销、ACL 禁用与 nonce 重放。
"""

from __future__ import annotations

import base64
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mindos.connectivity_ticket import (
    ConnectivityTicketError,
    JwtConnectivityTicketVerifier,
    JwtTicketVerifierConfig,
)
from mindos.stores import connectivity_store

FIXTURE = (
    Path(__file__).resolve().parent.parent.parent
    / "testdata"
    / "consumer_ticket_vectors.json"
)


def _b64u_to_int(value: str) -> int:
    padded = value + "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded), "big")


class _FakeJwksClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self.key)


class ConsumerTicketVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.exists():
            raise AssertionError(f"向量夹具缺失，请先运行 scripts/consumer_ticket_vectors.py: {FIXTURE}")
        cls.doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        jwk = cls.doc["jwks"]["keys"][0]
        public_key = rsa.RSAPublicNumbers(
            e=_b64u_to_int(jwk["e"]),
            n=_b64u_to_int(jwk["n"]),
        ).public_key()
        cls.verifier = JwtConnectivityTicketVerifier(
            JwtTicketVerifierConfig(
                jwks_url="https://consumer.example.test/.well-known/jwks.json",
                issuer=cls.doc["issuer"],
                audience=cls.doc["audience"],
                device_id=cls.doc["deviceId"],
                required_scope=cls.doc["requiredScope"],
                algorithms=tuple(cls.doc["algorithms"]),
                max_ttl_seconds=cls.doc["maxTtlSeconds"],
            ),
            _FakeJwksClient(public_key),
        )
        cls.private_key = serialization.load_pem_private_key(
            cls.doc["privateKeyPem"].encode("utf-8"),
            password=None,
        )

    def setUp(self) -> None:
        connectivity_store.reset()

    def tearDown(self) -> None:
        connectivity_store.reset()

    def _sign(self, claims: dict, ttl_seconds: int, connect_before: int | None) -> str:
        now = int(time.time())
        iat = now - 10
        payload = {**claims, "iat": iat, "nbf": iat, "exp": now + ttl_seconds}
        if connect_before is not None:
            payload["connect_before"] = iat + connect_before
        return jwt.encode(payload, self.private_key, algorithm="RS256", headers={"kid": self.doc["kid"]})

    def _apply_setup(self, setup: dict | None) -> None:
        if setup is None:
            return
        kind = setup["type"]
        if kind == "rotate_epoch":
            connectivity_store.rotate_epoch(
                account_id=setup["accountId"],
                client_id=setup["clientId"],
                device_id=self.doc["deviceId"],
                reason="vector",
            )
        elif kind == "revoke":
            connectivity_store.mark_revoked(
                account_id=setup["accountId"],
                client_id=setup["clientId"],
                device_id=self.doc["deviceId"],
                reason="vector",
            )
        elif kind == "deny_device":
            connectivity_store.set_acl(
                scope=f"device:{self.doc['deviceId']}",
                denied=True,
                reason="vector",
            )
        else:
            raise AssertionError(f"未知 setup: {kind}")

    def test_all_vectors_match_expected_outcome(self):
        for vector in self.doc["vectors"]:
            with self.subTest(vector=vector["id"]):
                self._apply_setup(vector.get("setup"))
                token = self._sign(vector["claims"], vector["ttlSeconds"], vector.get("connectBefore"))
                expected = vector["expected"]
                if expected == "accept":
                    principal = self.verifier.verify(token, method="GET", path="/api/mindos/home")
                    self.assertEqual(principal.device_id, self.doc["deviceId"])
                else:
                    with self.assertRaises(ConnectivityTicketError) as raised:
                        self.verifier.verify(token, method="GET", path="/api/mindos/home")
                    self.assertEqual(raised.exception.code, expected["code"])

    def test_fixture_jwks_roundtrips_and_rejects_wrong_key(self):
        jwk = self.doc["jwks"]["keys"][0]
        self.assertEqual(jwk["kid"], self.doc["kid"])
        public_key = rsa.RSAPublicNumbers(
            e=_b64u_to_int(jwk["e"]),
            n=_b64u_to_int(jwk["n"]),
        ).public_key()
        self.assertEqual(
            public_key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
            self.private_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
        )


if __name__ == "__main__":
    unittest.main()
