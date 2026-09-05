"""MindOS 治理待办（P11）。

把"发现问题"变成"用户可控处理"：
- 识别疑似重复、可能过时、待确认关联三类候选。
- 待办展示涉及对象、理由、片段与影响预览。
- 支持忽略、合并、归档三种人工仲裁。
- 仲裁前不修改任何知识卡片或原材料。
- 合并只允许改知识卡片；原材料归档仅改变状态，不自动物理删除。
"""
import json
import logging
import re
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from embedder import embed_query

from . import knowledge, related
from .services import ingestion
from .stores import governance_store as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mindos", tags=["mindos-governance"])

# 扫描阈值与数量上限
_DUPLICATE_MIN_SCORE = 0.50
_DUPLICATE_PER_CARD = 3
_RELATION_PER_CARD = 2

# 观点冲突：成对的对立关键词（命中任一即可判定为相反观点倾向）
_CONFLICT_PAIRS = [
    ("支持", "反对"),
    ("应该", "不应该"),
    ("必须", "禁止"),
    ("利好", "利空"),
    ("上涨", "下跌"),
    ("推荐", "不推荐"),
    ("同意", "不同意"),
    ("有效", "无效"),
    ("安全", "危险"),
    ("正确", "错误"),
]


class ResolveRequest(BaseModel):
    action: str  # ignore | merge
    note: str = ""
    keepKnowledgeId: str | None = None  # merge 时用户明确选择保留的主卡片


class RescanRequest(BaseModel):
    pass


def _card_text(page: dict) -> str:
    content = str(page.get("content") or "")
    try:
        _, body = knowledge.wiki_store._parse_frontmatter(content)
        return body.strip()[:500]
    except Exception:
        return content[:500]


def _conflict_polarity(text: str) -> tuple[int, str]:
    """返回文本的对立观点倾向。命中的对立词返回 (索引+1, 命中的词)，未命中返回 (0, '')。

    索引用于成对判断：两张卡片各自命中同一对对立词的两端时判定为观点冲突。
    """
    for idx, (word_a, word_b) in enumerate(_CONFLICT_PAIRS):
        if word_a in text and word_b not in text:
            return idx + 1, word_a
        if word_b in text and word_a not in text:
            return -(idx + 1), word_b
    return 0, ""


def _is_conflicting(text_a: str, text_b: str) -> tuple[bool, str]:
    """判断两张卡片是否观点相反：各自命中同一组对立词的两端。"""
    pol_a, _ = _conflict_polarity(text_a)
    pol_b, _ = _conflict_polarity(text_b)
    if pol_a == 0 or pol_b == 0:
        return False, ""
    # 同一组对立词（绝对值相同）但方向相反
    if abs(pol_a) == abs(pol_b) and pol_a * pol_b < 0:
        word_a = _CONFLICT_PAIRS[abs(pol_a) - 1][0]
        word_b = _CONFLICT_PAIRS[abs(pol_a) - 1][1]
        return True, f"检测到观点相反：「{word_a}」与「{word_b}」"
    return False, ""


def _rebuild_content(page: dict, extra_meta: dict, body: str | None = None) -> str:
    """按现有 frontmatter + 额外字段重建卡片内容（不丢失其余字段）。

    body 为 None 时保留原正文，否则覆盖正文。
    """
    content = str(page.get("content") or "")
    try:
        meta, old_body = knowledge.wiki_store._parse_frontmatter(content)
    except Exception:
        meta, old_body = {}, content
    meta.update(extra_meta)
    new_body = body if body is not None else old_body
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + new_body


def _is_material_available(material_id: str) -> bool:
    """材料已登记且处理状态为 available 才算可用。"""
    try:
        record = ingestion.status_of(material_id)
    except Exception:
        return False
    return record is not None and record.get("status") == "available"


