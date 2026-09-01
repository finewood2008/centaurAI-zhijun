"""外部 Agent REST 网关路由（AG-01：认证 + 能力声明）。

- /v1/agent 是唯一外部 REST 入口，使用 Bearer 服务凭证，不依赖 loopback。
- 所有响应返回 traceId（沿用/生成 X-Request-Id）；错误走统一契约。
- FastAPI 0.141 的 include_router 不会传播子路由的 exception handler 与
  middleware，因此本模块提供 install(app) 由 server 显式注册。
- 非 Agent 路径的校验/未处理异常一律委托 FastAPI 默认链路，不影响既有 API。
"""
from __future__ import annotations

import logging
import time
import traceback

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from . import audit, config as agent_config, evidence, rate_limit, service
from .auth import AgentPrincipal, require_scope, require_scopes
from .errors import AgentError, error_payload, ok_payload
from .schemas import AnswerRequest, EvidenceResolveRequest, SearchRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agent", tags=["agent"])


def _is_agent_path(path: str) -> bool:
    return path.startswith("/v1/agent") or path.startswith("/api/agent")


# ---- traceId 中间件 ----

class _AgentGatewayMiddleware(BaseHTTPMiddleware):
    """为 /v1/agent 请求生成/传递 X-Request-Id，并作为响应头与审计 traceId。

    网关未启用（MINDOS_AGENT_GATEWAY_ENABLED 缺失/非 true）时，对任何
    /v1/agent/* 路径（含未知路径、任意方法）一律返回 403/POLICY_DENIED，
    不落入路由，避免泄漏路由存在性。
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/v1/agent"):
            return await call_next(request)
        trace_id = audit.normalize_trace_id(request.headers.get("x-request-id"))
        request.state.agent_trace_id = trace_id
        request.state.agent_started = time.monotonic()
        if not agent_config.gateway_enabled():
            agent_error = AgentError(403, "POLICY_DENIED", "外部 Agent Gateway 未启用")
            audit.record(
                trace_id=trace_id,
                client_id="",
                action=_action_for_path(request.url.path),
                scope="",
                resource_type="",
                resource_id="",
                outcome="error",
                status_code=403,
                request_digest=audit.stable_digest("gateway-disabled"),
                response_digest=audit.stable_digest("POLICY_DENIED"),
                latency_ms=0,
            )
            return JSONResponse(
                status_code=403,
                content=error_payload(trace_id, agent_error),
                headers={"X-Request-Id": trace_id},
            )
        response = await call_next(request)
        response.headers["X-Request-Id"] = trace_id
        return response


# ---- 统一错误处理 ----

def _action_for_path(path: str) -> str:
    # /v1/agent/{action} -> action；/v1/agent/{resource}/{id} -> resource 段。
    # （如 /v1/agent/evidence:resolve -> evidence:resolve；/v1/agent/materials/{id} -> materials）
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "agent":
        return parts[-1]
    if len(parts) >= 3 and parts[-3] == "agent":
        return parts[-2]
    return path or "unknown"


def _latency_ms(request: Request) -> int:
    started = getattr(request.state, "agent_started", None)
    if started is None:
        return 0
    return int((time.monotonic() - started) * 1000)


def _record_error(request: Request, exc: Exception, status_code: int, code: str) -> str:
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    client_id = getattr(request.state, "agent_client_id", "") or ""
    audit.record(
        trace_id=trace_id,
        client_id=client_id,
        action=_action_for_path(request.url.path),
        scope=getattr(request.state, "agent_scope", "") or "",
        resource_type="",
        resource_id="",
        outcome="error",
        status_code=status_code,
        request_digest=audit.stable_digest("error"),
        response_digest=audit.stable_digest(code),
        latency_ms=_latency_ms(request),
    )
    return trace_id


def _log_unhandled(request: Request, exc: Exception) -> None:
    """记录服务端详细异常；日志中绝不出现 Authorization 明文。"""
    raw_auth = request.headers.get("authorization", "")
    text = traceback.format_exc()
    if raw_auth:
        text = text.replace(raw_auth, "[REDACTED]")
    logger.error(
        "Agent Gateway 未处理异常 path=%s traceId=%s\n%s",
        request.url.path,
        getattr(request.state, "agent_trace_id", "?"),
        text,
    )


async def _agent_error_handler(request: Request, exc: AgentError):
    trace_id = _record_error(request, exc, exc.status_code, exc.code)
    headers = dict(exc.headers or {})
    headers["X-Request-Id"] = trace_id
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(trace_id, exc),
        headers=headers,
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError):
    # 非 Agent 路径：委托 FastAPI 默认处理器，完整保留既有 422 契约。
    if not _is_agent_path(request.url.path):
        return await request_validation_exception_handler(request, exc)
    # Agent 接口（外部 /v1/agent 与本机管理 /api/agent）：统一 400/VALIDATION_ERROR 信封。
    trace_id = _record_error(request, exc, 400, "VALIDATION_ERROR")
    agent_error = AgentError(400, "VALIDATION_ERROR", "请求字段、筛选条件或分页参数非法")
    return JSONResponse(
        status_code=400,
        content=error_payload(trace_id, agent_error),
        headers={"X-Request-Id": trace_id},
    )


async def _unhandled_error_handler(request: Request, exc: Exception):
    # 非 Agent 路径：重新抛出，走既有默认 500 链路，不改变现有行为。
    if not _is_agent_path(request.url.path):
        raise exc
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    _log_unhandled(request, exc)
    try:
        audit.record(
            trace_id=trace_id,
            client_id=getattr(request.state, "agent_client_id", "") or "",
            action=_action_for_path(request.url.path),
            scope=getattr(request.state, "agent_scope", "") or "",
            resource_type="",
            resource_id="",
            outcome="error",
            status_code=500,
            request_digest=audit.stable_digest("unhandled"),
            response_digest=audit.stable_digest("INTERNAL_ERROR"),
            latency_ms=_latency_ms(request),
        )
    except Exception:  # noqa: BLE001 - 审计失败不能掩盖原始错误
        logger.exception("Agent Gateway 审计写入失败（忽略）")
    # 对外统一 500/INTERNAL_ERROR，不泄露原始异常信息。
    agent_error = AgentError(500, "INTERNAL_ERROR", "服务内部错误", retryable=False)
    return JSONResponse(
        status_code=500,
        content=error_payload(trace_id, agent_error),
        headers={"X-Request-Id": trace_id},
    )


async def _http_error_handler(request: Request, exc: StarletteHTTPException):
    # 未知 Agent 路径（404）返回统一错误信封；其余 HTTP 异常委托 FastAPI 默认处理，
    # 保留非 Agent API 的原有契约。
    if _is_agent_path(request.url.path) and exc.status_code == 404:
        trace_id = _record_error(request, exc, 404, "RESOURCE_NOT_FOUND")
        agent_error = AgentError(404, "RESOURCE_NOT_FOUND", "Agent 接口不存在", retryable=False)
        return JSONResponse(
            status_code=404,
            content=error_payload(trace_id, agent_error),
            headers={"X-Request-Id": trace_id},
        )
    return await http_exception_handler(request, exc)


def install(app: FastAPI) -> None:
    """由 server 在 include_router 前后调用：注册统一错误处理与 traceId 中间件。"""
    app.add_middleware(_AgentGatewayMiddleware)
    app.add_exception_handler(AgentError, _agent_error_handler)
    # fastapi.HTTPException 继承自 starlette.exceptions.HTTPException，注册父类
    # 可同时覆盖两者（Starlette 路由 404 抛出的是父类实例）。
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)


# ---- 端点 ----

@router.get("/capabilities")
def agent_capabilities(
    request: Request,
    principal: AgentPrincipal = Depends(require_scope("mindos.read")),
):
    """能力声明：tools / writeModes / limits / supportedFileTypes 由服务端实际功能与 scopes 计算。"""
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    rate_limit.check_or_deny(principal.client_id, "capabilities")
    started = time.monotonic()
    data = service.capabilities(principal)
    audit.record(
        trace_id=trace_id,
        client_id=principal.client_id,
        action="capabilities",
        scope="mindos.read",
        resource_type="",
        resource_id="",
        outcome="ok",
        status_code=200,
        request_digest=audit.stable_digest("capabilities"),
        response_digest=audit.response_digest(data),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ok_payload(trace_id, data)


@router.get("/materials/{material_id}")
def agent_material_detail(
    material_id: str,
    request: Request,
    principal: AgentPrincipal = Depends(require_scope("mindos.read")),
):
    """按 ID 读取材料详情（AG-02-04）。

    复用 ingestion.detail_of（状态、版本、summary、tags、contentParts、
    transcript）并投影为 Agent 安全响应；归档/回收/不存在统一 404。不返回
    source_path / previewUrl / 物理路径 / artifact key。
    """
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    rate_limit.check_or_deny(principal.client_id, "detail")
    request.state.agent_client_id = principal.client_id
    started = time.monotonic()
    data = service.material_detail(principal, material_id)
    audit.record(
        trace_id=trace_id,
        client_id=principal.client_id,
        action="material_detail",
        scope="mindos.read",
        resource_type="material",
        resource_id=material_id,
        outcome="ok",
        status_code=200,
        request_digest=audit.stable_digest("material_detail", material_id),
        response_digest=audit.response_digest(data),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ok_payload(trace_id, data)


@router.get("/knowledge/{knowledge_id}")
def agent_knowledge_detail(
    knowledge_id: str,
    request: Request,
    principal: AgentPrincipal = Depends(require_scope("mindos.read")),
):
    """按 ID 读取知识卡片详情（AG-02-04）。

    复用 knowledge.knowledge_view（active 过滤 + 清理正文 + 来源派生 + 证据可用
    标记）并投影为 Agent 安全响应；归档/合并/回收/不存在统一 404。正文不包含
    frontmatter 或重复标题，来源关系由卡片 frontmatter 派生。
    """
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    rate_limit.check_or_deny(principal.client_id, "detail")
    request.state.agent_client_id = principal.client_id
    started = time.monotonic()
    data = service.knowledge_detail(principal, knowledge_id)
    audit.record(
        trace_id=trace_id,
        client_id=principal.client_id,
        action="knowledge_detail",
        scope="mindos.read",
        resource_type="knowledge",
        resource_id=knowledge_id,
        outcome="ok",
        status_code=200,
        request_digest=audit.stable_digest("knowledge_detail", knowledge_id),
        response_digest=audit.response_digest(data),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ok_payload(trace_id, data)


@router.post("/answers")
def agent_answers(
    request: Request,
    body: AnswerRequest,
    principal: AgentPrincipal = Depends(require_scopes("mindos.answer", "mindos.read")),
):
    """带引用的 Agent 问答（AG-03）。

    必须同时具备 mindos.answer 与 mindos.read（防止问答绕过读取权限）。复用
    qa.answer_question 的检索证据与模型管线；citations 关联 evidenceRef 供证据
    展开复核。question 长度校验在 agent/answer_service.py 统一执行。
    """
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    if len(body.question) > agent_config.ANSWER_QUESTION_CHARS_MAX:
        raise AgentError(
            400,
            "VALIDATION_ERROR",
            f"question 超出 {agent_config.ANSWER_QUESTION_CHARS_MAX} 字上限",
        )
    rate_limit.check_or_deny(principal.client_id, "answer")
    request.state.agent_client_id = principal.client_id
    request.state.agent_scope = "mindos.answer"
    started = time.monotonic()
    data = service.answer(principal, body)
    audit.record(
        trace_id=trace_id,
        client_id=principal.client_id,
        action="answer",
        scope="mindos.answer,mindos.read",
        resource_type="",
        resource_id="",
        outcome="ok",
        status_code=200,
        request_digest=audit.stable_digest("answer", body.question[:200]),
        response_digest=audit.response_digest(data),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ok_payload(trace_id, data)


@router.post("/search")
def agent_search(
    request: Request,
    body: SearchRequest,
    principal: AgentPrincipal = Depends(require_scopes("mindos.search", "mindos.read")),
):
    """统一搜索知识卡片与原材料（AG-02-02）。

    检索主体由统一 MindOS 检索服务提供（向量 + BM25 + 知识卡片正文向量 +
    生命周期过滤），此处仅做鉴权、限流、审计与响应信封；参数业务校验在
    agent/search_service.py 执行。结果只返回卡片/材料摘要、ID 与证据句柄，
    不返回 source_path / chunk_id / 内部 score。
    """
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    rate_limit.check_or_deny(principal.client_id, "search")
    request.state.agent_client_id = principal.client_id
    started = time.monotonic()
    data = service.search(principal, body)
    audit.record(
        trace_id=trace_id,
        client_id=principal.client_id,
        action="search",
        scope="mindos.search,mindos.read",
        resource_type="",
        resource_id="",
        outcome="ok",
        status_code=200,
        request_digest=audit.stable_digest("search", body.query[:200]),
        response_digest=audit.response_digest(data),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ok_payload(trace_id, data)


@router.post("/evidence:resolve")
def agent_evidence_resolve(
    request: Request,
    body: EvidenceResolveRequest,
    principal: AgentPrincipal = Depends(require_scope("mindos.read")),
):
    """展开搜索命中的有限证据（AG-02-03）。

    evidenceRef 由搜索结果签发；此处按 client 绑定与过期校验句柄、按当前
    生命周期复核，再读取索引中已有的 chunk / 派生 part 返回有限正文与真实
    定位。任何无效/过期/跨 client/归档/回收/失败 ref 统一 404；处理中材料
    返回 409/EVIDENCE_NOT_READY。不返回 source_path / chunk_id / 内部路径。
    """
    trace_id = getattr(request.state, "agent_trace_id", "") or audit.new_trace_id()
    rate_limit.check_or_deny(principal.client_id, "evidence")
    request.state.agent_client_id = principal.client_id
    started = time.monotonic()
    data = service.resolve_evidence(principal, body)
    audit.record(
        trace_id=trace_id,
        client_id=principal.client_id,
        action="evidence:resolve",
        scope="mindos.read",
        resource_type="",
        resource_id="",
        outcome="ok",
        status_code=200,
        request_digest=audit.stable_digest(
            "evidence:resolve",
            "|".join(evidence.ref_digest(r) for r in body.evidenceRefs),
        ),
        response_digest=audit.response_digest(data),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ok_payload(trace_id, data)
