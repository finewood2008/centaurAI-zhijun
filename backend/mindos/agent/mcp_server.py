"""外部 Agent 只读 MCP 服务器（AG-04）。

MCP 工具是 Agent Service 的薄适配：直接调用 agent 服务层（search_service /
evidence / detail_service / answer_service / service.capabilities），不通过 HTTP
回调 REST 接口，也不导入前端模块。与 REST 使用相同的 AgentPrincipal、scope 与
资源策略；每个调用生成独立 traceId 并写入与 REST 等价的审计记录。

工具输出为结构化 JSON，与对应 REST 业务契约一致；凭证从环境变量读取，不作为
普通工具参数传入/回显。另提供标准化只读 Resources（mindos://materials/{id}、
mindos://knowledge/{id}），仅为读取别名，不替代权限检查、不提供目录枚举/文件路径。

安全约束：
- 凭证来自环境变量 MINDOS_AGENT_MCP_TOKEN（启动前配置）；缺失或无效时工具调用
  返回明确错误，不泄露任何内部信息；
- 工具描述明确「只能读取授权的 MindOS 内容」；
- 工具结果不含本地路径、token、模型密钥或 GBrain 错误；
- 工具不可通过任意参数调用写接口（导入/更新/删除）、原始 SQL、向量库或文件下载；
- stdio transport 的 stdout 只输出 MCP 消息，日志写 stderr。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError, WithJsonSchema

from . import audit, config as agent_config, service as agent_service
from . import store as agent_store
from .auth import AgentPrincipal
from .errors import AgentError
from .schemas import AnswerOptions, AnswerRequest, EvidenceResolveRequest, SearchRequest

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# 供审计使用的动作名（与 REST 语义对应，避免直接暴露客户端/内部细节）
_ACTION_CAPABILITIES = "mcp_capabilities"
_ACTION_SEARCH = "mcp_search"
_ACTION_EVIDENCE = "mcp_get_evidence"
_ACTION_MATERIAL = "mcp_get_material"
_ACTION_KNOWLEDGE = "mcp_get_knowledge"
_ACTION_ANSWER = "mcp_answer"
_ACTION_CONTEXT_PACK = "mcp_context_pack"


def _resolve_principal() -> AgentPrincipal | None:
    """从 MINDOS_AGENT_MCP_TOKEN 解析 AgentPrincipal；缺失/无效返回 None。"""
    token = os.getenv("MINDOS_AGENT_MCP_TOKEN", "").strip()
    if not token or not agent_config.gateway_enabled():
        return None
    record = agent_store.instance().authenticate(token)
    if record is None:
        return None
    return AgentPrincipal(
        client_id=record["client_id"],
        name=record["name"],
        scopes=frozenset(record["scopes"]),
        workspace_id=record["workspace"] or agent_config.WORKSPACE_ID,
    )


class _Gateway:
    """MCP 工具门面：鉴权 + scope 校验 + 审计 + 服务调用（薄适配）。"""

    def __init__(self, principal: AgentPrincipal | None = None):
        self._principal = principal or _resolve_principal()

    def _audit(self, trace_id: str, action: str, scope: str, *, resource_type="", resource_id="", ok=True, status=200) -> None:
        try:
            audit.record(
                trace_id=trace_id,
                client_id=(self._principal.client_id if self._principal is not None else ""),
                action=action,
                scope=scope,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome="ok" if ok else "error",
                status_code=status,
                request_digest=audit.stable_digest("mcp", action),
                response_digest=audit.stable_digest(action),
                latency_ms=0,
            )
        except Exception:  # noqa: BLE001 - 审计失败不能掩盖工具结果
            logger.exception("Agent MCP 审计写入失败（忽略）")

    def _run(self, action: str, scopes: tuple[str, ...], fn, *, resource_type="", resource_id=""):
        trace_id = audit.new_trace_id()
        scope = ",".join(scopes)
        # 鉴权/授权失败也要写入与 REST 等价的拒绝审计（凭证缺失→401，scope 不足→403）。
        if self._principal is None:
            self._audit(
                trace_id, action, scope,
                resource_type=resource_type, resource_id=resource_id,
                ok=False, status=401,
            )
            raise ToolError(
                f"{trace_id} AUTHENTICATION_REQUIRED: Agent Gateway MCP 未配置有效凭证"
            )
        missing = next((s for s in scopes if s not in self._principal.scopes), None)
        if missing is not None:
            self._audit(
                trace_id, action, scope,
                resource_type=resource_type, resource_id=resource_id,
                ok=False, status=403,
            )
            raise ToolError(f"{trace_id} SCOPE_DENIED: 当前凭证不具有 {missing} 权限")
        started = time.monotonic()
        try:
            result = fn()
            self._audit(trace_id, action, scope, resource_type=resource_type, resource_id=resource_id)
            # MCP 结果顶层携带 traceId（与 REST 信封语义一致），便于外部 Agent
            # 将本次调用与审计日志关联。
            if isinstance(result, dict):
                result = dict(result)
                result["traceId"] = trace_id
            return result
        except AgentError as exc:
            self._audit(
                trace_id, action, scope,
                resource_type=resource_type, resource_id=resource_id,
                ok=False, status=exc.status_code,
            )
            # 错误文本携带 traceId + 稳定错误码 + 可读消息（与 REST 错误信封语义一致）；
            # 绝不泄露内部路径/异常堆栈。
            raise ToolError(f"{trace_id} {exc.code}: {exc.message}") from exc
        except ValidationError as exc:  # 输入校验（构造请求模型）失败 → 400/VALIDATION_ERROR
            self._audit(
                trace_id, action, scope,
                resource_type=resource_type, resource_id=resource_id,
                ok=False, status=400,
            )
            raise ToolError(f"{trace_id} VALIDATION_ERROR: 请求参数非法") from exc
        except Exception as exc:  # noqa: BLE001
            self._audit(
                trace_id, action, scope,
                resource_type=resource_type, resource_id=resource_id,
                ok=False, status=500,
            )
            logger.exception("Agent MCP 工具异常 action=%s", action)
            raise ToolError(f"{trace_id} INTERNAL_ERROR: 服务内部错误") from exc

    # ---- 工具实现 ---------------------------------------------------

    def mindos_capabilities(self) -> dict[str, Any]:
        """读取服务能力声明（与 REST GET /v1/agent/capabilities 一致）。"""
        return self._run(
            _ACTION_CAPABILITIES,
            ("mindos.read",),
            lambda: agent_service.capabilities(self._principal),
        )

    def mindos_search(
        self,
        query: Any,
        types: Any = None,
        limit: Any = 10,
        source_ids: Any = None,
    ) -> dict[str, Any]:
        """搜索 MindOS 知识卡片与原材料，返回摘要、ID 与证据句柄。

        参数类型/范围/数量校验经请求模型在 _run 内统一执行（FastMCP 不做前置
        基础类型校验），失败返回带 traceId 的 VALIDATION_ERROR 并写入审计。
        """
        return self._run(
            _ACTION_SEARCH,
            ("mindos.search", "mindos.read"),
            lambda: agent_service.search(
                self._principal,
                SearchRequest(
                    query=query,
                    types=types,
                    limit=limit,
                    sourceIds=[] if source_ids is None else source_ids,
                ),
            ),
        )

    def mindos_get_evidence(
        self,
        evidence_refs: Any,
        max_chars_per_item: Any = 3000,
        include_locator: Any = True,
    ) -> dict[str, Any]:
        """展开搜索命中的有限证据（含真实定位），只读已索引内容。"""
        return self._run(
            _ACTION_EVIDENCE,
            ("mindos.read",),
            lambda: agent_service.resolve_evidence(
                self._principal,
                EvidenceResolveRequest(
                    evidenceRefs=evidence_refs,
                    maxCharsPerItem=max_chars_per_item,
                    includeLocator=include_locator,
                ),
            ),
        )

    def mindos_get_material(
        self,
        material_id: Any,
    ) -> dict[str, Any]:
        """按 ID 读取 MindOS 原材料详情（状态、版本、结构化 parts、转写）。"""
        return self._run(
            _ACTION_MATERIAL,
            ("mindos.read",),
            lambda: agent_service.material_detail(
                self._principal, _require_nonempty_str(material_id, "materialId")
            ),
            resource_type="material",
            resource_id=str(material_id),
        )

    def mindos_get_knowledge(
        self,
        knowledge_id: Any,
    ) -> dict[str, Any]:
        """按 ID 读取 MindOS 知识卡片详情（正文、标签、来源、证据可用标记）。"""
        return self._run(
            _ACTION_KNOWLEDGE,
            ("mindos.read",),
            lambda: agent_service.knowledge_detail(
                self._principal, _require_nonempty_str(knowledge_id, "knowledgeId")
            ),
            resource_type="knowledge",
            resource_id=str(knowledge_id),
        )

    def mindos_answer(
        self,
        question: Any,
        source_ids: Any = None,
    ) -> dict[str, Any]:
        """带引用的 MindOS 问答：返回答案与带 evidenceRef 的引用。只读。"""
        return self._run(
            _ACTION_ANSWER,
            ("mindos.answer", "mindos.read"),
            lambda: agent_service.answer(
                self._principal,
                AnswerRequest(
                    question=question,
                    options=AnswerOptions(
                        sourceIds=[] if source_ids is None else source_ids
                    ),
                ),
            ),
        )


_GATEWAY: _Gateway | None = None


def _gateway() -> _Gateway:
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = _Gateway()
    return _GATEWAY


    def mindos_context_pack(self, purpose: Any, sections: Any = None, max_claims: Any = 50) -> dict[str, Any]:
        """知君 P4：只读个人上下文包（用户已确认且允许导出的理解），用途绑定、写回执。"""
        def _call():
            if not isinstance(purpose, str) or len(purpose.strip()) < 2:
                raise AgentError(400, "VALIDATION_ERROR", "purpose 必须说明用途（≥ 2 字）")
            if sections is not None and not isinstance(sections, list):
                raise AgentError(400, "VALIDATION_ERROR", "sections 必须是列表")
            try:
                limit = int(max_claims)
            except (TypeError, ValueError):
                raise AgentError(400, "VALIDATION_ERROR", "max_claims 必须是整数") from None
            if not 1 <= limit <= 200:
                raise AgentError(400, "VALIDATION_ERROR", "max_claims 需在 1–200 之间")
            return agent_service.context_pack(self._principal, purpose, sections, limit)

        return self._run(_ACTION_CONTEXT_PACK, ("zhijun.profile",), _call, resource_type="context_pack")


# ---- 模块级工具（FastMCP 注册用） -------------------------------------

def _require_nonempty_str(value: Any, name: str) -> str:
    """在 _run 链路内校验参数为非空字符串（FastMCP 不做前置类型校验）。"""
    if not isinstance(value, str) or not value.strip():
        raise AgentError(400, "VALIDATION_ERROR", f"{name} 必须为非空字符串")
    return value


def mindos_capabilities() -> dict[str, Any]:
    return _gateway().mindos_capabilities()


def mindos_search(
    query: Annotated[Any, WithJsonSchema({"type": "string", "minLength": 2, "maxLength": 500})],
    types: Annotated[Any, WithJsonSchema({
        "type": "array",
        "items": {"type": "string", "enum": ["knowledge", "material"]},
        "maxItems": 2,
    })] = None,
    limit: Annotated[Any, WithJsonSchema({"type": "integer", "minimum": 1, "maximum": 20})] = 10,
    source_ids: Annotated[Any, WithJsonSchema({
        "type": "array", "items": {"type": "string"}, "maxItems": 20,
    })] = None,
) -> dict[str, Any]:
    return _gateway().mindos_search(query, types=types, limit=limit, source_ids=source_ids)


def mindos_get_evidence(
    evidence_refs: Annotated[Any, WithJsonSchema({
        "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10,
    })],
    max_chars_per_item: Annotated[Any, WithJsonSchema({
        "type": "integer", "minimum": 1, "maximum": 3000,
    })] = 3000,
    include_locator: Annotated[Any, WithJsonSchema({"type": "boolean"})] = True,
) -> dict[str, Any]:
    return _gateway().mindos_get_evidence(
        evidence_refs, max_chars_per_item=max_chars_per_item, include_locator=include_locator
    )


def mindos_get_material(
    material_id: Annotated[Any, WithJsonSchema({"type": "string", "minLength": 1})],
) -> dict[str, Any]:
    return _gateway().mindos_get_material(material_id)


def mindos_get_knowledge(
    knowledge_id: Annotated[Any, WithJsonSchema({"type": "string", "minLength": 1})],
) -> dict[str, Any]:
    return _gateway().mindos_get_knowledge(knowledge_id)


def mindos_answer(
    question: Annotated[Any, WithJsonSchema({"type": "string", "minLength": 2, "maxLength": 500})],
    source_ids: Annotated[Any, WithJsonSchema({
        "type": "array", "items": {"type": "string"}, "maxItems": 20,
    })] = None,
) -> dict[str, Any]:
    return _gateway().mindos_answer(question, source_ids=source_ids)


def mindos_context_pack(
    purpose: Annotated[Any, WithJsonSchema({"type": "string", "minLength": 2, "maxLength": 200})],
    sections: Annotated[Any, WithJsonSchema({
        "type": "array",
        "items": {"type": "string", "enum": ["who", "people", "matters", "principles", "ways", "direction"]},
        "maxItems": 6,
    })] = None,
    max_claims: Annotated[Any, WithJsonSchema({"type": "integer", "minimum": 1, "maximum": 200})] = 50,
) -> dict[str, Any]:
    return _gateway().mindos_context_pack(purpose, sections=sections, max_claims=max_claims)


AGENT_TOOLS = [
    (
        mindos_context_pack,
        "知君个人上下文包（只读）：返回用户已确认、且允许带走的自我理解（我是谁 / 我的人 / 我的事 / "
        "我的原则 / 我的做法 / 我的方向），必须说明用途；不含未确认印象、敏感内容与证据原文。每次调用留回执。",
    ),
    (
        mindos_capabilities,
        "读取 MindOS Agent 服务能力声明（已启用工具、写入模式、内容上限）。只读，"
        "仅返回你有权限访问的 MindOS 内容。",
    ),
    (
        mindos_search,
        "只读搜索 MindOS 知识卡片与原材料，返回卡片/材料摘要、ID 与 evidenceRef 句柄。"
        "只能读取已授权的 MindOS 内容；不返回任何本地路径。",
    ),
    (
        mindos_get_evidence,
        "展开搜索命中的有限证据：只读取索引中已有内容并返回真实定位（表格/音频/图片）。"
        "只读、不重新解析文档。",
    ),
    (
        mindos_get_material,
        "按 materialId 读取 MindOS 原材料详情（状态、版本、摘要、标签、结构化 parts、转写）。只读。",
    ),
    (
        mindos_get_knowledge,
        "按 knowledgeId 读取 MindOS 知识卡片详情（正文、标签、来源、证据可用标记）。只读。",
    ),
    (
        mindos_answer,
        "带引用的 MindOS 问答：返回答案与带 evidenceRef 的引用（可经 get_evidence "
        "展开复核）。回答含引用；资料不足时返回 INSUFFICIENT_EVIDENCE 并明确标识，"
        "绝不虚构证据外信息。只读。",
    ),
]


def create_agent_mcp_server(*, principal: AgentPrincipal | None = None, **kwargs: Any) -> FastMCP:
    """创建 Agent 只读 MCP 服务器。

    未显式传入 principal 时使用 MINDOS_AGENT_MCP_TOKEN 解析；凭证缺失/无效时
    工具调用返回明确错误（服务器可启动但不泄露任何内部信息）。
    """
    global _GATEWAY
    _GATEWAY = _Gateway(principal=principal)
    server = FastMCP(
        "mindos-agent-gateway",
        instructions=(
            "Use these read-only tools to search MindOS knowledge cards and raw materials, "
            "expand bounded evidence with real locators, and read material/card details. "
            "Only authorized MindOS content is accessible; no local paths are exposed. "
            "Each tool result carries a top-level traceId; rejected/failed calls include "
            "the same traceId plus a stable error code in the error text. All traceIds "
            "match the server audit record (queryable via the loopback admin API)."
        ),
        **kwargs,
    )
    for function, description in AGENT_TOOLS:
        server.add_tool(
            function,
            description=description,
            annotations=READ_ONLY,
            structured_output=True,
        )

    # 标准化只读 Resources（读取别名）：不替代权限检查，不提供目录枚举/文件路径/无限正文。
    @server.resource(
        "mindos://materials/{materialId}",
        name="mindos-material",
        title="MindOS 材料详情（只读）",
        description="按 materialId 读取 MindOS 原材料详情；仅返回你有权限访问的内容，不含本地路径。",
        mime_type="application/json",
    )
    def _material_resource(materialId: str) -> str:
        gateway = _gateway()
        data = gateway._run(
            _ACTION_MATERIAL,
            ("mindos.read",),
            lambda: agent_service.material_detail(gateway._principal, materialId),
            resource_type="material",
            resource_id=materialId,
        )
        return json.dumps(data, ensure_ascii=False)

    @server.resource(
        "mindos://knowledge/{knowledgeId}",
        name="mindos-knowledge",
        title="MindOS 知识卡片详情（只读）",
        description="按 knowledgeId 读取 MindOS 知识卡片详情；仅返回你有权限访问的内容。",
        mime_type="application/json",
    )
    def _knowledge_resource(knowledgeId: str) -> str:
        gateway = _gateway()
        data = gateway._run(
            _ACTION_KNOWLEDGE,
            ("mindos.read",),
            lambda: agent_service.knowledge_detail(gateway._principal, knowledgeId),
            resource_type="knowledge",
            resource_id=knowledgeId,
        )
        return json.dumps(data, ensure_ascii=False)

    return server


def main() -> None:
    """stdio 入口：stdout 只输出 MCP 协议消息，日志写 stderr。"""
    create_agent_mcp_server().run("stdio")


def reset_for_tests() -> None:
    """测试用：清空网关单例。"""
    global _GATEWAY
    _GATEWAY = None


if __name__ == "__main__":
    # stdio 入口：`python -m mindos.agent.mcp_server`（stdout 只输出 MCP 协议消息）。
    main()