def scan() -> dict:
    """扫描三类治理候选并写入待办存储，返回新增数量。

    注意：扫描不恢复 processing 中间态——运行中的服务绝不自动回收仍在执行的仲裁，
    遗留 processing 仅在服务重启时由 recover_stale_processing() 清理。
    """
    cards = knowledge.knowledge_list(limit=500).get("items", [])
    created = 0

    # ---- 1. 疑似重复：内容高度相似的知识卡片对 ----
    for card in cards:
        text = _card_text(_find_page(card["knowledgeId"])) or card.get("content", "")
        if not text.strip():
            continue
        try:
            similar = knowledge.search_cards(text, limit=_DUPLICATE_PER_CARD * 4)
        except Exception:
            similar = []
        count = 0
        for hit in similar:
            other_id = hit.get("knowledgeId")
            score = float(hit.get("score") or 0.0)
            if not other_id or other_id == card["knowledgeId"] or score < _DUPLICATE_MIN_SCORE:
                continue
            a, b = sorted((card["knowledgeId"], other_id))
            created += store.instance().create([{
                "kind": store.KIND_DUPLICATE,
                "title": f"疑似重复：{card['title']} 与 {hit['title']}",
                "reason": f"内容相似度 {score:.0%}，建议人工确认后合并或忽略。",
                "snippet": (hit.get("snippet") or "")[:300],
                "source_knowledge_id": a,
                "target_knowledge_id": b,
                "score": score,
                "fingerprint": f"duplicate:{a}:{b}",
            }])
            count += 1
            if count >= _DUPLICATE_PER_CARD:
                break

    # ---- 2. 观点冲突：内容相关但观点相反的知识卡片对 ----
    for card in cards:
        text = _card_text(_find_page(card["knowledgeId"])) or card.get("content", "")
        if not text.strip():
            continue
        try:
            similar = knowledge.search_cards(text, limit=_DUPLICATE_PER_CARD * 4)
        except Exception:
            similar = []
        for hit in similar:
            other_id = hit.get("knowledgeId")
            if not other_id or other_id == card["knowledgeId"]:
                continue
            # 避免与疑似重复重复提审：冲突对象应语义相关但观点相反
            other_text = _card_text(_find_page(other_id)) or hit.get("snippet", "")
            conflicting, reason = _is_conflicting(text, other_text)
            if not conflicting:
                continue
            a, b = sorted((card["knowledgeId"], other_id))
            created += store.instance().create([{
                "kind": store.KIND_CONFLICT,
                "title": f"观点冲突：{card['title']} 与 {hit['title']}",
                "reason": f"{reason}。建议人工确认观点并决定保留哪一张。",
                "snippet": (hit.get("snippet") or "")[:300],
                "source_knowledge_id": a,
                "target_knowledge_id": b,
                "score": max(float(hit.get("score") or 0.0), 0.6),
                "fingerprint": f"conflict:{a}:{b}",
            }])

    # ---- 3. 可能过时：卡片引用的来源资料不可用 ----
    for card in cards:
        page = _find_page(card["knowledgeId"])
        source_ids = knowledge._source_ids(page)
        for mid in source_ids:
            if _is_material_available(mid):
                continue
            created += store.instance().create([{
                "kind": store.KIND_OUTDATED,
                "title": f"可能过时：{card['title']} 引用的来源资料不可用",
                "reason": "该知识卡片登记的来源原材料不存在、处理中或处理失败，卡片内容可能过时。",
                "snippet": (card.get("content") or "")[:300],
                "source_knowledge_id": card["knowledgeId"],
                "material_id": mid,
                "score": 0.9,
                "fingerprint": f"outdated:{card['knowledgeId']}:{mid}",
            }])

    # ---- 4. 待确认关联：卡片与原材料的内容相似 / 共享标签候选 ----
    for card in cards:
        page = _find_page(card["knowledgeId"])
        tags = knowledge._tags(page)
        text = _card_text(page)
        try:
            embedding = embed_query(text) if text.strip() else []
        except Exception:
            embedding = []
        candidates = {}
        for item in related._similar_materials(embedding or [], card["knowledgeId"], limit=_RELATION_PER_CARD):
            # 推荐服务异常/历史数据可能混入非原材料项；治理扫描只接受带 materialId
            # 的候选，不能因一条脏数据中断整个扫描。
            mid = str(item.get("materialId") or "")
            if not mid:
                continue
            candidates[mid] = (item.get("score", 0.0), str(item.get("reason") or "内容相似"))
        for item in related._shared_tag_materials(tags, card["knowledgeId"], limit=_RELATION_PER_CARD):
            mid = str(item.get("materialId") or "")
            if not mid:
                continue
            if mid not in candidates or item.get("score", 0) > candidates[mid][0]:
                candidates[mid] = (item.get("score", 0.0), str(item.get("reason") or "共享标签"))
        # 已确认的来源关系（mindos_source_material_ids）与已回收原材料不再生成候选。
        confirmed_source_ids = set(knowledge._source_ids(page))
        recycled = ingestion.recycled_material_ids()
        candidates = {
            mid: value
            for mid, value in candidates.items()
            if mid not in confirmed_source_ids and mid not in recycled
        }
        for mid, (score, reason) in sorted(candidates.items(), key=lambda x: x[1][0], reverse=True)[:_RELATION_PER_CARD]:
            created += store.instance().create([{
                "kind": store.KIND_RELATION,
                "title": f"待确认关联：{card['title']} ↔ 原材料",
                "reason": f"依据：{reason}。人工确认后才成为正式关联。",
                "snippet": "",
                "source_knowledge_id": card["knowledgeId"],
                "material_id": mid,
                "score": score,
                "fingerprint": f"relation:{card['knowledgeId']}:{mid}",
            }])

    return {"scanned": len(cards), "created": created}


