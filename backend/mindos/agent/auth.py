"""Agent Gateway 认证与授权（AG-01）。

- Authorization: Bearer <token> 服务到服务凭证
- 数据库仅存散列；日志/异常/审计/响应均不出现明文 token
- scope 校验与资源可见性策略在此统一执行
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, Request

from . import config as agent_config
from . import errors, store as agent_store
from .errors import AgentError


@dataclass(frozen=True)
class AgentPrincipal:
    """认证后的调用方身份。"""

    client_id: str
    name: str
    scopes: frozenset
    workspace_id: str


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise AgentError(401, "AUTHENTICATION_REQUIRED", "缺少 Authorization: Bearer <token> 凭证")
    if not authorization.lower().startswith("bearer "):
        raise AgentError(401, "AUTHENTICATION_REQUIRED", "Authorization 头格式必须是 Bearer <token>")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise AgentError(401, "AUTHENTICATION_REQUIRED", "凭证为空")
    return token


def authenticate_agent(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> AgentPrincipal:
    """解析并校验 Bearer 凭证，返回 AgentPrincipal；任何失败即拒绝（默认拒绝）。"""
    if not agent_config.gateway_enabled():
        raise AgentError(403, "POLICY_DENIED", "外部 Agent Gateway 未启用")
    token = _extract_bearer(authorization)
    # 仅在此处接触明文 token 用于散列查找，随后立即丢弃；绝不进入日志/审计/响应。
    record = agent_store.instance().authenticate(token)
    if record is None:
        # 不区分「不存在 / 停用 / 过期」，统一 TOKEN_INVALID 防枚举。
        raise AgentError(401, "TOKEN_INVALID", "凭证无效或已过期")
    principal = AgentPrincipal(
        client_id=record["client_id"],
        name=record["name"],
        scopes=frozenset(record["scopes"]),
        workspace_id=record["workspace"] or agent_config.WORKSPACE_ID,
    )
    request.state.agent_client_id = principal.client_id
    request.state.agent_principal = principal
    return principal


def require_agent(principal: AgentPrincipal = Depends(authenticate_agent)) -> AgentPrincipal:
    return principal


def require_scope(scope: str):
    """返回一个 FastAPI 依赖：要求调用方具备指定 scope，否则 403 SCOPE_DENIED。

    无论成功与否都记录目标 scope 到 request.state.agent_scope，
    便于后续未处理异常/审计定位调用目标。
    """

    def dependency(
        request: Request,
        principal: AgentPrincipal = Depends(authenticate_agent),
    ) -> AgentPrincipal:
        request.state.agent_scope = scope
        if scope not in principal.scopes:
            raise AgentError(
                403,
                "SCOPE_DENIED",
                f"当前凭证不具有 {scope} 权限",
            )
        return principal

    return dependency


def require_scopes(*scopes: str):
    """要求调用方同时具备全部指定 scopes（用于 answers 等需读取范围约束的能力）。

    成功时 request.state.agent_scope 记录完整 scope 列表（逗号分隔），
    失败时记录缺失项，供审计定位。
    """

    def dependency(
        request: Request,
        principal: AgentPrincipal = Depends(authenticate_agent),
    ) -> AgentPrincipal:
        missing = next((s for s in scopes if s not in principal.scopes), None)
        if missing is not None:
            request.state.agent_scope = missing
            raise AgentError(
                403,
                "SCOPE_DENIED",
                f"当前凭证不具有 {missing} 权限",
            )
        request.state.agent_scope = ",".join(scopes)
        return principal

    return dependency
