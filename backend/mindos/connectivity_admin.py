"""Consumer Connectivity 本机管理接口（loopback + CSRF）。

阶段 2：Consumer API Webhook 未接入时，本模块提供本机可用的撤销 / epoch 轮换 /
设备禁用管理入口，使「撤销后 5 秒内断开、连接 epoch 失效、设备禁用」可端到端
验证与回滚。生产事件源接入后，Consumer Webhook 必须复用同一 store API
（mark_revoked / rotate_epoch / set_acl）并同样触发设备上下文失效，不能新增
第二条状态分支。

写操作 require_local（loopback + CSRF），读操作 require_loopback。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import connectivity_session, device_context
from .connectivity_ticket import ConnectivityTicketError
from .stores import connectivity_store

router = APIRouter(prefix="/api/mindos/connectivity", tags=["mindos-connectivity-admin"])

_GLOBAL_ACL_SCOPE = "global"
_DEVICE_ACL_RE = re.compile(r"^device:[A-Za-z0-9._:-]{1,128}$")


class ConnectivityRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(..., min_length=1, max_length=128)
    client_id: str = Field(..., min_length=1, max_length=128)
    device_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field("revoked", max_length=256)


class EpochRotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(..., min_length=1, max_length=128)
    client_id: str = Field(..., min_length=1, max_length=128)
    device_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field("epoch-rotated", max_length=256)


class ConnectivityAclRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(..., min_length=1, max_length=160)
    denied: bool
    reason: str = Field("", max_length=256)


def _validate_acl_scope(scope: str) -> None:
    if scope != _GLOBAL_ACL_SCOPE and not _DEVICE_ACL_RE.match(scope):
        raise ValueError("scope 必须是 'global' 或 'device:<device_id>'")


def revoke_connectivity(req: ConnectivityRevokeRequest):
    result = connectivity_store.mark_revoked(
        account_id=req.account_id,
        client_id=req.client_id,
        device_id=req.device_id,
        reason=req.reason,
    )
    device_context.get_device_registry().invalidate(req.device_id)
    return {"success": True, **result}


def rotate_epoch(req: EpochRotateRequest):
    result = connectivity_store.rotate_epoch(
        account_id=req.account_id,
        client_id=req.client_id,
        device_id=req.device_id,
        reason=req.reason,
    )
    device_context.get_device_registry().invalidate(req.device_id)
    return {"success": True, **result}


def set_acl(req: ConnectivityAclRequest):
    try:
        _validate_acl_scope(req.scope)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    connectivity_store.set_acl(scope=req.scope, denied=req.denied, reason=req.reason)
    return {"success": True, "applied": {"scope": req.scope, "denied": req.denied}}


def exchange_connectivity_session(request: Request):
    """连接票据一次性交换为受控会话凭证（票据即凭证，无需 CSRF）。"""
    try:
        return connectivity_session.exchange_ticket(request.headers.get("authorization"))
    except ConnectivityTicketError as exc:
        raise HTTPException(exc.status_code, exc.message) from None


def connectivity_state():
    state = connectivity_store.snapshot()
    state["deviceContexts"] = device_context.get_device_registry().list()
    return state


def configure_admin_guards(require_local, require_loopback) -> None:
    """由 server 注入本机防护：写操作 require_local，读操作 require_loopback。"""
    global router
    router = APIRouter(prefix="/api/mindos/connectivity", tags=["mindos-connectivity-admin"])
    router.add_api_route(
        "/revoke",
        revoke_connectivity,
        methods=["POST"],
        dependencies=[Depends(require_local)],
    )
    router.add_api_route(
        "/epoch/rotate",
        rotate_epoch,
        methods=["POST"],
        dependencies=[Depends(require_local)],
    )
    router.add_api_route(
        "/acl",
        set_acl,
        methods=["POST"],
        dependencies=[Depends(require_local)],
    )
    # 票据交换仅需 loopback：票据本身即凭证（CSRF 防护面向浏览器会话，不适用）。
    router.add_api_route(
        "/sessions/exchange",
        exchange_connectivity_session,
        methods=["POST"],
        dependencies=[Depends(require_loopback)],
    )
    router.add_api_route(
        "/state",
        connectivity_state,
        methods=["GET"],
        dependencies=[Depends(require_loopback)],
    )
