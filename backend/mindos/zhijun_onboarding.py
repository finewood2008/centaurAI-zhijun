"""知君首次引导：显式状态、可恢复建档会话与完成动作。

状态保存在 ontology_meta；它只描述引导进度，不复制本体、判断或资料内容。
已有数据首次升级时迁移为 ready，避免打断老用户。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .stores.conversation_store import ConversationStore
from .stores.ontology_store import OntologyStore

_PREFIX = "/api/mindos/zhijun/onboarding"
_META_KEY = "zhijun_onboarding_state_v1"
_LOCK = threading.Lock()

OnboardingState = Literal[
    "welcome",
    "profile_building",
    "profile_review",
    "starter_import",
    "source_connect",
    "first_result",
    "ready",
]
OnboardingAction = Literal[
    "start",
    "skip",
    "profile_ready",
    "profile_confirmed",
    "import_completed",
    "skip_import",
    "sources_completed",
    "skip_sources",
    "finish",
    "restart",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnboardingCommand(_StrictModel):
    action: OnboardingAction
    conversationId: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _base(state: OnboardingState, *, migrated: bool = False, conversation_id: str | None = None) -> dict:
    now = _now()
    return {
        "state": state,
        "conversationId": conversation_id,
        "profileReviewed": state in ("starter_import", "source_connect", "first_result", "ready"),
        "starterImport": "pending",
        "sourceConnect": "pending",
        "startedAt": None if state == "ready" and migrated else now,
        "updatedAt": now,
        "completedAt": now if state == "ready" else None,
        "migrated": migrated,
    }


def _key(scope: str) -> str:
    return _META_KEY if scope == "global" else _META_KEY + ":" + scope


def _read(store: OntologyStore, scope="global") -> dict | None:
    raw = store.meta_get(_key(scope))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("state") in OnboardingState.__args__ else None


def _write(store: OntologyStore, progress: dict, scope="global") -> dict:
    progress = {**progress, "updatedAt": _now()}
    store.meta_set(_key(scope), json.dumps(progress, ensure_ascii=False, separators=(",", ":")))
    return progress


def _migrate(ontology: OntologyStore, conversations: ConversationStore, scope="global") -> dict:
    # An archived first meeting still belongs to the user's onboarding history.
    onboarding = next(iter(conversations.list_conversations(limit=1, status="all", mode="onboarding", device_scope=scope)), None)
    if onboarding:
        # 旧流程 8 个用户轮次；新流程有一条 assistant opening，7 个用户轮次。
        messages = conversations.list_messages(onboarding["id"])
        from .zhijun.persona import onboarding_answer_count
        user_turns = onboarding_answer_count(messages)
        has_opening = any(item.get("role") == "assistant" and (item.get("meta") or {}).get("kind") == "onboarding_open" for item in messages)
        complete = user_turns >= (7 if has_opening else 8)
        if not complete:
            return _write(ontology, _base("profile_building", migrated=True, conversation_id=onboarding["id"]), scope)
    with ontology._connect() as db:
        has_ontology = db.execute("SELECT 1 FROM claims WHERE device_scope=? AND trust_state IN ('working','confirmed') LIMIT 1", (scope,)).fetchone() is not None
    if has_ontology or onboarding:
        return _write(ontology, _base("ready", migrated=True, conversation_id=onboarding["id"] if onboarding else None), scope)
    return _write(ontology, _base("welcome"), scope)


def get_progress(*, ontology: OntologyStore | None = None, conversations: ConversationStore | None = None, scope="global") -> dict:
    onto = ontology or OntologyStore.instance()
    convs = conversations or ConversationStore.instance()
    progress = _read(onto, scope)
    if progress and progress.get("conversationId"):
        from .zhijun.alignment import scope_for
        conversation = convs.get_conversation(progress["conversationId"])
        if not conversation or scope_for(progress["conversationId"], convs) != scope:
            progress = None
    progress = progress or _migrate(onto, convs, scope)
    from .zhijun.charter import topic_progress
    cid = progress.get("conversationId")
    topics = topic_progress(convs.list_messages(cid)) if cid and convs.get_conversation(cid) else topic_progress([])
    return {**progress, "topics": topics, "canStart": True}


def _fail(message: str) -> HTTPException:
    return HTTPException(409, {"code": "ONBOARDING_TRANSITION", "detail": message})


def _require(progress: dict, *states: OnboardingState) -> None:
    if progress.get("state") not in states:
        raise _fail(f"当前处于 {progress.get('state')}，不能执行这一步")


def _start(progress: dict, ontology: OntologyStore, conversations: ConversationStore, scope="global") -> dict:
    if progress.get("state") == "profile_building" and progress.get("conversationId"):
        if conversations.get_conversation(progress["conversationId"]):
            return progress
    _require(progress, "welcome", "profile_building")
    conversation = conversations.create_conversation(mode="onboarding", title="第一次认识", device_scope=scope)
    from .stores.routing_store import RoutingStore
    routing = RoutingStore(ontology)
    default = routing.mode("default:" + scope)
    if default["mode"] in ("local", "online"):
        routing.set_mode(conversation["id"], default["mode"], default["service"])
    from .zhijun.charter import TOPICS
    conversations.append_message(
        conversation["id"],
        "assistant",
        "我们先聊几个小话题，没想清楚可以跳过，随时开始使用。" + TOPICS[0][2],
        provider="template",
        model="template",
        # This is a local application template with no ancestry. Mark it at
        # creation so later reply assistance does not mistake it for opaque
        # pre-routing history and fail lifecycle validation.
        meta={"kind": "onboarding_open", "onboardingTopic": "situation", "question": 1,
              "routingOrigin": {"service": "", "external": False}, "routingSources": []},
    )
    started = _base("profile_building", conversation_id=conversation["id"])
    started["startedAt"] = progress.get("startedAt") or started["startedAt"]
    return _write(ontology, started, scope)


def apply_action(
    command: OnboardingCommand,
    *,
    ontology: OntologyStore | None = None,
    conversations: ConversationStore | None = None,
    scope: str = "global",
) -> dict:
    onto = ontology or OntologyStore.instance()
    convs = conversations or ConversationStore.instance()
    with _LOCK:
        progress = get_progress(ontology=onto, conversations=convs, scope=scope)
        action = command.action
        if action == "restart":
            return _write(onto, _base("welcome"), scope)
        if action == "skip":
            if progress.get("state") == "ready":
                return progress
            progress["state"] = "ready"
            if progress.get("starterImport") == "pending":
                progress["starterImport"] = "skipped"
            if progress.get("sourceConnect") == "pending":
                progress["sourceConnect"] = "skipped"
            progress["completedAt"] = _now()
            return _write(onto, progress, scope)
        if action == "start":
            return _start(progress, onto, convs, scope)
        if command.conversationId and progress.get("conversationId") and command.conversationId != progress["conversationId"]:
            raise _fail("这不是当前引导使用的建档会话")

        if action == "profile_ready":
            _require(progress, "profile_building", "profile_review")
            conversation_id = progress.get("conversationId")
            conversation = convs.get_conversation(conversation_id) if conversation_id else None
            if conversation is None:
                raise _fail("找不到当前建档会话")
            progress["state"] = "profile_review"
        elif action == "profile_confirmed":
            _require(progress, "profile_review")
            progress["state"] = "ready"
            progress["profileReviewed"] = True
            progress["completedAt"] = _now()
        elif action in ("import_completed", "skip_import"):
            _require(progress, "starter_import")
            progress["state"] = "source_connect"
            progress["starterImport"] = "completed" if action == "import_completed" else "skipped"
        elif action in ("sources_completed", "skip_sources"):
            _require(progress, "source_connect")
            progress["state"] = "first_result"
            progress["sourceConnect"] = "completed" if action == "sources_completed" else "skipped"
        elif action == "finish":
            progress["state"] = "ready"
            progress["completedAt"] = _now()
        else:
            raise _fail("未知的引导动作")
        return _write(onto, progress, scope)


def read_progress(request: Request):
    from .uploads import _device_scope_of
    return get_progress(scope=_device_scope_of(request))


def update_progress(command: OnboardingCommand, request: Request):
    from .uploads import _device_scope_of
    return apply_action(command, scope=_device_scope_of(request))


def _build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=["zhijun-onboarding"])
    write_dependencies = [Depends(write_guard)] if write_guard is not None else []
    built.add_api_route("", read_progress, methods=["GET"])
    built.add_api_route("", update_progress, methods=["POST"], dependencies=write_dependencies)
    return built


router = _build_router()


def configure_write_guard(guard) -> None:
    """为推进引导状态的写接口注入 server.py 的 loopback + CSRF 防护。"""
    global router
    router = _build_router(guard)
