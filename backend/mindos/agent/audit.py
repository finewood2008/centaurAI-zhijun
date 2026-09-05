"""Agent Gateway 审计与 traceId（AG-01 骨架）。

原则：token 明文、完整上传正文、完整问答上下文、模型密钥绝不写入审计。
request_digest / response_digest 是脱敏字段的稳定哈希，而非原请求全文。
"""
from __future__ import annotations

import hashlib
import json
import secrets

from . import store as agent_store

TRACE_ID_MAX = 200


def new_trace_id() -> str:
    return "atr_" + secrets.token_urlsafe(12)


def normalize_trace_id(value) -> str:
    """校验并规整外部传入的 X-Request-Id；非法时丢弃并生成新 ID。"""
    raw = str(value or "").strip()
    if not raw or len(raw) > TRACE_ID_MAX or any(ch in raw for ch in ("\r", "\n", "\x00")):
        return new_trace_id()
    return raw


def stable_digest(*parts) -> str:
    """对一组非敏感字段做稳定哈希，形成脱敏摘要。"""
    normalized = "\x1f".join(
        str(p) if p is not None else ""
        for p in parts
    ).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def response_digest(data) -> str:
    """对响应数据做规范序列化哈希（AG-01 响应不含 token/路径等敏感字段）。"""
    try:
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return stable_digest(type(data).__name__)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def record(
    *,
    trace_id: str,
    client_id: str,
    action: str,
    scope: str = "",
    resource_type: str = "",
    resource_id: str = "",
    outcome: str = "ok",
    status_code: int = 200,
    request_digest: str = "",
    response_digest: str = "",
    latency_ms: int = 0,
) -> None:
    agent_store.instance().record_audit(
        trace_id=trace_id,
        client_id=client_id,
        action=action,
        scope=scope,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        status_code=status_code,
        request_digest=request_digest,
        response_digest=response_digest,
        latency_ms=latency_ms,
    )
