"""MindOS unified search: cards first, then current MindOS raw materials only.

P14-08：独立返回 visualMaterials（Chinese-CLIP 以文搜图，复用入库时建立的图片视觉
索引）。文本 BGE 分与 CLIP 分属不同向量空间，不得相加或统一排序——按命中依据
分开分组返回，由前端分别展示。
"""
import logging

from fastapi import APIRouter, HTTPException, Query, Request

import lexical
from config import CLIP_ENABLED, IMAGE_SIM_THRESHOLD
from embedder import embed_query, embed_query_clip, clip_available
from vector_store import (
    search as vector_search,
    get_chunks_by_ids,
    get_source_chunks,
    search_images,
)

from . import knowledge
from .services import ingestion
from .services import search_service

router = APIRouter(prefix="/api/mindos/search", tags=["mindos-search"])
logger = logging.getLogger(__name__)


def _device_scope(request: Request) -> str:
    """票据模式下按真实 device_id 生成业务数据作用域；调试模式为 global。"""
    from .device_context import scope_for_device

    context = getattr(request.state, "mindos_device_context", None)
    return scope_for_device(getattr(context, "device_id", None))


def _material_results(query: str, limit: int, device_scope: str = "global") -> list[dict]:
    """Web 搜索原材料口径（每材料一条的最高命中快照）。

    检索主体由 mindos/services/search_service.py::search_material_results
    单点实现（与 QA/Agent 共用同一批检索原语和生命周期过滤）；此处按调用时
    取值传入本模块依赖，保证既有测试的 patch("mindos.search.vector_search")
    等保持生效。视觉检索（_visual_material_results）仍在本模块。

    阶段 2：device_scope 由请求票据身份决定，跨设备/账号材料不进入召回。
    """
    return search_service.search_material_results(
        query,
        limit,
        device_scope=device_scope,
        embed_query_callable=embed_query,
        vector_search_callable=vector_search,
        lexical_search_callable=lexical.search,
        get_chunks_by_ids_callable=get_chunks_by_ids,
        material_for_source_callable=lambda sp: ingestion.material_for_source(sp, device_scope=device_scope),
        job_store_instance_callable=ingestion.JobStore.instance,
        status_of_callable=lambda mid: ingestion.status_of(mid, device_scope=device_scope),
    )


def _unavailable_material_results(query: str, limit: int, device_scope: str = "global") -> list[dict]:
    return search_service.search_unavailable_material_results(
        query, limit, device_scope=device_scope,
        job_store_instance_callable=ingestion.JobStore.instance,
        status_of_callable=lambda mid: ingestion.status_of(mid, device_scope=device_scope),
    )


def _text_snippet_of(source_path: str) -> str:
    """取图片在文本集合中的首块文本（VLM 描述 / OCR / 用户说明）作为命中依据片段。

    纯图无任何文字时返回空串——前端据此如实展示「纯图无文字」，绝不把文件名
    伪充当命中依据（Review：空白纯图不能仅凭文件名被宣称为视觉语义命中）。
    """
    from .stage_d_admin import legacy_read_enabled
    if not legacy_read_enabled():
        return ""
    try:
        chunks = get_source_chunks(source_path, limit=1)
    except Exception:
        return ""
    if not chunks:
        return ""
    return str(chunks[0].get("text") or "").strip()[:200]


