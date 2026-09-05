"""Agent 材料 / 知识卡片详情门面（AG-02-04）。

只读复用既有详情能力（ingestion.detail_of / knowledge.knowledge_view）并通过
projection 投影为 Agent 安全响应：

- 生命周期（归档/回收）规则复用统一检索服务的单一维护点，Agent 层不复制；
- 归档/回收/不存在/不可见统一返回 404/RESOURCE_NOT_FOUND（不泄露对象状态）；
- 不返回 source_path / previewUrl / 全文 text / Wiki path / artifact key。
"""
from __future__ import annotations

from . import projection
from .errors import AgentError


def material_detail(material_id: str) -> dict:
    """按 ID 读取材料详情并投影为 Agent 安全响应。

    复用 ingestion.detail_of（状态、版本、summary、tags、contentParts、
    transcript 等既有派生结果），不重新解析文件或调用 LLM。
    """
    from .. import derived as derived_svc
    from ..services import ingestion, search_service
    recycled = ingestion.recycled_material_ids()
    # 生命周期规则只复用统一检索服务的单一维护点。
    if search_service.is_material_excluded(material_id, set(), recycled):
        raise AgentError(404, "RESOURCE_NOT_FOUND", "资源不存在或当前不可访问")
    detail = ingestion.detail_of(material_id)
    if detail is None:
        raise AgentError(404, "RESOURCE_NOT_FOUND", "资源不存在或当前不可访问")
    # 内容处理中 / 失败：只返回元数据、状态与摘要状态，contentParts / 图片 /
    # 转写强制为空数组，避免残留旧派生数据被误读为可用正文。
    if detail.get("status") != "available":
        detail = dict(detail)
        detail["contentParts"] = []
        detail["embeddedImages"] = []
        detail["transcript"] = []
    entities = derived_svc.entities_of(material_id)
    return projection.project_material_detail(detail, entities=entities)


def knowledge_detail(knowledge_id: str) -> dict:
    """按 ID 读取知识卡片详情并投影为 Agent 安全响应。

    复用 knowledge.knowledge_view（active 过滤 + 清理正文 + 证据可用标记），
    来源关系由卡片 frontmatter 派生，不接受客户端传入。
    """
    from .. import knowledge

    view = knowledge.knowledge_view(knowledge_id)
    if view is None:
        raise AgentError(404, "RESOURCE_NOT_FOUND", "资源不存在或当前不可访问")
    return projection.project_knowledge_detail(view)
