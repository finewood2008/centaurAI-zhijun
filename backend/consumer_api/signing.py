"""Consumer API 连接票据签名与 JWKS（Mock 专用测试密钥）。

密钥仅用于 Mock 与联调，禁止进入生产包。生产接入后由真实 Consumer API
使用其自有私钥，MindOS 侧仍只消费公开 JWKS。
"""

from __future__ import annotations

import base64
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_KID = "consumer-ticket-test-1"
ALGORITHM = "RS256"

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


def load_private_key() -> rsa.RSAPrivateKey:
    return serialization.load_pem_private_key(TEST_PRIVATE_KEY_PEM.encode("utf-8"), password=None)


def public_jwks(kid: str = TEST_KID) -> dict:
    numbers = load_private_key().public_key().public_numbers()
    n_bytes = (numbers.n.bit_length() + 7) // 8
    e_bytes = (numbers.e.bit_length() + 7) // 8
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": ALGORITHM,
        "use": "sig",
        "n": base64.urlsafe_b64encode(numbers.n.to_bytes(n_bytes, "big")).decode().rstrip("="),
        "e": base64.urlsafe_b64encode(numbers.e.to_bytes(e_bytes, "big")).decode().rstrip("="),
    }


def issue_ticket(
    *,
    issuer: str,
    audience: str,
    device_id: str,
    account_id: str,
    client_id: str,
    scope: str,
    nonce: str,
    epoch_generation: int,
    ttl_seconds: int,
    connect_before_seconds: int,
    kid: str = TEST_KID,
) -> tuple[str, int, int]:
    """签发短期连接票据，返回 (token, expires_at, connect_before)。"""
    now = int(time.time())
    expires_at = now + ttl_seconds
    connect_before = now + connect_before_seconds
    claims = {
        "iss": issuer,
        "aud": audience,
        "account_id": account_id,
        "client_id": client_id,
        "device_id": device_id,
        "scope": scope,
        "nonce": nonce,
        "epoch_generation": epoch_generation,
        "connect_before": connect_before,
        "iat": now,
        "nbf": now,
        "exp": expires_at,
    }
    token = jwt.encode(claims, load_private_key(), algorithm=ALGORITHM, headers={"kid": kid})
    return token, expires_at, connect_before
