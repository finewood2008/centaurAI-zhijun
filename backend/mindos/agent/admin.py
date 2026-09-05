"""外部 Agent Gateway 本机管理员接口（AG-01）。

令牌创建 / 轮换 / 停用与审计日志查看仅限本机（loopback + CSRF），
不开放给外部 Agent。明文 token 仅在创建 / 轮换响应中展示一次。

V1 固定单工作区：创建接口不接受 workspace 入参（extra=forbid），
客户端永远归属配置的 WORKSPACE_ID，防止客户端自行声明工作区。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from . import store as agent_store
from .errors import AgentError

admin_router = APIRouter(prefix="/api/agent", tags=["agent-admin"])


class AgentClientCreate(BaseModel):
    # V1 单工作区：不暴露 workspace 入参；多余字段一律拒绝（400/VALIDATION_ERROR）。
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    scopes: list[str] = Field(..., min_length=1)
    expiresAt: Optional[datetime] = None


def _client_created_response(client: dict, token: str) -> dict:
    return {
        "success": True,
        "client": client,
        "token": token,
        "token_display_once": True,
    }


def _expiry_timestamp(req: AgentClientCreate) -> float | None:
    if req.expiresAt is None:
        return None
    if req.expiresAt.tzinfo is None or req.expiresAt.utcoffset() is None:
        raise AgentError(400, "VALIDATION_ERROR", "expiresAt 必须是带时区的 ISO-8601 时间")
    if req.expiresAt.timestamp() <= time.time():
        raise AgentError(400, "VALIDATION_ERROR", "expiresAt 必须晚于当前时间")
    return req.expiresAt.timestamp()


def create_client(req: AgentClientCreate):
    try:
        client, token = agent_store.instance().create_client(
            req.name,
            req.scopes,
            expires_at=_expiry_timestamp(req),
        )
    except ValueError as exc:
        raise AgentError(400, "VALIDATION_ERROR", str(exc))
    return _client_created_response(client, token)


def list_clients(includeDisabled: bool = False):
    clients = agent_store.instance().list_clients(include_disabled=includeDisabled)
    return {"clients": clients, "total": len(clients)}


def rotate_client(client_id: str):
    try:
        client, token = agent_store.instance().rotate_client(client_id)
    except ValueError as exc:
        raise AgentError(404, "RESOURCE_NOT_FOUND", str(exc))
    return _client_created_response(client, token)


def disable_client(client_id: str):
    if not agent_store.instance().disable_client(client_id):
        raise AgentError(404, "RESOURCE_NOT_FOUND", "客户端不存在")
    return {"success": True, "client_id": client_id, "disabled": True}


def list_audit(limit: int = 100, client_id: str = "", trace_id: str = ""):
    items = agent_store.instance().list_audit(
        limit=limit, client_id=client_id, trace_id=trace_id
    )
    return {"items": items, "total": len(items)}


def configure_admin_guards(require_local, require_loopback) -> None:
    """由 server 注入本机防护：写操作 require_local，读操作 require_loopback。"""
    global admin_router
    admin_router = APIRouter(prefix="/api/agent", tags=["agent-admin"])
    admin_router.add_api_route(
        "/clients", create_client, methods=["POST"], dependencies=[Depends(require_local)]
    )
    admin_router.add_api_route(
        "/clients", list_clients, methods=["GET"], dependencies=[Depends(require_loopback)]
    )
    admin_router.add_api_route(
        "/clients/{client_id}/rotate",
        rotate_client,
        methods=["POST"],
        dependencies=[Depends(require_local)],
    )
    admin_router.add_api_route(
        "/clients/{client_id}/disable",
        disable_client,
        methods=["POST"],
        dependencies=[Depends(require_local)],
    )
    admin_router.add_api_route(
        "/audit", list_audit, methods=["GET"], dependencies=[Depends(require_loopback)]
    )
