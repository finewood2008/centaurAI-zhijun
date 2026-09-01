"""MindOS 相关内容召回（P9 / P14-09 可信 Top-3 关联推荐）。

P14-09：面向产品的关联推荐闭环。复用向量相似度、共享标签、关键词/文件名三类
召回来源，合并为统一推荐列表：
- RECOMMENDED_LIMIT=3 为对外产品上限；内部保留更大召回池（_RECALL_LIMIT）择优。
- 同一对象多来源命中时合并为一项，reasons 并列全部依据；同一对象不占多个名额。
- 每项返回稳定 reason、scoreBand（高/中）与 sourceType；不向用户暴露不可解释的
  原始模型分。
- 不足 3 个时如实返回 total 与 note（原因），绝不硬塞低相关对象凑数。
关联仅作候选展示，不创建图谱 confirmed edge；用户可点击查看或后续阶段确认。

阶段 2：全量召回（原材料/知识卡片）按请求票据身份推导的 device_scope 隔离，
跨设备/账号的对象不得作为关联候选回显。
"""
from fastapi import APIRouter, HTTPException, Request

from embedder import embed_query
from vector_store import search as vector_search, get_source_embedding
from annotations import get as _ann_get

from . import knowledge
from .stores import card_ledger_store
from .services import ingestion

router = APIRouter(prefix="/api/mindos", tags=["mindos-related"])

# 对外产品推荐上限；内部召回池更大，择优后截断。
RECOMMENDED_LIMIT = 3
_RECALL_LIMIT = 15
# 向量相似度阈值：过低会产生噪音，但缺失向量/低分时依赖关键词与文件名兜底
_MIN_SCORE = 0.15
_TEXT_LIMIT = 400
# scoreBand 分界：达到阈值但低于此分的归为“一般相关（medium）”，高于等于为“高可信（high）”
_BAND_HIGH = 0.7


def _device_scope(request: Request = None) -> str:
    """票据模式下按真实 device_id 生成业务数据作用域；调试模式为 global。"""
    from .device_context import scope_for_device

    context = getattr(getattr(request, "state", None), "mindos_device_context", None)
    return scope_for_device(getattr(context, "device_id", None))


def _hidden_material_ids(device_scope: str = "global") -> set[str]:
    """已回收原材料不应再作为关联候选（仅当前设备作用域内的回收记录）。"""
    hidden: set[str] = set()
    try:
        hidden |= set(ingestion.recycled_material_ids(device_scope=device_scope))
    except Exception:
        pass
    return hidden


def _material_candidate(material_id: str, record: dict, snippet: str, score: float, reason: str) -> dict:
    return {
        "id": material_id,
        "sourceType": "material",
        "title": record["file_name"],
        "snippet": snippet,
        "score": score,
        "reason": reason,
    }


def _knowledge_candidate(knowledge_id: str, title: str, snippet: str, score: float, reason: str) -> dict:
    return {
        "id": knowledge_id,
        "sourceType": "knowledge",
        "title": title,
        "snippet": snippet,
        "score": score,
        "reason": reason,
    }


def _similar_materials(embedding: list[float], exclude_id: str, limit: int = _RECALL_LIMIT, device_scope: str = "global") -> list[dict]:
    """Find materials with similar vector embeddings, excluding the source itself."""
    if not embedding:
        return []
    hidden = _hidden_material_ids(device_scope)
    chunks = vector_search(embedding, n_results=max(30, limit * 6))
    best: dict[str, dict] = {}
    for chunk in chunks:
        record = ingestion.material_for_source(str(chunk.get("source_path") or ""), device_scope=device_scope)
        if record is None:
            continue
        material_id = record["material_id"]
        if material_id == exclude_id or material_id in hidden:
            continue
        score = float(chunk.get("vector_score") or 0.0)
        if score < _MIN_SCORE:
            continue
        if material_id not in best or score > best[material_id]["score"]:
            best[material_id] = _material_candidate(
                material_id, record,
                snippet=str(chunk.get("text") or "")[:200],
                score=score, reason="内容相似",
            )
    return sorted(best.values(), key=lambda x: x["score"], reverse=True)[:limit]


