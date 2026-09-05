"""连接票据的一次性交换与受控会话验证。

票据是单次使用凭证：`exchange_ticket` 每次交换只验签并消费一次 nonce，
随后把会话登记到 connectivity_sessions（生产调用点，撤销/epoch 轮换可关闭）。
业务请求携带 `X-MindOS-Session` 会话凭证，`validate_session` 逐请求
fail-closed 验证：会话存在、未关闭、未过期、tuple 未撤销、epoch 未失效、
设备未被 ACL 禁用。后续接入 P2P/长连接时，同一会话凭证即该连接的标识。
"""

from __future__ import annotations

import hashlib
import secrets
import time

from .connectivity_ticket import (
    ConnectivityPrincipal,
    ConnectivityTicketError,
    authenticate_connectivity_ticket,
)
from .stores import connectivity_store

SESSION_HEADER = "X-MindOS-Session"
EXCHANGE_PATH = "/api/mindos/connectivity/sessions/exchange"


def _hash_token(token: str) -> str:
    """会话凭证以 SHA-256 摘要入库，数据库不保存明文凭证。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def exchange_ticket(authorization: str | None) -> dict:
    """用连接票据交换受控会话凭证（票据一次性使用，nonce 在验签期被消费）。"""
    principal = authenticate_connectivity_ticket(
        authorization,
        method="POST",
        path=EXCHANGE_PATH,
    )
    raw_token = secrets.token_hex(32)
    session_id = _hash_token(raw_token)
    connectivity_store.register_session(
        session_id=session_id,
        account_id=principal.account_id,
        client_id=principal.client_id,
        device_id=principal.device_id,
        epoch_generation=principal.epoch_generation,
        expires_at=float(principal.expires_at),
        scopes=" ".join(sorted(principal.scopes)),
    )
    return {
        "sessionToken": raw_token,
        "sessionId": session_id,
        "deviceId": principal.device_id,
        "accountId": principal.account_id,
        "clientId": principal.client_id,
        "epochGeneration": principal.epoch_generation,
        "expiresAt": principal.expires_at,
    }


def validate_session(session_token: str, *, method: str, path: str) -> ConnectivityPrincipal:
    """验证会话凭证并返回设备主身份；全部 fail-closed。"""
    del method, path
    session_id = _hash_token(session_token)
    session = connectivity_store.get_session(session_id=session_id)
    if session is None:
        raise ConnectivityTicketError(401, "CONNECTIVITY_SESSION_INVALID", "连接会话无效或已关闭")
    account_id = session["account_id"]
    client_id = session["client_id"]
    device_id = session["device_id"]
    if connectivity_store.is_device_denied(device_id=device_id):
        raise ConnectivityTicketError(403, "CONNECTIVITY_SESSION_DEVICE_DENIED", "设备连接已被管理员禁用")
    # 撤销/epoch 轮换会同时关闭会话；先给调用方可行动的失败码，再兜底 closed。
    if connectivity_store.is_revoked(
        account_id=account_id,
        client_id=client_id,
        device_id=device_id,
    ) is not None:
        raise ConnectivityTicketError(401, "CONNECTIVITY_SESSION_REVOKED", "设备连接已被撤销")
    if float(session["expires_at"]) <= time.time():
        raise ConnectivityTicketError(401, "CONNECTIVITY_SESSION_EXPIRED", "连接会话已过期")
    current_epoch = connectivity_store.current_epoch(
        account_id=account_id,
        client_id=client_id,
        device_id=device_id,
    )
    if current_epoch and int(session["epoch_generation"]) != current_epoch:
        raise ConnectivityTicketError(401, "CONNECTIVITY_SESSION_EPOCH_STALE", "连接会话 epoch 已失效")
    if session.get("closed_at") is not None:
        raise ConnectivityTicketError(401, "CONNECTIVITY_SESSION_INVALID", "连接会话已关闭")
    return ConnectivityPrincipal(
        account_id=account_id,
        client_id=client_id,
        device_id=device_id,
        scopes=frozenset(str(session.get("scopes") or "").split()),
        expires_at=int(session["expires_at"]),
        nonce="",
        epoch_generation=int(session["epoch_generation"]),
    )
