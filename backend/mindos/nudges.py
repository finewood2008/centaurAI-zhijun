"""知君提醒路由：今日提醒、立即扫描、稍后 / 永久静默、策略。契约见 zhijun-api-contract.md §7。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .stores.conversation_store import ConversationError, ConversationStore
from .zhijun import nudges as nudge_service

_PREFIX = "/api/mindos/nudges"
_TAGS = ["zhijun-nudges"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyUpdate(_StrictModel):
    enabled: bool | None = None
    maxPerDay: int | None = Field(default=None, ge=1, le=10)
    silencedRefs: list[str] | None = Field(default=None, max_length=200)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "detail": message})


def get_today():
    return nudge_service.today()


def scan_now():
    return nudge_service.scan()


def dismiss(nudge_id: str):
    store = ConversationStore.instance()
    if store.get_nudge(nudge_id) is None:
        raise _error(404, "NOT_FOUND", "提醒不存在")
    return store.set_nudge_status(nudge_id, "dismissed")


def silence(nudge_id: str):
    store = ConversationStore.instance()
    nudge = store.get_nudge(nudge_id)
    if nudge is None:
        raise _error(404, "NOT_FOUND", "提醒不存在")
    policy = store.silence_trigger(nudge["triggerKey"])
    return {"nudge": store.get_nudge(nudge_id), "policy": policy}


def get_policy():
    return ConversationStore.instance().nudge_policy()


def put_policy(req: PolicyUpdate):
    try:
        return ConversationStore.instance().save_nudge_policy(
            enabled=req.enabled, max_per_day=req.maxPerDay, silenced_refs=req.silencedRefs
        )
    except ConversationError as exc:
        raise _error(400, "BAD_REQUEST", str(exc)) from None


def _build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=_TAGS)
    write_dependencies = [Depends(write_guard)] if write_guard is not None else []
    built.add_api_route("/today", get_today, methods=["GET"])
    built.add_api_route("/scan", scan_now, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{nudge_id}/dismiss", dismiss, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{nudge_id}/silence", silence, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/policy", get_policy, methods=["GET"])
    built.add_api_route("/policy", put_policy, methods=["PUT"], dependencies=write_dependencies)
    return built


router = _build_router()


def configure_write_guard(guard) -> None:
    global router
    router = _build_router(guard)
