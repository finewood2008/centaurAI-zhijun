"""Consumer Connectivity Ticket 的 MindOS 接入边界。

阶段 2 尚未交付 Consumer API 的 OpenAPI、claims、issuer/audience 与 JWKS 前，
默认 verifier 必须拒绝所有票据。路由仅通过本模块接触 Authorization，后续接入
真实验签器时不允许在业务端点复制 Bearer 解析或自行相信 client 声明的身份。

验签通过后，本模块还强制阶段 2 的本地安全约束（全部 fail-closed）：
- 设备级/全局 ACL 禁用：CONNECTIVITY_TICKET_DEVICE_DENIED
- 撤销记录命中：CONNECTIVITY_TICKET_REVOKED（服务端 5 秒内断开的前提是
  后续请求立即失败 + 会话登记被 mark_revoked 关闭）
- 连接 epoch 失效：CONNECTIVITY_TICKET_EPOCH_STALE（票据必须携带 epoch_generation）
- 首次连接窗口：CONNECTIVITY_TICKET_CONNECT_WINDOW_CLOSED（connect_before
  必填，窗口关闭后即使未到 exp 也不得交换）
- nonce 单次使用：CONNECTIVITY_TICKET_NONCE_REUSED
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Mapping, Protocol
from urllib.parse import urlparse
import os
import time

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from .stores import connectivity_store


@dataclass(frozen=True)
class ConnectivityPrincipal:
    """已由上游权威验签并绑定到本机设备的调用方身份。"""

    account_id: str
    client_id: str
    device_id: str
    scopes: frozenset[str]
    expires_at: int
    nonce: str
    epoch_generation: int = 0


class ConnectivityTicketError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ConnectivityTicketVerifier(Protocol):
    def verify(self, token: str, *, method: str, path: str) -> ConnectivityPrincipal:
        """验证票据及其设备绑定、scope、有效期、签名和 nonce。"""


@dataclass(frozen=True)
class JwtTicketVerifierConfig:
    """Consumer API 的公开验签配置；不含私钥或任何可用于签发的材料。"""

    jwks_url: str
    issuer: str
    audience: str
    device_id: str
    required_scope: str
    algorithms: tuple[str, ...]
    max_ttl_seconds: int

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "JwtTicketVerifierConfig":
        env = environ if environ is not None else os.environ
        required = {
            "jwks_url": (env.get("MINDOS_CONNECTIVITY_JWKS_URL") or "").strip(),
            "issuer": (env.get("MINDOS_CONNECTIVITY_ISSUER") or "").strip(),
            "audience": (env.get("MINDOS_CONNECTIVITY_AUDIENCE") or "").strip(),
            "device_id": (env.get("MINDOS_DEVICE_ID") or "").strip(),
            "required_scope": (env.get("MINDOS_CONNECTIVITY_REQUIRED_SCOPE") or "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"缺少连接票据配置：{', '.join(missing)}")

        parsed = urlparse(required["jwks_url"])
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("MINDOS_CONNECTIVITY_JWKS_URL 必须是 HTTPS URL")

        algorithms = tuple(
            item.strip() for item in (env.get("MINDOS_CONNECTIVITY_JWT_ALGORITHMS") or "RS256").split(",") if item.strip()
        )
        allowed_algorithms = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}
        if not algorithms or any(item not in allowed_algorithms for item in algorithms):
            raise ValueError("MINDOS_CONNECTIVITY_JWT_ALGORITHMS 包含不允许的算法")
        try:
            max_ttl_seconds = int(env.get("MINDOS_CONNECTIVITY_MAX_TTL_SECONDS") or "600")
        except ValueError as exc:
            raise ValueError("MINDOS_CONNECTIVITY_MAX_TTL_SECONDS 必须是整数") from exc
        if not 30 <= max_ttl_seconds <= 3600:
            raise ValueError("MINDOS_CONNECTIVITY_MAX_TTL_SECONDS 必须在 30 到 3600 秒之间")

        return cls(
            **required,
            algorithms=algorithms,
            max_ttl_seconds=max_ttl_seconds,
        )


class JwtConnectivityTicketVerifier:
    """基于 Consumer API JWKS 的严格 JWT 验签器。"""

    def __init__(self, config: JwtTicketVerifierConfig, jwks_client: object | None = None) -> None:
        self._config = config
        self._jwks_client = jwks_client if jwks_client is not None else PyJWKClient(config.jwks_url)

    def verify(self, token: str, *, method: str, path: str) -> ConnectivityPrincipal:
        del method, path  # P0 使用一个受控业务 scope；细粒度路由 scope 由后续合同扩展。
        try:
            header = jwt.get_unverified_header(token)
            algorithm = str(header.get("alg") or "")
            if algorithm not in self._config.algorithms:
                raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_INVALID", "设备连接票据算法不被接受")
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={
                    "require": [
                        "exp", "iat", "nbf", "account_id", "client_id", "device_id",
                        "scope", "nonce", "epoch_generation", "connect_before",
                    ],
                },
            )
        except ConnectivityTicketError:
            raise
        except PyJWKClientError as exc:
            raise ConnectivityTicketError(
                503,
                "CONNECTIVITY_TICKET_JWKS_UNAVAILABLE",
                "设备连接票据公钥暂不可用",
            ) from exc
        except InvalidTokenError as exc:
            raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_INVALID", "设备连接票据无效或已过期") from exc

        account_id = _required_claim(claims, "account_id")
        client_id = _required_claim(claims, "client_id")
        device_id = _required_claim(claims, "device_id")
        nonce = _required_claim(claims, "nonce")
        epoch_generation = _required_int_claim(claims, "epoch_generation")
        if device_id != self._config.device_id:
            raise ConnectivityTicketError(403, "CONNECTIVITY_TICKET_DEVICE_MISMATCH", "设备连接票据不属于当前设备")
        scopes = _parse_scopes(claims.get("scope"))
        if self._config.required_scope not in scopes:
            raise ConnectivityTicketError(403, "CONNECTIVITY_TICKET_SCOPE_DENIED", "设备连接票据权限不足")

        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
        if expires_at - issued_at > self._config.max_ttl_seconds:
            raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_INVALID", "设备连接票据有效期超出限制")

        # 首次连接窗口：票据必须在 connect_before 之前完成交换/建立连接，
        # 否则即使未到 exp 也不得再使用（防止票据长期闲置被冒用）。
        connect_before = _required_int_claim(claims, "connect_before")
        if not issued_at <= connect_before <= expires_at:
            raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_INVALID", "设备连接票据首次连接窗口非法")
        if connect_before < int(time.time()):
            raise ConnectivityTicketError(
                401,
                "CONNECTIVITY_TICKET_CONNECT_WINDOW_CLOSED",
                "设备连接票据已超过首次连接窗口",
            )

        # 阶段 2 本地安全约束：ACL、撤销、连接 epoch、nonce 重放（全部 fail-closed）。
        if connectivity_store.is_device_denied(device_id=device_id):
            raise ConnectivityTicketError(403, "CONNECTIVITY_TICKET_DEVICE_DENIED", "设备连接已被管理员禁用")
        if connectivity_store.is_revoked(account_id=account_id, client_id=client_id, device_id=device_id) is not None:
            raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_REVOKED", "设备连接票据已被撤销")
        current_epoch = connectivity_store.current_epoch(
            account_id=account_id,
            client_id=client_id,
            device_id=device_id,
        )
        if current_epoch and epoch_generation != current_epoch:
            raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_EPOCH_STALE", "设备连接票据 epoch 已失效")
        if not connectivity_store.consume_nonce(
            account_id=account_id,
            client_id=client_id,
            device_id=device_id,
            nonce=nonce,
            expires_at=float(expires_at),
        ):
            raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_NONCE_REUSED", "设备连接票据 nonce 已使用")

        return ConnectivityPrincipal(
            account_id=account_id,
            client_id=client_id,
            device_id=device_id,
            scopes=frozenset(scopes),
            expires_at=expires_at,
            nonce=nonce,
            epoch_generation=epoch_generation,
        )


def _required_claim(claims: Mapping[str, object], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_INVALID", "设备连接票据缺少必要声明")
    return value.strip()


def _required_int_claim(claims: Mapping[str, object], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_INVALID", "设备连接票据缺少必要整数声明")
    return value


def _parse_scopes(value: object) -> frozenset[str]:
    if isinstance(value, str):
        scopes = {item for item in value.split() if item}
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        scopes = {item.strip() for item in value if item.strip()}
    else:
        scopes = set()
    if not scopes:
        raise ConnectivityTicketError(401, "CONNECTIVITY_TICKET_INVALID", "设备连接票据 scope 无效")
    return frozenset(scopes)


class _UnavailableVerifier:
    """外部合同尚未接入时的默认实现，任何 Bearer 票据都不能放行。"""

    def verify(self, token: str, *, method: str, path: str) -> ConnectivityPrincipal:
        del token, method, path
        raise ConnectivityTicketError(
            503,
            "CONNECTIVITY_TICKET_VERIFIER_UNAVAILABLE",
            "设备连接票据验证尚未配置",
        )


_lock = Lock()
_verifier: ConnectivityTicketVerifier = _UnavailableVerifier()


def configure_ticket_verifier(verifier: ConnectivityTicketVerifier | None) -> None:
    """由阶段 2 Consumer Adapter 在启动期注入真实验签器；None 恢复默认拒绝。"""
    global _verifier
    with _lock:
        _verifier = verifier if verifier is not None else _UnavailableVerifier()


def configure_ticket_verifier_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """启动期加载公开 JWKS 配置；无配置或非法配置时保持默认拒绝。"""
    try:
        config = JwtTicketVerifierConfig.from_environment(environ)
    except ValueError:
        configure_ticket_verifier(None)
        return "unconfigured"
    configure_ticket_verifier(JwtConnectivityTicketVerifier(config))
    return "configured"


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise ConnectivityTicketError(
            401,
            "CONNECTIVITY_TICKET_REQUIRED",
            "缺少设备连接票据",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ConnectivityTicketError(
            401,
            "CONNECTIVITY_TICKET_INVALID",
            "设备连接票据格式无效",
        )
    return token.strip()


def authenticate_connectivity_ticket(
    authorization: str | None,
    *,
    method: str,
    path: str,
) -> ConnectivityPrincipal:
    """提取并验证 Bearer 票据；绝不记录或返回明文 token。"""
    token = _extract_bearer(authorization)
    with _lock:
        verifier = _verifier
    return verifier.verify(token, method=method, path=path)
