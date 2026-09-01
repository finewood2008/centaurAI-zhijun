"""Consumer Connectivity 票据测试向量生成器（阶段 2 conformance 前置）。

为与真实 Consumer API/JWKS 联调前提供确定性的 JWKS + 票据向量：
- 使用内嵌固定测试私钥（仅测试），输出 JWKS 与各正/反例 claims 模板；
- 生成 testdata/consumer_ticket_vectors.json，由 conformance 测试在运行时
  以当前时间签名并校验（保证向量不会因绝对时间过期而失效）；
- 私钥/向量仅供测试：发布检查必须排除 testdata 且禁止打包本测试私钥。

运行：
    .venv\\Scripts\\python.exe scripts/consumer_ticket_vectors.py
"""
from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ISSUER = "https://consumer.example.test"
AUDIENCE = "mindos-device-service"
DEVICE_ID = "device_local"
REQUIRED_SCOPE = "mindos:access"
MAX_TTL_SECONDS = 300
ALGORITHMS = ["RS256"]
KID = "consumer-ticket-test-1"

TEST_PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDRv29nXQSeWg1J
/XxgVvEwZaNzGPeScj643Huct28P1XYH/lS5HIHjlRpS3hVPGQl6le2LxvmFhujP
i4pLizorz6SwS7RNXvBmX79PXhYTwFFxA4K18bl7tBoozH9HCASpTLk4T6oiFPIP
rlA/vFWJ1f9sombfXTxlAhX9uu4EpY9c6o39dK5aBFl+cx456hoh+cfslTjk3Txw
UW0kgJOPboG65aNDQAs1CWBKeJL7rTuJb4vR4lLJsVaLj8q6cC50Ge/es82h2wjp
osl+dGEAw7HCqNBk4cNyZW3eyzRPHEaaGnMf9lOk38/lebQD6msjAFenHFlAkxvW
igV0lZhNAgMBAAECggEAPJC2rnYhm0gNhkv32inAw2TV6apP8q2ihubDmuEs5LmS
t4QtGrasmva2/y65oHluT0NzsGToMDJgj22PpXiyd2wh9fYmPiEn8ae8KkLUxSdH
XQbSe48tLBc5ZoaGShB6qBhLc4MtcWHy86w15/GOEZsFgmzyn5Tgl5oel4GesSBH
bs4KHxT4wsPEA3fkpa8VkGgjEhx1A+9MFpcmC+ftau+8f8jvSBkVB4t+N10RM339
6cNTDUJomOLaIeW9S2HKSBk7Kc8QnYS55VkNuVtQl1fe6w8X+rP8FJ3gYuyE3prA
42tplBao2o1jy3oAD9NthuW/wwzgz69yYgmmTQo0UQKBgQDx7TCMtXEA9vFnzUET
3fSKte2Ltrb9JqTNH9kvGxSeFqpmyGIvk0DQUcJPPGt4LkXLKW1A7EUE2NHLiE6K
h1VHVGDfIkmJynbryJBMZxeY4T8dwdgO5x3sGPQXG+xuQJIQbDK8844s9P9Itgil
JAFsEulP8LfbhtJqKSLycHvbuwKBgQDd8wjVHIVwIv/OJsfDuQi5zEnf+XyFOIHq
4PT1wc5QqhmN8KeAqQMAp9tA2bxBRJFbKrFUypfeQ9VGcZbdmAjqWFRttkU4XKYV
TyKSfJckLzQxQ8cjFWKQ1Zn2fJFKC7swagTBfUzCE/PQYtKuUMsAHRYg3QTuwZho
SFKPwpCnlwKBgQDCIiGpaBgMPB0vvMeSF7Qacy7xxGdG8XGhoQL5B/Qdf/axj+8q
WjHSeSlByCw9PnSHOPEQ/gfMgeioOPM9uqe2G4G4zJzSU4PmZQVWKgwHhAjP6jNk
khWy1btZp/Cr8GjFgO2eLptSfC82u8xoKGJzxSEwIuyG3sOOqQAAKD9b7QKBgAxU
qUP1vrAZMa8JVoXYLNTttZj86l8YYZdkAhf5OXYfzSWmnhe2zBToPnUe46eYoJ65
A3sbek595EZynxgWj0A9wgsKWlQkSZHbgKc0xsza1oJ6KoEXeg9j3pbkGspLVo39
BeCeDnql0yDbrKrEkFKkSwtuXAzLsqTwh622+IRnAoGARSKiqBT0onouguAqnS+h
7iM4UUdfqxHo9xLUNRJadoxlmxj1hSGChDFe9Zrt9uJEzf7wvYNwY3iEOIMibJdi
huNPUqX0lU01+8ELU7nHKdjcYw+eJXXTVst07EvE/uE5xDZyCHN+8EFhRZdZPPJ5
3MeKGSkNgDbbipOPZRNCPhw=
-----END PRIVATE KEY-----
"""


def _load_private_key() -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(TEST_PRIVATE_KEY_PEM.encode("utf-8"), password=None)


def _jwk(kid: str) -> dict:
    """把内嵌测试公钥导出为 JWK（确定性输出）。"""
    public_key = _load_private_key().public_key()
    numbers = public_key.public_numbers()
    n_bytes = (numbers.n.bit_length() + 7) // 8
    e_bytes = (numbers.e.bit_length() + 7) // 8
    n_b64 = base64.urlsafe_b64encode(numbers.n.to_bytes(n_bytes, "big")).decode().rstrip("=")
    e_b64 = base64.urlsafe_b64encode(numbers.e.to_bytes(e_bytes, "big")).decode().rstrip("=")
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": n_b64,
        "e": e_b64,
    }


def _base_claims(nonce: str, *, account_id: str, client_id: str, epoch_generation: int = 0) -> dict:
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "account_id": account_id,
        "client_id": client_id,
        "device_id": DEVICE_ID,
        "scope": f"{REQUIRED_SCOPE} mindos:read",
        "nonce": nonce,
        "epoch_generation": epoch_generation,
    }


def build_vectors() -> list[dict]:
    vectors = []

    valid_claims = _base_claims(secrets.token_hex(16), account_id="account_vector_1", client_id="client_vector_1")
    vectors.append({
        "id": "valid",
        "description": "绑定本机设备、scope 足够、epoch=0、首连窗口未关闭的合法票据",
        "claims": valid_claims,
        "ttlSeconds": 120,
        "connectBefore": 60,
        "expected": "accept",
    })
    vectors.append({
        "id": "replay",
        "description": "与 valid 相同 nonce 的票据再次使用（运行时重签）",
        "claims": valid_claims,
        "ttlSeconds": 120,
        "connectBefore": 60,
        "expected": {"code": "CONNECTIVITY_TICKET_NONCE_REUSED"},
    })

    vectors.append({
        "id": "expired",
        "description": "exp 早于 iat（票据已过期）",
        "claims": _base_claims(secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"),
        "ttlSeconds": -60,
        "connectBefore": 60,
        "expected": {"code": "CONNECTIVITY_TICKET_INVALID"},
    })
    vectors.append({
        "id": "wrong_device",
        "description": "device_id 不是本机设备",
        "claims": {
            **_base_claims(secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"),
            "device_id": "device_other",
        },
        "ttlSeconds": 120,
        "connectBefore": 60,
        "expected": {"code": "CONNECTIVITY_TICKET_DEVICE_MISMATCH"},
    })
    vectors.append({
        "id": "wrong_audience",
        "description": "audience 不匹配",
        "claims": {
            **_base_claims(secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"),
            "aud": "other-audience",
        },
        "ttlSeconds": 120,
        "connectBefore": 60,
        "expected": {"code": "CONNECTIVITY_TICKET_INVALID"},
    })
    vectors.append({
        "id": "missing_epoch",
        "description": "缺少 epoch_generation 声明",
        "claims": {
            k: v for k, v in _base_claims(
                secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"
            ).items() if k != "epoch_generation"
        },
        "ttlSeconds": 120,
        "connectBefore": 60,
        "expected": {"code": "CONNECTIVITY_TICKET_INVALID"},
    })
    vectors.append({
        "id": "missing_connect_before",
        "description": "缺少 connect_before 声明",
        "claims": {
            k: v for k, v in _base_claims(
                secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"
            ).items() if k != "connect_before"
        },
        "ttlSeconds": 120,
        "connectBefore": None,
        "expected": {"code": "CONNECTIVITY_TICKET_INVALID"},
    })
    vectors.append({
        "id": "connect_window_closed",
        "description": "connect_before 已过（窗口关闭，但 exp 未到）",
        "claims": _base_claims(secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"),
        "ttlSeconds": 120,
        "connectBefore": 5,
        "expected": {"code": "CONNECTIVITY_TICKET_CONNECT_WINDOW_CLOSED"},
    })
    vectors.append({
        "id": "ttl_too_long",
        "description": "有效期超过 maxTtlSeconds(300)",
        "claims": _base_claims(secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"),
        "ttlSeconds": 3600,
        "connectBefore": 60,
        "expected": {"code": "CONNECTIVITY_TICKET_INVALID"},
    })
    vectors.append({
        "id": "scope_denied",
        "description": "scope 缺少 mindos:access",
        "claims": {
            **_base_claims(secrets.token_hex(16), account_id="account_vector_2", client_id="client_vector_2"),
            "scope": "mindos:read",
        },
        "ttlSeconds": 120,
        "connectBefore": 60,
        "expected": {"code": "CONNECTIVITY_TICKET_SCOPE_DENIED"},
    })
    vectors.append({
        "id": "stale_epoch",
        "description": "先轮换 tuple 的 epoch，旧 epoch=0 票据失效",
        "claims": _base_claims(secrets.token_hex(16), account_id="account_stale", client_id="client_stale"),
        "ttlSeconds": 120,
        "connectBefore": 60,
        "setup": {"type": "rotate_epoch", "accountId": "account_stale", "clientId": "client_stale"},
        "expected": {"code": "CONNECTIVITY_TICKET_EPOCH_STALE"},
    })
    vectors.append({
        "id": "revoked",
        "description": "tuple 已被撤销",
        "claims": _base_claims(secrets.token_hex(16), account_id="account_rev", client_id="client_rev"),
        "ttlSeconds": 120,
        "connectBefore": 60,
        "setup": {"type": "revoke", "accountId": "account_rev", "clientId": "client_rev"},
        "expected": {"code": "CONNECTIVITY_TICKET_REVOKED"},
    })
    vectors.append({
        "id": "denied_device",
        "description": "本机设备被 ACL 禁用（最后执行，影响所有本机设备票据）",
        "claims": _base_claims(secrets.token_hex(16), account_id="account_deny", client_id="client_deny"),
        "ttlSeconds": 120,
        "connectBefore": 60,
        "setup": {"type": "deny_device"},
        "expected": {"code": "CONNECTIVITY_TICKET_DEVICE_DENIED"},
    })
    return vectors


def main() -> None:
    document = {
        "version": 1,
        "schema": "consumer-connectivity-ticket-vector/v1",
        "generatedBy": "scripts/consumer_ticket_vectors.py",
        "note": "仅测试夹具：私钥禁止进入生产包（发布检查排除 testdata）。",
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "deviceId": DEVICE_ID,
        "requiredScope": REQUIRED_SCOPE,
        "maxTtlSeconds": MAX_TTL_SECONDS,
        "algorithms": ALGORITHMS,
        "kid": KID,
        "privateKeyPem": TEST_PRIVATE_KEY_PEM,
        "jwks": {"keys": [_jwk(KID)]},
        "vectors": build_vectors(),
    }
    out = PROJECT_ROOT / "testdata" / "consumer_ticket_vectors.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