def _find_page(knowledge_id: str) -> dict:
    try:
        return knowledge._find(knowledge_id)
    except HTTPException:
        return {"path": "", "content": "", "title": knowledge_id}


def _merge_text_key(text: str) -> str:
    """生成用于合并去重的正文指纹，不修改用户原始正文。"""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line == "---" or line.startswith("## 合并自："):
            continue
        if re.fullmatch(r"#{1,6}\s+.*", line):
            continue
        lines.append(re.sub(r"\s+", "", line).lower())
    return "".join(lines)


def _merged_source_ids(page: dict) -> list[str]:
    try:
        meta, _ = knowledge.wiki_store._parse_frontmatter(str(page.get("content") or ""))
    except Exception:
        return []
    values = meta.get("mindos_merged_source_ids") or []
    return [str(value) for value in values if str(value).strip()]


def _merge_knowledge(source_id: str, target_id: str) -> None:
    """幂等合并：正文去重后并入主卡片，再标记目标卡片不可见。

    先把 target ID 写入 source 的 frontmatter。若第二次写 target 时进程异常，重试会
    识别该标记并只补齐 target 的归档状态，不会再次追加正文。
    """
    if source_id == target_id:
        raise HTTPException(400, "不能合并同一张知识卡片")
    source = knowledge._find(source_id)
    target = knowledge._find(target_id)
    try:
        target_meta, _ = knowledge.wiki_store._parse_frontmatter(str(target.get("content") or ""))
    except Exception:
        target_meta = {}
    merged_into = str(target_meta.get("mindos_merged_into") or "")
    if merged_into and merged_into != source_id:
        raise HTTPException(409, "该卡片已合并到另一张知识卡片")

    _, source_body = knowledge.wiki_store._parse_frontmatter(str(source.get("content") or ""))
    target_body = knowledge._card_body(target)
    merged_ids = _merged_source_ids(source)
    source_key = _merge_text_key(source_body)
    target_key = _merge_text_key(target_body)
    should_append = bool(target_key) and target_id not in merged_ids and target_key not in source_key
    merged_body = source_body.rstrip()
    if should_append:
        merged_body += f"\n\n---\n\n## 合并自：{target.get('title') or target_id}\n\n{target_body.rstrip()}"
    if target_id not in merged_ids:
        merged_ids.append(target_id)
    updated_source = knowledge.wiki_store.write_page(
        str(source["path"]),
        _rebuild_content(source, {
            "updated_at": _now_iso(),
            "mindos_merged_source_ids": merged_ids,
        }, body=merged_body),
        source_agent="mindos",
    )
    updated_target = knowledge.wiki_store.write_page(
        str(target["path"]),
        _rebuild_content(target, {"mindos_merged_into": source_id, "mindos_archived": True}),
        source_agent="mindos",
    )
    # 合并会同时改变主卡片正文并归档被合并卡片；两张卡片的独立向量索引都必须同步。
    knowledge._sync_card_index(updated_source)
    knowledge._sync_card_index(updated_target)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _claim_valid(item_id: str, claim_token: str) -> bool:
    """实体操作前的二次校验：确认仍持有 processing claim（未被并发抢占覆盖）。"""
    item = store.instance().get(item_id)
    return (
        item is not None
        and item["status"] == store.STATUS_PROCESSING
        and store.instance().current_claim(item_id) == claim_token
    )


def recover_stale_processing() -> int:
    """服务启动时恢复进程崩溃遗留的 processing 中间态。

    运行中的服务绝不自动恢复（避免回收仍在执行的仲裁导致重复合并/归档），
    仅服务重启（所有请求进程已终止）后调用一次。
    """
    return store.instance().recover_processing()


