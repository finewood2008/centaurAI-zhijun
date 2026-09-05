"""Agent evidenceRef 签发、校验与证据展开（AG-02-02 签发；AG-02-03 展开）。

evidenceRef 是外部 Agent 读取证据的短期 opaque 句柄，不是 source_path、chunk ID
或可猜测 URL。本模块采用「短期内存记录」实现：

- ref 为随机 opaque token（ev_...），其中不含任何可读路径、chunk ID 或业务 ID；
- 记录绑定签发时的 clientId，任何跨 client 使用一律拒绝；
- 默认 10 分钟过期（EVIDENCE_TTL_SECONDS）；
- 资源生命周期（归档/回收/版本变化）在 resolve 时按当前状态重新复核，不因签发
  时间冻结；
- 审计只记录 ref 的稳定 digest，绝不记录明文 ref 与内部路径的对应关系。

resolve 规则（AG-02-03）：
- 一次最多 10 个 ref；maxCharsPerItem 最大 3000；单次总正文最多 12000 字；
- 任何无效/过期/跨 client/已归档/已回收/已失败 ref → 统一 404/RESOURCE_NOT_FOUND；
- 材料仍在解析/索引 → 409/EVIDENCE_NOT_READY（可重试）；
- 结果顺序与请求 ref 顺序一致；重复 ref 只返回一次并标记 deduplicated；
- 不因 resolve 重新解析、重新向量化或调用模型，仅读取索引中已有 chunk / 派生 part。
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import Counter

from . import config as agent_config
from .errors import AgentError

EVIDENCE_REF_PREFIX = "ev_"
EVIDENCE_TTL_SECONDS = 600

# AG-02-03 预算（与 agent_config.EVIDENCE_CHARS_MAX 对应单次总上限）
EVIDENCE_REFS_MAX = 10
EVIDENCE_MAX_CHARS_PER_ITEM = 3000

# 材料状态（与 ingestion 模块常量保持一致，避免硬编码散落）
_ST_AVAILABLE = "available"
_ST_PROCESSING = "processing"
_ST_UPLOADED = "uploaded"

_LOCK = threading.Lock()
# ref -> 内部记录；内存记录满足「短期、client 绑定」约束，进程重启后 ref 自然失效。
_RECORDS: dict[str, dict] = {}


def ref_digest(ref: str) -> str:
    """审计用稳定摘要；绝不把明文 ref 与内部路径对应关系写入日志/审计。"""
    normalized = "evidence-ref\x1f" + str(ref or "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def sign_evidence_ref(
    *,
    client_id: str,
    source_type: str,
    source_id: str,
    chunk_key: str | None = None,
    source_path: str | None = None,
    title: str | None = None,
    ttl_seconds: int = EVIDENCE_TTL_SECONDS,
) -> str:
    """为一次检索命中签发 evidenceRef。

    source_path / chunk_key 仅保存在服务端内存记录中，绝不进入对外响应；
    ref 本身不含任何可反推本地路径的信息。
    """
    now = time.time()
    ref = EVIDENCE_REF_PREFIX + secrets.token_urlsafe(16)
    record = {
        "ref": ref,
        "client_id": client_id,
        "source_type": source_type,
        "source_id": source_id,
        "chunk_key": chunk_key,
        "source_path": source_path,
        "title": title,
        "created_at": now,
        "expires_at": now + float(ttl_seconds),
    }
    with _LOCK:
        _RECORDS[ref] = record
    return ref


def verify_evidence_ref(client_id: str, ref: str) -> dict | None:
    """校验 client 绑定与过期，返回内部记录；无效/过期/跨 client 返回 None。

    仅校验句柄本身有效；资源是否仍可读取（归档/回收/版本状态）由 resolve 时
    按当前生命周期复核，避免签发后生命周期变化被绕过。
    """
    if not ref or not ref.startswith(EVIDENCE_REF_PREFIX):
        return None
    with _LOCK:
        record = _RECORDS.get(ref)
        if record is None:
            return None
        if record.get("client_id") != client_id:
            return None
        if float(record.get("expires_at") or 0) < time.time():
            return None
        return dict(record)


def reset_for_tests() -> None:
    """测试用：清空全部证据记录。"""
    with _LOCK:
        _RECORDS.clear()


# ---- 证据展开（AG-02-03）--------------------------------------------------

def _revalidate_lifecycle(record: dict) -> None:
    """按当前生命周期与处理状态复核证据是否仍可读取。

    - knowledge：active 且正文有效才可读，否则 404；
    - material：归档/回收/不存在/失败 → 404；处理中 → 409/EVIDENCE_NOT_READY。
    """
    source_type = record.get("source_type")
    if source_type == "knowledge":
        from .. import knowledge

        if knowledge.evidence_body(record["source_id"]) is None:
            raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")
        return
    from ..services import search_service
    from ..services.ingestion import recycled_material_ids, status_of
    material_id = record["source_id"]
    recycled = recycled_material_ids()
    # 生命周期规则只复用统一检索服务的单一维护点，Agent 层不复制归档/回收条件。
    if search_service.is_material_excluded(material_id, set(), recycled):
        raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")
    public = status_of(material_id)
    if public is None:
        raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")
    if public.get("status") != _ST_AVAILABLE:
        if public.get("status") in (_ST_PROCESSING, _ST_UPLOADED):
            raise AgentError(
                409,
                "EVIDENCE_NOT_READY",
                "材料仍在解析/索引中，请稍后重试",
                retryable=True,
            )
        # 失败材料不能伪装为可检索证据
        raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")


def _resolve_evidence(
    record: dict, *, max_chars_per_item: int, include_locator: bool
) -> tuple[dict, int]:
    """展开单条证据；只读取索引中已有内容，不重新解析/向量化。"""
    source_type = record.get("source_type")
    if source_type == "knowledge":
        from .. import knowledge

        body = knowledge.evidence_body(record["source_id"]) or ""
        truncated = len(body) > max_chars_per_item
        text = body[:max_chars_per_item] if truncated else body
        item = {
            "evidenceRef": record["ref"],
            "sourceType": "knowledge",
            "sourceId": record["source_id"],
            "title": str(record.get("title") or ""),
            "text": text,
            "locator": None,
            "truncated": truncated,
        }
        return item, len(text)

    from vector_store import get_chunks_by_ids
    from ..services import search_service
    from ..services.ingestion import material_for_source

    # 材料句柄必须绑定精确命中的 chunk_key：无 chunk_key 时不展开（避免回退为
    # 该材料首个分块，导致引用与问答/搜索实际命中不一致）。
    chunk = None
    if record.get("chunk_key"):
        found = get_chunks_by_ids([record["chunk_key"]])
        if found:
            chunk = found[0]
    # 版本/索引变化导致 chunk 不存在时，证据不可用（不返回猜测正文）。
    if chunk is None:
        raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")
    # chunk 归属校验：stale chunk / ID 冲突 / 错误关联时，不得返回与 evidenceRef
    # 所属材料不一致的正文——chunk 的 source_path 必须与签发记录一致，且该路径仍
    # 映射到签发时的 source_id。
    chunk_source = str(chunk.get("source_path") or "")
    if not chunk_source or chunk_source != str(record.get("source_path") or ""):
        raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")
    mapped = material_for_source(chunk_source)
    if mapped is None or str(mapped.get("material_id") or "") != str(record.get("source_id") or ""):
        raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")
    text = str(chunk.get("text") or "").strip()
    truncated = len(text) > max_chars_per_item
    text = text[:max_chars_per_item] if truncated else text
    locator = (
        search_service.build_material_locator(record["source_id"], chunk.get("metadata") or {})
        if include_locator
        else None
    )
    item = {
        "evidenceRef": record["ref"],
        "sourceType": "material",
        "sourceId": record["source_id"],
        "title": str(record.get("title") or ""),
        "text": text,
        "locator": locator,
        "truncated": truncated,
    }
    return item, len(text)


def resolve_evidence_batch(
    client_id: str,
    refs: list[str],
    *,
    max_chars_per_item: int = EVIDENCE_MAX_CHARS_PER_ITEM,
    include_locator: bool = True,
) -> dict:
    """展开证据批次，返回 {"items": [...], "totalChars": N}。

    顺序与请求 ref 顺序一致；重复 ref 只返回一次并标记 deduplicated。任何
    无效/过期/跨 client/生命周期不可用的 ref 统一使整个请求返回 404。
    """
    if not refs or len(refs) > EVIDENCE_REFS_MAX:
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"evidenceRefs 最多 {EVIDENCE_REFS_MAX} 个",
        )
    if max_chars_per_item < 1 or max_chars_per_item > EVIDENCE_MAX_CHARS_PER_ITEM:
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"maxCharsPerItem 必须在 1–{EVIDENCE_MAX_CHARS_PER_ITEM} 之间",
        )
    # 单次总正文上限（确定性预算校验）：去重后按每项上限估算。
    if len(set(refs)) * max_chars_per_item > int(agent_config.EVIDENCE_CHARS_MAX):
        raise AgentError(
            413,
            "CONTENT_LIMIT_EXCEEDED",
            "证据总量超出单次上限，请减少 evidenceRefs 或 maxCharsPerItem",
        )

    # 句柄校验：client 绑定 + 过期；任何无效 ref 统一 404，防枚举。
    records = []
    for ref in refs:
        record = verify_evidence_ref(client_id, ref)
        if record is None:
            raise AgentError(404, "RESOURCE_NOT_FOUND", "证据不存在或当前不可访问")
        records.append(record)
    # 生命周期复核（不因签发时间冻结当前可见性）。
    for record in records:
        _revalidate_lifecycle(record)

    counts = Counter(refs)
    seen: set[str] = set()
    items: list[dict] = []
    total_chars = 0
    for ref, record in zip(refs, records):
        if ref in seen:
            continue
        seen.add(ref)
        item, n = _resolve_evidence(
            record,
            max_chars_per_item=int(max_chars_per_item),
            include_locator=include_locator,
        )
        if counts[ref] > 1:
            item["deduplicated"] = True
        items.append(item)
        total_chars += n
    return {"items": items, "totalChars": total_chars}
