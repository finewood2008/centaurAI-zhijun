"""Agent 统一服务适配层（AG-01 能力声明；AG-04 起 REST 与 MCP 共用的业务门面）。

capabilities 之外，AG-02/03/04 将搜索、证据展开、材料/知识详情、问答统一封装
到本模块：REST Router 与 MCP Server 都只调用本适配层调用领域服务，参数校验、
错误映射、响应投影只在此维护，避免 REST / MCP 两套实现产生差异。
"""
from __future__ import annotations

from . import config as agent_config
from . import detail_service
from . import evidence
from . import answer_service as agent_answer_service
from . import search_service as agent_search_service
from .auth import AgentPrincipal
from .schemas import AnswerRequest, EvidenceResolveRequest, SearchRequest

# 工具注册表：name 对外公开；scope 为调用所需权限；enabled 表示本服务端是否已实现。
# search/getEvidence/getMaterial/getKnowledge 在 AG-02-02/03/04 落地，answer 在
# AG-03 落地，均已 enabled。
_TOOL_REGISTRY = [
    {"name": "search", "scope": "mindos.search", "enabled": True},          # AG-02-02
    {"name": "getEvidence", "scope": "mindos.read", "enabled": True},       # AG-02-03
    {"name": "getMaterial", "scope": "mindos.read", "enabled": True},       # AG-02-04
    {"name": "getKnowledge", "scope": "mindos.read", "enabled": True},      # AG-02-04
    {"name": "answer", "scope": "mindos.answer", "enabled": True},          # AG-03
    {"name": "context_pack", "scope": "zhijun.profile", "enabled": True},   # 知君 P4：只读个人上下文包
]


def context_pack(principal, purpose: str, sections: list[str] | None, max_claims: int) -> dict:
    """知君 P4：只读个人上下文包（confirmed ∧ export_allowed ∧ 非敏感），用途绑定。"""
    from ..stores.ontology_store import OntologyError
    from ..zhijun import context_pack as pack_module
    from .errors import AgentError

    try:
        return pack_module.build_pack(purpose=purpose, sections=sections, max_claims=max_claims, consumer=principal.client_id)
    except OntologyError as exc:
        raise AgentError(400, "VALIDATION_ERROR", str(exc)) from None


def enabled_tools(scopes: frozenset) -> list[str]:
    """按 scopes 计算当前对调用方可见且服务端已实现的工具列表。"""
    return [t["name"] for t in _TOOL_REGISTRY if t["enabled"] and t["scope"] in scopes]


def write_modes(scopes: frozenset) -> dict:
    """写能力声明。V1 第一阶段仅开放读取，写入均保持关闭（AG-05/06 开启）。"""
    _ = scopes
    return {
        "import": False,
        "knowledgeDraft": False,
        "knowledgeCommit": False,
    }


def supported_file_types() -> list[str]:
    """基于服务端实际支持的扩展名集合派生，不返回物理路径。"""
    from config import SUPPORTED_EXTENSIONS

    return sorted(ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS)


def limits_payload() -> dict:
    return {
        "searchPageSizeMax": agent_config.SEARCH_PAGE_SIZE_MAX,
        "evidenceCharsMax": agent_config.EVIDENCE_CHARS_MAX,
        "answerQuestionCharsMax": agent_config.ANSWER_QUESTION_CHARS_MAX,
    }


def capabilities(principal: AgentPrincipal) -> dict:
    return {
        "apiVersion": "v1",
        "workspaceId": principal.workspace_id,
        "tools": enabled_tools(principal.scopes),
        "writeModes": write_modes(principal.scopes),
        "limits": limits_payload(),
        "supportedFileTypes": supported_file_types(),
    }


# ---- AG-02/03/04：REST 与 MCP 共用的业务门面（统一适配层）-----------------

def search(principal: AgentPrincipal, req: SearchRequest) -> dict:
    """统一搜索（AG-02-02）。参数校验/错误映射在 agent/search_service.py 内。"""
    return agent_search_service.search(req, principal)


def resolve_evidence(principal: AgentPrincipal, req: EvidenceResolveRequest) -> dict:
    """证据展开（AG-02-03）。"""
    return evidence.resolve_evidence_batch(
        principal.client_id,
        req.evidenceRefs,
        max_chars_per_item=req.maxCharsPerItem,
        include_locator=req.includeLocator,
    )


def material_detail(principal: AgentPrincipal, material_id: str) -> dict:
    """材料详情（AG-02-04）。"""
    return detail_service.material_detail(material_id)


def knowledge_detail(principal: AgentPrincipal, knowledge_id: str) -> dict:
    """知识卡片详情（AG-02-04）。"""
    return detail_service.knowledge_detail(knowledge_id)


def answer(principal: AgentPrincipal, req: AnswerRequest) -> dict:
    """带引用的问答（AG-03）。"""
    return agent_answer_service.answer(req, principal)
