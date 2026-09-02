"""知君对话路由：会话 CRUD、一轮流式生成（SSE）、出设备回执、判断草稿、回访结果。

契约见 docs/development/zhijun-api-contract.md §2、§6、§8。写路由挂 server.py 的 loopback + CSRF 防护。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .stores.conversation_store import ConversationError, ConversationNotFoundError, ConversationStore
from .zhijun import deliberate
from .zhijun.turn import TurnError, run_turn

_PREFIX = "/api/mindos/conversations"
_TAGS = ["zhijun-conversations"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(_StrictModel):
    mode: Literal["chat", "onboarding", "review"] = "chat"
    title: str = Field(default="", max_length=80)
    decisionId: str | None = Field(default=None, max_length=100)


class MessageCreate(_StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    depth: Literal["brief", "deep"] = "brief"
    mode: Literal["chat", "deliberate"] = "chat"


class DraftConfirm(_StrictModel):
    choice: str | None = Field(default=None, max_length=2000)
    rationale: str | None = Field(default=None, max_length=10000)
    confidence: int | None = Field(default=None, ge=0, le=100)
    expectedOutcome: str | None = Field(default=None, max_length=5000)
    reviewAt: datetime | None = None
    title: str | None = Field(default=None, max_length=300)
    options: list[str] | None = Field(default=None, max_length=30)


class OutcomeBody(_StrictModel):
    result: str = Field(min_length=1, max_length=10000)
    notes: str = Field(default="", max_length=10000)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "detail": message})


def _store() -> ConversationStore:
    return ConversationStore.instance()


def _growth_store():
    from .stores.growth_store import GrowthStore

    return GrowthStore.instance()


def create_conversation(req: ConversationCreate):
    store = _store()
    decision = None
    if req.mode == "review":
        if not req.decisionId:
            raise _error(400, "BAD_REQUEST", "回访会话需要 decisionId")
        decision = _growth_store().get_decision(req.decisionId)
        if decision is None:
            raise _error(404, "DECISION_NOT_FOUND", "判断不存在")
    try:
        conversation = store.create_conversation(
            mode=req.mode,
            title=req.title or (f"回访：{decision['title']}" if decision else ""),
            decision_id=decision["id"] if decision else None,
        )
    except ConversationError as exc:
        raise _error(400, "BAD_REQUEST", str(exc)) from None
    if decision is not None:
        store.append_message(
            conversation["id"],
            "system",
            f"这是对「{decision['title']}」的回访：当时你选了「{decision['choice']}」，把握 {decision['confidence']}%，预期「{decision['expectedOutcome']}」。",
            meta={"kind": "review_open", "decisionId": decision["id"], "status": decision["status"]},
        )
        conversation = store.get_conversation(conversation["id"])
    return conversation


def list_conversations(limit: int = Query(50, ge=1, le=200)):
    items = _store().list_conversations(limit=limit)
    return {"items": items, "total": len(items)}


def get_conversation(conversation_id: str):
    store = _store()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    payload = {"conversation": conversation, "messages": store.list_messages(conversation_id)}
    draft = store.get_draft(conversation_id)
    if draft is not None:
        payload["decisionDraft"] = draft
    if conversation.get("decisionId"):
        payload["decision"] = _growth_store().get_decision(conversation["decisionId"])
    return payload


def delete_conversation(conversation_id: str):
    if not _store().delete_conversation(conversation_id):
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    return {"deleted": True, "id": conversation_id}


def get_receipt(conversation_id: str, message_id: str):
    receipt = _store().get_receipt(message_id)
    if receipt is None or receipt.get("conversationId") != conversation_id:
        raise _error(404, "RECEIPT_NOT_FOUND", "回执不存在")
    return receipt


def _encode(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def post_message(conversation_id: str, req: MessageCreate):
    generator = run_turn(conversation_id, req.content, depth=req.depth, mode=req.mode)
    try:
        first = next(generator)
    except TurnError as exc:
        raise _error(exc.status_code, exc.code, exc.message) from None
    except ConversationNotFoundError as exc:
        raise _error(404, "CONVERSATION_NOT_FOUND", str(exc)) from None
    except StopIteration:
        raise _error(500, "EMPTY_STREAM", "生成流提前结束") from None

    def body():
        try:
            yield _encode(*first)
            for name, data in generator:
                yield _encode(name, data)
        finally:
            # 客户端断开时触发 run_turn 的 GeneratorExit：已生成文本以 aborted 落库。
            generator.close()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------- 判断草稿（P2）
def get_draft(conversation_id: str):
    if _store().get_conversation(conversation_id) is None:
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    draft = _store().get_draft(conversation_id)
    if draft is None:
        raise _error(404, "DRAFT_NOT_FOUND", "这段对话还没有判断草稿")
    return draft


def confirm_draft(conversation_id: str, req: DraftConfirm):
    overrides = req.model_dump()
    if overrides.get("reviewAt") is not None:
        overrides["reviewAt"] = overrides["reviewAt"].isoformat()
    try:
        return deliberate.confirm_draft(conversation_id, overrides, conv_store=_store())
    except ConversationNotFoundError as exc:
        raise _error(404, "DRAFT_NOT_FOUND", str(exc)) from None
    except ConversationError as exc:
        raise _error(400, "DRAFT_INCOMPLETE", str(exc)) from None


def discard_draft(conversation_id: str):
    try:
        return deliberate.discard_draft(conversation_id, conv_store=_store())
    except ConversationNotFoundError as exc:
        raise _error(404, "DRAFT_NOT_FOUND", str(exc)) from None
    except ConversationError as exc:
        raise _error(409, "DRAFT_STATE", str(exc)) from None


# ---------------------------------------------------------------- 回访结果（P2）
def record_outcome(conversation_id: str, req: OutcomeBody):
    store = _store()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    decision_id = conversation.get("decisionId")
    if not decision_id:
        raise _error(400, "NOT_REVIEW", "这不是回访会话，没有绑定的判断")
    from . import growth as growth_api

    ref = json.dumps({"kind": "conversation", "conversationId": conversation_id}, ensure_ascii=False)
    decision = growth_api.record_outcome(
        decision_id, growth_api.OutcomeCreate(result=req.result, notes=req.notes, evidenceRefs=[ref])
    )  # 404 / 409 由 growth 层的 HTTPException 直接透传
    store.append_message(
        conversation_id,
        "system",
        f"你记下了结果：{req.result.strip()[:200]}",
        meta={"kind": "outcome_recorded", "decisionId": decision_id},
    )
    from .zhijun.nudges import trigger_key_for

    acted = store.act_nudges(trigger_key_for(decision_id))
    return {"decision": decision, "nudgesActed": acted}


def _build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=_TAGS)
    write_dependencies = [Depends(write_guard)] if write_guard is not None else []
    built.add_api_route("", create_conversation, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("", list_conversations, methods=["GET"])
    built.add_api_route("/{conversation_id}", get_conversation, methods=["GET"])
    built.add_api_route("/{conversation_id}", delete_conversation, methods=["DELETE"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/messages", post_message, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/messages/{message_id}/receipt", get_receipt, methods=["GET"])
    built.add_api_route("/{conversation_id}/decision-draft", get_draft, methods=["GET"])
    built.add_api_route("/{conversation_id}/decision-draft/confirm", confirm_draft, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/decision-draft/discard", discard_draft, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/outcome", record_outcome, methods=["POST"], dependencies=write_dependencies)
    return built


router = _build_router()


def configure_write_guard(guard) -> None:
    """为所有写路由注入 server.py 的 loopback + CSRF 防护。"""
    global router
    router = _build_router(guard)
