"""知君对话路由：会话 CRUD、一轮流式生成（SSE）、出设备回执。

契约见 docs/development/zhijun-api-contract.md §2。写路由挂 server.py 的 loopback + CSRF 防护。
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .stores.conversation_store import ConversationError, ConversationNotFoundError, ConversationStore
from .zhijun.turn import TurnError, run_turn

_PREFIX = "/api/mindos/conversations"
_TAGS = ["zhijun-conversations"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(_StrictModel):
    mode: Literal["chat", "onboarding"] = "chat"
    title: str = Field(default="", max_length=80)


class MessageCreate(_StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    depth: Literal["brief", "deep"] = "brief"


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "detail": message})


def _store() -> ConversationStore:
    return ConversationStore.instance()


def create_conversation(req: ConversationCreate):
    try:
        return _store().create_conversation(mode=req.mode, title=req.title)
    except ConversationError as exc:
        raise _error(400, "BAD_REQUEST", str(exc)) from None


def list_conversations(limit: int = Query(50, ge=1, le=200)):
    items = _store().list_conversations(limit=limit)
    return {"items": items, "total": len(items)}


def get_conversation(conversation_id: str):
    store = _store()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    return {"conversation": conversation, "messages": store.list_messages(conversation_id)}


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
    generator = run_turn(conversation_id, req.content, depth=req.depth)
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


def _build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=_TAGS)
    write_dependencies = [Depends(write_guard)] if write_guard is not None else []
    built.add_api_route("", create_conversation, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("", list_conversations, methods=["GET"])
    built.add_api_route("/{conversation_id}", get_conversation, methods=["GET"])
    built.add_api_route("/{conversation_id}", delete_conversation, methods=["DELETE"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/messages", post_message, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/messages/{message_id}/receipt", get_receipt, methods=["GET"])
    return built


router = _build_router()


def configure_write_guard(guard) -> None:
    """为所有写路由注入 server.py 的 loopback + CSRF 防护。"""
    global router
    router = _build_router(guard)