def _keyword_materials(query: str, exclude_id: str, limit: int = _RECALL_LIMIT, device_scope: str = "global") -> list[dict]:
    """按正文关键词 / 文件名匹配召回相关原材料（向量缺失或召回不足时的兜底）。"""
    if not (query or "").strip():
        return []
    needle = query.strip().casefold()
    hidden = _hidden_material_ids(device_scope)
    results = []
    for rec in ingestion.JobStore.instance().list(device_scope=device_scope):
        material_id = rec["material_id"]
        if material_id == exclude_id or material_id in hidden:
            continue
        file_name = str(rec.get("file_name") or "")
        # 文件名精确匹配
        if needle and needle in file_name.casefold():
            results.append(_material_candidate(material_id, rec, file_name, 1.0, "文件名匹配"))
            continue
        # 正文关键词匹配：取已解析文本前若干字符做子串命中
        try:
            text = str(ingestion.detail_of(material_id, device_scope=device_scope).get("text") or "")[:_TEXT_LIMIT].casefold()
        except Exception:
            text = ""
        if needle and needle in text:
            results.append(_material_candidate(material_id, rec, text[:200], 0.7, "内容包含关键词"))
    return sorted(results, key=lambda x: x["score"], reverse=True)[:limit]


def _shared_tag_materials(tags: list[str], exclude_id: str, limit: int = _RECALL_LIMIT, device_scope: str = "global") -> list[dict]:
    """Find materials sharing at least one tag."""
    if not tags:
        return []
    hidden = _hidden_material_ids(device_scope)
    tag_set = {t.lower() for t in tags}
    results = []
    for rec in ingestion.JobStore.instance().list(device_scope=device_scope):
        if rec["material_id"] == exclude_id or rec["material_id"] in hidden:
            continue
        ann = _ann_get(str(rec["source_path"]))
        rec_tags = {t.lower() for t in ann.get("tags", [])}
        shared = tag_set & rec_tags
        if shared:
            results.append(_material_candidate(
                rec["material_id"], rec,
                "", len(shared) / len(tag_set), "共享标签",
            ))
    return sorted(results, key=lambda x: x["score"], reverse=True)[:limit]


def _similar_knowledge(text: str, exclude_id: str, limit: int = _RECALL_LIMIT, device_scope: str = "global") -> list[dict]:
    """Find knowledge cards with similar content."""
    if not text.strip():
        return []
    cards = knowledge.search_cards(text, limit=limit * 3, device_scope=device_scope)
    return [
        _knowledge_candidate(c["knowledgeId"], c["title"], c["snippet"][:200], c["score"], "内容相似")
        for c in cards if c["knowledgeId"] != exclude_id
    ][:limit]


def _shared_tag_knowledge(tags: list[str], exclude_id: str, limit: int = _RECALL_LIMIT, device_scope: str = "global") -> list[dict]:
    """Find knowledge cards sharing at least one tag."""
    if not tags:
        return []
    tag_set = {t.lower() for t in tags}
    results = []
    for page in knowledge.knowledge_list(limit=500, device_scope=device_scope).get("items", []):
        if page["knowledgeId"] == exclude_id:
            continue
        card_tags = {t.lower() for t in page.get("tags", [])}
        shared = tag_set & card_tags
        if shared:
            results.append(_knowledge_candidate(
                page["knowledgeId"], page["title"],
                str(page.get("content", ""))[:200],
                len(shared) / len(tag_set), "共享标签",
            ))
    return sorted(results, key=lambda x: x["score"], reverse=True)[:limit]


def _keyword_knowledge(query: str, exclude_id: str, limit: int = _RECALL_LIMIT, device_scope: str = "global") -> list[dict]:
    """按标题/正文关键词匹配召回相关知识卡片（向量召回不足时的兜底）。"""
    if not (query or "").strip():
        return []
    needle = query.strip().casefold()
    results = []
    for page in knowledge.knowledge_list(limit=500, device_scope=device_scope).get("items", []):
        if page["knowledgeId"] == exclude_id:
            continue
        title = str(page.get("title") or "").casefold()
        body = str(page.get("content") or "")[:_TEXT_LIMIT].casefold()
        if needle in title:
            results.append(_knowledge_candidate(
                page["knowledgeId"], page["title"],
                str(page.get("content", ""))[:200], 1.0, "标题匹配",
            ))
        elif needle in body:
            results.append(_knowledge_candidate(
                page["knowledgeId"], page["title"],
                str(page.get("content", ""))[:200], 0.7, "内容包含关键词",
            ))
    return sorted(results, key=lambda x: x["score"], reverse=True)[:limit]


def _merge_related(source_groups: list[list[dict]]) -> list[dict]:
    """按对象 ID 合并多来源召回：同一对象合并为一项，reasons 并列全部依据。

    不得让同一对象占多个名额；主 reason 取分数最高的来源，其余依据进 reasons。
    """
    merged: dict[str, dict] = {}
    for group in source_groups:
        for item in group:
            key = item["id"]
            existing = merged.get(key)
            if existing is None:
                merged[key] = {
                    "id": key,
                    "sourceType": item["sourceType"],
                    "title": item["title"],
                    "snippet": item.get("snippet", ""),
                    "score": item["score"],
                    "reason": item["reason"],
                    "reasons": [item["reason"]],
                }
                continue
            if item["score"] > existing["score"]:
                existing["score"] = item["score"]
                existing["reason"] = item["reason"]
                # 分数更高但无片段（如共享标签）时保留原有片段，避免内容丢失
                if item.get("snippet"):
                    existing["snippet"] = item["snippet"]
            if item["reason"] not in existing["reasons"]:
                existing["reasons"].append(item["reason"])
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)