def resolve_item(item_id: str, req: ResolveRequest) -> dict:
    item = store.instance().get(item_id)
    if item is None:
        raise HTTPException(404, "治理待办不存在")
    if item["status"] != store.STATUS_PENDING:
        raise HTTPException(409, "该待办已处理，不能重复仲裁")
    action = req.action
    if action not in ("ignore", "merge"):
        raise HTTPException(400, "action 必须是 ignore/merge")

    source_id = item.get("sourceKnowledgeId")
    target_id = item.get("targetKnowledgeId")
    keep = req.keepKnowledgeId
    other_id = None

    if action == "merge":
        if item["kind"] != store.KIND_DUPLICATE:
            raise HTTPException(400, "只有疑似重复候选项可以合并")
        if not source_id or not target_id:
            raise HTTPException(400, "缺少合并所需的两个知识卡片")
        if not keep or keep not in (source_id, target_id):
            raise HTTPException(400, "请明确选择要保留的主卡片 keepKnowledgeId")
        other_id = target_id if keep == source_id else source_id
    final_status = {
        "merge": store.STATUS_MERGED,
        "ignore": store.STATUS_IGNORED,
    }[action]

    # 两阶段提交：先原子抢占（pending → processing，带唯一 claim token），再执行实体操作，最后转最终状态。
    # 完成与回滚都必须携带原 claim token，仅匹配当前 processing 记录，避免旧请求覆盖新一次抢占。
    claim_token = uuid.uuid4().hex[:16]
    claimed = store.instance().resolve(
        item_id, store.STATUS_PROCESSING, req.note,
        from_status=store.STATUS_PENDING, claim_token=claim_token,
    )
    if claimed is None:
        raise HTTPException(409, "该待办已处理，不能重复仲裁")

    # 实体操作前二次校验：确认 claim 仍有效（未被并发抢占覆盖）。运行中不自动恢复，
    # 此校验作为纵深防御，防止极端的过期 claim 在异常路径下执行实体写入。
    if not _claim_valid(item_id, claim_token):
        raise HTTPException(409, "该待办已被并发处理，请刷新后重试")

    try:
        if action == "merge":
            _merge_knowledge(keep, other_id)
    except HTTPException as exc:
        store.instance().resolve(
            item_id, store.STATUS_PENDING, "", from_status=store.STATUS_PROCESSING, claim_token=claim_token)
        raise exc
    except Exception as exc:
        store.instance().resolve(
            item_id, store.STATUS_PENDING, "", from_status=store.STATUS_PROCESSING, claim_token=claim_token)
        logger.error("治理仲裁失败 %s: %s", item_id, exc)
        raise HTTPException(500, "仲裁失败")

    # 实体操作成功后转最终状态（仅当 claim 仍有效）。
    result = store.instance().resolve(
        item_id, final_status, req.note, from_status=store.STATUS_PROCESSING, claim_token=claim_token)
    if result is None:
        # claim 已失效（可能被并发覆盖），不做强制回滚以免覆盖新抢占。
        raise HTTPException(409, "该待办状态已变化，请刷新后重试")
    return result


@router.get("/governance")
def list_governance(
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    if status and status not in store._ALL_STATUSES:
        raise HTTPException(400, "不支持的状态筛选")
    if kind and kind not in store._ALL_KINDS:
        raise HTTPException(400, "不支持的候选类型筛选")
    items = store.instance().list(status, kind, limit)
    return {"items": items, "total": len(items)}


@router.get("/governance/stats")
def governance_stats():
    items = store.instance().list(limit=1000)
    stats = {
        "total": len(items),
        "pending": 0,
        "processing": 0,
        "ignored": 0,
        "merged": 0,
        "archived": 0,
        "duplicate": 0,
        "outdated": 0,
        "relation": 0,
        "conflict": 0,
    }
    for item in items:
        stats[item["status"]] = stats.get(item["status"], 0) + 1
        stats[item["kind"]] = stats.get(item["kind"], 0) + 1
    return stats


def configure_write_guard(guard) -> None:
    """由 server 在定义 require_local 后注入写操作防护。"""
    global router
    router = APIRouter(prefix="/api/mindos", tags=["mindos-governance"])
    router.add_api_route("/governance/rescan", governance_rescan, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/governance/{item_id}/resolve", governance_resolve, methods=["POST"], dependencies=[Depends(guard)])
    router.add_api_route("/governance", list_governance, methods=["GET"])
    router.add_api_route("/governance/stats", governance_stats, methods=["GET"])


def governance_rescan(req: RescanRequest):
    return scan()


def governance_resolve(item_id: str, req: ResolveRequest):
    return resolve_item(item_id, req)
