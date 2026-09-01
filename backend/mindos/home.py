"""MindOS 首页聚合（P12）。

向首页提供四类概览数据，全部基于现有存储只读聚合：
- 最近导入资料（已过滤回收站材料，按 createdAt 倒序）。
- 最近编辑知识卡片（按 updatedAt 倒序，已过滤归档/已合并卡片）。
- 失败任务数量。
- 待处理治理候选数量。
"""
from fastapi import APIRouter

from . import knowledge
from .services import ingestion
from .stores import governance_store

router = APIRouter(prefix="/api/mindos", tags=["mindos-home"])

_RECENT_LIMIT = 5


@router.get("/home")
def home_overview():
    # 服务层 list_materials 不做回收过滤，此处显式排除回收站材料。
    recycled = ingestion.recycled_material_ids()

    # 最近导入：已排除回收材料，按 createdAt 倒序取前 N。
    recent_materials = [
        item for item in ingestion.list_materials()
        if item["materialId"] not in recycled
    ][: _RECENT_LIMIT]

    # 最近编辑：knowledge_list 已过滤归档/已合并/已回收卡片，按 updatedAt 倒序取前 N。
    cards = knowledge.knowledge_list(limit=500).get("items", [])
    cards.sort(key=lambda c: c.get("updatedAt") or "", reverse=True)
    recent_knowledge = cards[: _RECENT_LIMIT]

    failed_count = sum(
        1 for item in ingestion.list_materials(status="failed")
        if item["materialId"] not in recycled
    )
    pending_governance = len(governance_store.instance().list(status="pending", limit=1000))

    return {
        "recentMaterials": recent_materials,
        "recentKnowledge": recent_knowledge,
        "failedCount": failed_count,
        "pendingGovernance": pending_governance,
    }