def _score_band(score: float) -> str:
    """可信度档位：高/中，不向用户暴露不可解释的原始模型分。"""
    return "high" if score >= _BAND_HIGH else "medium"


def _recommend_response(merged: list[dict]) -> dict:
    """组装产品推荐响应：最多 RECOMMENDED_LIMIT 项；不足时如实说明原因，绝不凑数。"""
    total = len(merged)
    items = []
    for m in merged[:RECOMMENDED_LIMIT]:
        items.append({
            "id": m["id"],
            "sourceType": m["sourceType"],
            "title": m["title"],
            "snippet": m["snippet"][:200],
            "reason": m["reason"],
            "reasons": m["reasons"],
            "scoreBand": _score_band(m["score"]),
        })
    note = ""
    if total == 0:
        note = "暂无达到阈值的关联"
    elif total < RECOMMENDED_LIMIT:
        note = f"仅 {total} 项达到阈值，其余关联相关度不足"
    return {
        "items": items,
        "recommendedLimit": RECOMMENDED_LIMIT,
        "total": total,
        "note": note,
    }


@router.get("/materials/{material_id}/related")
def material_related(material_id: str, request: Request = None):
    """返回资料的可信 Top-3 关联（原材料 + 知识卡片统一列表）。

    阶段 2：原材料不在当前设备作用域视为不存在（404），召回仅在 scope 内进行。
    """
    device_scope = _device_scope(request)
    sp = ingestion.source_path_of(material_id, device_scope=device_scope)
    if not sp:
        raise HTTPException(404, "资料不存在")
    tags = ingestion.material_tags(material_id, device_scope=device_scope)
    embedding = get_source_embedding(sp)
    detail = ingestion.detail_of(material_id, device_scope=device_scope) or {}
    text = ingestion.summary_text_of(detail) if detail else ""

    sim_materials = _similar_materials(embedding or [], material_id, device_scope=device_scope)
    shared_materials = _shared_tag_materials(tags, material_id, device_scope=device_scope)
    keyword_materials = _keyword_materials(text or str(detail.get("fileName") or ""), material_id, device_scope=device_scope)

    sim_knowledge = _similar_knowledge(text, "", device_scope=device_scope)
    shared_knowledge = _shared_tag_knowledge(tags, "", device_scope=device_scope)
    keyword_knowledge = _keyword_knowledge(text, "", device_scope=device_scope)

    merged = _merge_related([
        sim_materials, shared_materials, keyword_materials,
        sim_knowledge, shared_knowledge, keyword_knowledge,
    ])
    return _recommend_response(merged)


@router.get("/knowledge/{knowledge_id}/related")
def knowledge_related(knowledge_id: str, request: Request = None):
    """返回知识卡片的可信 Top-3 关联（原材料 + 知识卡片统一列表）。

    阶段 2：目标卡片不在当前设备作用域视为不存在（404），召回仅在 scope 内进行。
    """
    device_scope = _device_scope(request)
    if device_scope != "global":
        if not card_ledger_store.get(knowledge_id, device_scope=device_scope):
            raise HTTPException(404, "资料不存在")
    page = knowledge._find(knowledge_id)
    public = knowledge._public(page)
    tags = public.get("tags", [])
    # Use card body text (strip frontmatter) for similarity
    content = str(page.get("content") or "")
    try:
        _, body = knowledge.wiki_store._parse_frontmatter(content)
        text = body.strip()[:500]
    except Exception:
        text = content[:500]

    embedding = embed_query(text) if text.strip() else []
    sim_materials = _similar_materials(embedding or [], "", device_scope=device_scope)
    shared_materials = _shared_tag_materials(tags, "", device_scope=device_scope)
    keyword_materials = _keyword_materials(text, "", device_scope=device_scope)

    sim_knowledge = _similar_knowledge(text, knowledge_id, device_scope=device_scope)
    shared_knowledge = _shared_tag_knowledge(tags, knowledge_id, device_scope=device_scope)
    keyword_knowledge = _keyword_knowledge(text, knowledge_id, device_scope=device_scope)

    merged = _merge_related([
        sim_materials, shared_materials, keyword_materials,
        sim_knowledge, shared_knowledge, keyword_knowledge,
    ])
    return _recommend_response(merged)
