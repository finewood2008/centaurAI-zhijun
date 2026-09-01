"""Agent 搜索门面（AG-02-02）。

只调用统一 MindOS 检索服务（mindos/services/search_service.py），不保存第二套
向量/BM25 索引，也不在 Agent 层复制生命周期过滤规则。参数的业务校验在此执行
（业务逻辑不落入 router.py），REST 与后续 MCP 工具共用同一入口。

- query 2–500 字；limit 不超过 capabilities 声明的上限；
- types 为空时查询知识卡片与原材料；只允许 knowledge/material 两种；
- sourceIds 最多 20 个，仅缩小服务端已授权的检索范围，无法绕过可见性；
- cursor 使用服务端签发的分页游标；本阶段尚未实现分页，非空一律拒绝。
"""
from __future__ import annotations

from ..services import search_service
from . import projection
from .auth import AgentPrincipal
from .errors import AgentError
from . import config as agent_config

ALLOWED_TYPES = ("knowledge", "material")
SOURCE_IDS_MAX = 20
QUERY_CHARS_MIN = 2
QUERY_CHARS_MAX = 500


def _validate(req) -> None:
    query = str(req.query or "").strip()
    if len(query) < QUERY_CHARS_MIN or len(query) > QUERY_CHARS_MAX:
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"query 长度必须在 {QUERY_CHARS_MIN}–{QUERY_CHARS_MAX} 字之间",
        )
    if not (1 <= req.limit <= agent_config.SEARCH_PAGE_SIZE_MAX):
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"limit 必须在 1–{agent_config.SEARCH_PAGE_SIZE_MAX} 之间",
        )
    if req.types:
        for t in req.types:
            if t not in ALLOWED_TYPES:
                raise AgentError(
                    400,
                    "VALIDATION_ERROR",
                    f"非法类型 {t}，仅支持 {'/'.join(ALLOWED_TYPES)}",
                )
    if req.sourceIds and len(req.sourceIds) > SOURCE_IDS_MAX:
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"sourceIds 最多 {SOURCE_IDS_MAX} 个",
        )
    # 分页尚未实现：不接受任意游标，避免调用方自造 offset/path 绕过授权。
    if req.cursor:
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            "cursor 分页能力尚未开放，请移除 cursor 参数",
        )


def search(req, principal: AgentPrincipal) -> dict:
    """执行统一搜索并投影为 Agent 安全响应。

    检索主体复用 mindos/services/search_service.py::search_unified（向量 + BM25 +
    知识卡片正文向量 + 生命周期过滤），此处只做参数校验、范围限定与响应投影。
    """
    _validate(req)
    include_snippet = bool(req.include.snippet) if req.include else True
    include_locator = bool(req.include.locator) if req.include else True
    internal = search_service.UnifiedSearchRequest(
        query=str(req.query or "").strip(),
        limit=req.limit,
        types=tuple(req.types) if req.types else None,
        source_ids=tuple(req.sourceIds) if req.sourceIds else None,
        include_snippet=include_snippet,
    )
    result = search_service.search_unified(internal)
    items = [
        projection.project_search_item(
            hit,
            client_id=principal.client_id,
            include_locator=include_locator,
        )
        for hit in result.get("items", [])
    ]
    return {
        "query": str(req.query or "").strip(),
        "items": items,
        "nextCursor": None,
        "total": int(result.get("total") or len(items)),
    }