def _visual_material_results(query: str, limit: int, device_scope: str = "global") -> tuple[list[dict], bool]:
    """Chinese-CLIP 以文搜图（视觉命中，独立于文本检索分组）。

    复用图片入库时已有的图片视觉索引（embed_image_clip → add_image_vector），
    不新建第三套索引。返回 (visualMaterials, 视觉检索是否可用)：

    - CLIP 未启用 / 加载失败 / 查询嵌入失败 → 返回 ([], False)。这是显式降级，
      前端展示「视觉检索暂不可用」；绝不把失败吞掉后伪称「未命中」。
    - 仅保留能经 ingestion.material_for_source() 映射、未归档且类型为 image 的
      MindOS 材料；同一材料按最高视觉分去重；低于阈值（IMAGE_SIM_THRESHOLD）的
      弱信号不算视觉语义命中。
    - 图片集合复用了旧项目集合，可能混有旧项目图片 / 视频帧 / 已归档材料：若
      前若干条都不是有效 MindOS 图片，则分批扩大 n_results 继续召回，直到收齐
      limit 条合格结果或索引已遍历完成/到达召回上限——避免有效图片排位靠后被
      漏掉（Review P14-08 P1）。
    - 文本吞吐：不返回 source path / 模型内部异常 / 原始视觉 embedding。
    """
    from .stage_d_admin import legacy_read_enabled
    if not legacy_read_enabled():
        return [], False
    if not (CLIP_ENABLED and clip_available()):
        return [], False
    try:
        qv = embed_query_clip(query)
        if not qv:
            return [], False
    except Exception:
        logger.exception("CLIP 视觉检索异常，按不可用降级")
        return [], False

    recycled = ingestion.recycled_material_ids(device_scope=device_scope)
    availability: dict[str, bool] = {}
    best: dict[str, dict] = {}
    # source → material_for_source 结果缓存（None 也缓存），分批扩大召回时同一
    # source 会重复出现，用它避免反复查询；同时不跳过同 source 的更高分更新。
    memo: dict[str, dict | None] = {}
    batch = max(limit, 40)
    cap = max(limit * 40, 2000)
    try:
        while len(best) < limit and batch <= cap:
            imgs = search_images(qv, n_results=batch)
            for im in imgs:
                source = str(im.get("source_path") or "")
                if source not in memo:
                    memo[source] = ingestion.material_for_source(source, device_scope=device_scope)
                record = memo[source]
                if record is None:
                    continue
                material_id = record["material_id"]
                if material_id in recycled:
                    continue
                if material_id not in availability:
                    try:
                        public = ingestion.status_of(material_id, device_scope=device_scope)
                        availability[material_id] = bool(public and public.get("status") == "available")
                    except Exception:
                        availability[material_id] = False
                if not availability[material_id]:
                    continue
                if record.get("file_type") != "image":
                    continue
                shot = float(im.get("vector_score") or 0.0)
                # 低分不计入视觉语义命中（避免空白纯图靠文件名等弱信号被宣称命中）
                if shot < IMAGE_SIM_THRESHOLD:
                    continue
                if material_id not in best or shot > best[material_id]["score"]:
                    best[material_id] = {
                        "materialId": material_id,
                        "title": record["file_name"],
                        "fileType": "image",
                        "snippet": _text_snippet_of(source),
                        "score": round(shot, 4),
                        # 命中依据：当前阶段视觉命中全部来自 CLIP 图文语义匹配；
                        # ocr/caption 为依据类别（后续阶段扩展），此处绝不把 OCR
                        # 命中误标为视觉。
                        "matchMode": "visual",
                        "previewUrl": f"/api/mindos/materials/{material_id}/file",
                    }
                if len(best) >= limit:
                    break
            if len(imgs) < batch:
                break  # 索引已遍历完成，无更多可召回
            if batch >= cap:
                break  # 已到达召回上限，避免无限扩大
            batch = min(batch * 4, cap)
    except Exception:
        logger.exception("CLIP 视觉检索异常，按不可用降级")
        return [], False

    items = sorted(best.values(), key=lambda item: item["score"], reverse=True)[:limit]
    return items, True


@router.get("")
def unified_search(
    request: Request,
    q: str = Query(min_length=1, max_length=300),
    limit: int = Query(default=12, ge=1, le=30),
):
    query = q.strip()
    if not query:
        raise HTTPException(400, "查询内容为空")
    device_scope = _device_scope(request)
    cards = knowledge.search_cards(query, limit=limit, device_scope=device_scope)
    materials = _material_results(query, limit, device_scope=device_scope)
    unavailable_materials = _unavailable_material_results(query, limit, device_scope=device_scope)
    visual_materials, visual_ok = _visual_material_results(query, limit, device_scope=device_scope)
    return {
        "query": query,
        "knowledge": cards,
        "materials": materials,
        "unavailableMaterials": unavailable_materials,
        "visualMaterials": visual_materials,
        "capabilities": {"visualSearch": visual_ok},
        # total 保持文本口径（知识成品 + 原材料）；视觉命中的 CLIP 分与 BGE 分
        # 不同空间，不加进同一计数，避免把两类命中混为一谈。
        "total": len(cards) + len(materials),
        "unavailableTotal": len(unavailable_materials),
    }
