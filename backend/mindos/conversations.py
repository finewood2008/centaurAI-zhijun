"""知君对话路由：会话 CRUD、一轮流式生成（SSE）、出设备回执、判断草稿、回访结果。

契约见 docs/development/zhijun-api-contract.md §2、§6、§8。写路由挂 server.py 的 loopback + CSRF 防护。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .stores.conversation_store import ConversationError, ConversationNotFoundError, ConversationStore
from .zhijun import deliberate, persona
from .zhijun.turn import TurnError, run_turn
from .chat_import_routes import MaterialRef
from .uploads import _device_scope_of
from .zhijun.reply_assistance import ReplyInput

_PREFIX = "/api/mindos/conversations"
_TAGS = ["zhijun-conversations"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(_StrictModel):
    mode: Literal["chat", "onboarding", "review"] = "chat"
    title: str = Field(default="", max_length=80)
    decisionId: str | None = Field(default=None, max_length=100)
    taskContext: Literal["charter"] | None = None


class ConversationUpdate(_StrictModel):
    expectedRevision: int = Field(ge=0, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=80)
    status: Literal["active", "archived"] | None = None
    pinned: bool | None = Field(default=None, strict=True)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        if not isinstance(value, str):
            raise ValueError("名称必须是文字")
        return value.strip()

    @model_validator(mode="after")
    def require_changes(self):
        fields = self.model_fields_set - {"expectedRevision"}
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError("请提供要修改的名称、归档状态或置顶状态")
        return self


class MessageCreate(_StrictModel):
    content: str = Field(default="", max_length=4000)
    depth: Literal["brief", "deep"] = "brief"
    mode: Literal["chat", "deliberate"] = "chat"
    materialRefs: list[MaterialRef] = Field(default_factory=list, max_length=5)
    localOnly: bool = False
    routeRevision: str | None = Field(default=None, max_length=64)
    omitSources: bool = False
    requestId: str | None = Field(default=None, min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    retryUserMessageId: str | None = Field(default=None, max_length=100)
    replyAssistance: ReplyInput | None = None
    charterExceptionId: str | None = Field(default=None, max_length=100)


class DraftConfirm(_StrictModel):
    choice: str | None = Field(default=None, max_length=2000)
    rationale: str | None = Field(default=None, max_length=10000)
    confidence: int | None = Field(default=None, ge=0, le=100)
    expectedOutcome: str | None = Field(default=None, max_length=5000)
    reviewAt: datetime | None = None
    title: str | None = Field(default=None, max_length=300)
    options: list[str] | None = Field(default=None, max_length=30)
    assistedFields: list[Literal["choice", "rationale", "expectedOutcome"]] | None = Field(default=None, max_length=3)


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


def create_conversation(req: ConversationCreate, request: Request = None):
    store = _store()
    decision = None
    if req.mode == "review":
        if not req.decisionId:
            raise _error(400, "BAD_REQUEST", "回访会话需要 decisionId")
        decision = _growth_store().get_decision(req.decisionId)
        if decision is None:
            raise _error(404, "DECISION_NOT_FOUND", "判断不存在")
    try:
        # Serialize lookup/create only; never hold the conversation lock while
        # initializing consent or other stores.
        with store._lock:
            if decision is not None:
                existing = store.find_conversation_by_decision(decision["id"], mode="review", status="all",
                                                               device_scope=_device_scope_of(request))
                if existing is not None:
                    return {**existing, "reused": True}
            conversation = store.create_conversation(
                mode=req.mode,
                title=req.title or (f"回访：{decision['title']}" if decision else ""),
                decision_id=decision["id"] if decision else None,
                device_scope=_device_scope_of(request),
            )
    except ConversationError as exc:
        raise _error(400, "BAD_REQUEST", str(exc)) from None
    from .stores.routing_store import RoutingStore
    routing = RoutingStore(_ontology_store())
    if req.taskContext:
        routing.set_task(conversation["id"], req.taskContext)
    default = routing.mode("default:" + _device_scope_of(request))
    if default["mode"] != "legacy":
        routing.set_mode(conversation["id"], default["mode"], default["service"])
    if decision is not None:
        # 开场是知君的一句话（模板生成，不调模型）：先问感受，不催结果。
        from .zhijun.history import local_only_decision
        store.append_message(
            conversation["id"],
            "assistant",
            persona.review_opening(decision),
            provider="template",
            model="template",
            meta={"kind": "review_open", "decisionId": decision["id"], "status": decision["status"],
                  "localOnlyDerived": local_only_decision(decision)},
        )
        conversation = store.get_conversation(conversation["id"])
        conversation["reused"] = False
    return conversation


# ---------------------------------------------------------------- 对话产出
def _ontology_store():
    from .stores.ontology_store import OntologyStore

    return OntologyStore.instance()


def _decision_brief(decision: dict | None) -> dict | None:
    if not decision:
        return None
    return {k: decision.get(k) for k in ("id", "title", "choice", "reviewAt", "status")}


def _outcome_decision_id(store: ConversationStore, conversation: dict, confirmed_map: dict[str, str] | None = None) -> str | None:
    """本会话确认入簿的判断：conversation.decisionId，或本会话里已确认草稿绑定的判断。"""
    if conversation.get("decisionId"):
        return str(conversation["decisionId"])
    if confirmed_map is not None:
        return confirmed_map.get(conversation["id"])
    return store.confirmed_decision_id(conversation["id"])


def _claim_brief(claim: dict) -> dict:
    return {"id": claim["id"], "content": claim["content"], "section": claim["section"], "layer": claim["layer"]}


def get_outcomes(conversation_id: str):
    """这段对话留下了什么：本会话归属的已确认 / 待确认理解、确认入簿的判断、带期限的承诺、后台待处理数、已撤回数。"""
    store = _store()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    onto = _ontology_store()
    claims = onto.claims_for_conversation(conversation_id, trust_states=("confirmed", "working"))
    confirmed = [c for c in claims if c["trustState"] == "confirmed"]
    working = [c for c in claims if c["trustState"] == "working"]
    commitments = [
        {"claimId": c["id"], "content": c["content"], "validTo": c["validTo"]}
        for c in claims
        if c.get("predicate") == "committed_to" and c.get("validTo")
    ]
    decision = None
    decision_id = _outcome_decision_id(store, conversation)
    if decision_id:
        decision = _decision_brief(_growth_store().get_decision(decision_id))
    try:
        pending = onto.pending_jobs_for_conversation(conversation_id)
    except Exception:  # noqa: BLE001 - 取不到本会话的就给全局数
        pending = onto.pending_jobs()
    counts = onto.conversation_outcome_counts([conversation_id]).get(conversation_id) or {}
    return {
        "conversationId": conversation_id,
        "confirmedClaims": [_claim_brief(c) for c in confirmed],
        "workingClaims": [_claim_brief(c) for c in working],
        "decision": decision,
        "commitments": commitments,
        "pendingJobs": int(pending),
        "retracted": int(counts.get("retracted") or 0),
    }


def list_conversations(limit: int = Query(50, ge=1, le=200), request: Request = None,
                       status: Literal["active", "archived", "all"] = "active",
                       q: str = Query("", max_length=100), offset: int = Query(0, ge=0, le=2**63 - 1)):
    store = _store()
    page = store.query_conversations(limit=limit, status=status, q=q.strip(), offset=offset,
                                     device_scope=_device_scope_of(request))
    items = page["items"]
    counts: dict[str, dict] = {}
    confirmed_map: dict[str, str] = {}
    try:
        counts = _ontology_store().conversation_outcome_counts([item["id"] for item in items])
        confirmed_map = store.confirmed_decision_ids()
    except Exception:  # noqa: BLE001 - 产出计数是附加信息，不阻塞列表
        counts, confirmed_map = {}, {}
    for item in items:
        item.setdefault("mode", "chat")
        count = counts.get(item["id"]) or {}
        item["outcomes"] = {
            "confirmed": int(count.get("confirmed") or 0),
            "working": int(count.get("working") or 0),
            "decision": bool(_outcome_decision_id(store, item, confirmed_map)),
            "commitments": int(count.get("commitments") or 0),
        }
    return {**page, "items": items}


def update_conversation(conversation_id: str, req: ConversationUpdate, request: Request = None):
    from .chat_imports import require_conversation
    from .stores.conversation_store import ConversationConflictError
    scope = _device_scope_of(request)
    require_conversation(conversation_id, scope)
    try:
        return _store().update_metadata(conversation_id, expected_revision=req.expectedRevision,
            title=req.title, status=req.status, pinned=req.pinned, device_scope=scope)
    except ConversationNotFoundError as exc:
        raise _error(404, "CONVERSATION_NOT_FOUND", str(exc)) from None
    except ConversationConflictError as exc:
        raise _error(409, "CONVERSATION_CHANGED", str(exc)) from None
    except ConversationError as exc:
        raise _error(400, "BAD_CONVERSATION_UPDATE", str(exc)) from None


def _provenance_from_receipt(receipt: dict | None) -> dict | None:
    """把落库的出设备回执还原成前端出处条需要的结构（刷新后历史回复也有出处）。"""
    if not receipt:
        return None
    from .stores.ontology_store import OntologyStore

    onto = OntologyStore.instance()

    def _briefs(ids: list[str]) -> list[dict]:
        items = []
        for claim_id in ids:
            claim = onto.get_claim(claim_id, with_evidence=False)
            if claim is None:
                continue
            items.append({"id": claim["id"], "content": claim["content"], "section": claim["section"], "layer": claim["layer"], "trustState": claim["trustState"]})
        return items

    materials = []
    for key in receipt.get("materialChunkKeys") or []:
        material_id = str(key).split("::", 1)[0]
        materials.append({"materialId": material_id, "title": material_id, "chunkKey": key})
    return {
        "confirmedClaims": _briefs(receipt.get("confirmedClaimIds") or []),
        "workingClaims": _briefs(receipt.get("workingClaimIds") or []),
        "materials": materials,
        "retractedNotices": int(receipt.get("retractedNoticeCount") or 0),
        "charterVersion": None,
        "promptChars": int(receipt.get("promptChars") or 0),
        "channel": "external" if receipt.get("external") else "local",
        "pastDecisions": [],
        "anchorClaimIds": [],
        "fromReceipt": True,
    }


def get_conversation(conversation_id: str, request: Request = None):
    store = _store()
    from .chat_imports import require_conversation
    from .uploads import _device_scope_of
    require_conversation(conversation_id, _device_scope_of(request))
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    messages = store.list_messages(conversation_id)
    for message in messages:
        if (message.get("meta") or {}).get("routingProvenance"):
            message["provenance"] = message["meta"]["routingProvenance"]
        if message["role"] == "assistant":
            provenance = (message.get("meta") or {}).get("routingProvenance") or (message.get("meta") or {}).get("attachmentProvenance") or _provenance_from_receipt(store.get_receipt(message["id"]))
            if provenance is not None:
                provenance.setdefault("alignmentSources", (message.get("meta") or {}).get("alignmentSources", []))
                message["provenance"] = provenance
    payload = {"conversation": conversation, "messages": messages}
    draft = store.get_draft(conversation_id)
    if draft is not None:
        payload["decisionDraft"] = draft
    if conversation.get("decisionId"):
        payload["decision"] = _growth_store().get_decision(conversation["decisionId"])
    return payload


def delete_conversation(conversation_id: str, request: Request = None):
    from .chat_imports import require_conversation
    require_conversation(conversation_id, _device_scope_of(request))
    if not _store().delete_conversation(conversation_id):
        raise _error(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    from .stores.memory_store import MemoryStore
    MemoryStore(_ontology_store()).remove_conversation(conversation_id)
    from .stores.routing_store import RoutingStore
    onto = _ontology_store()
    RoutingStore(onto)
    with onto._lock, onto._connect() as db:
        db.execute("DELETE FROM context_lookup_stages WHERE conversation_id=?", (conversation_id,))
    return {"deleted": True, "id": conversation_id}


def get_receipt(conversation_id: str, message_id: str, request: Request = None):
    from .chat_imports import require_conversation
    require_conversation(conversation_id, _device_scope_of(request))
    receipt = _store().get_receipt(message_id)
    if receipt is None or receipt.get("conversationId") != conversation_id:
        raise _error(404, "RECEIPT_NOT_FOUND", "回执不存在")
    return receipt


def _encode(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def post_message(conversation_id: str, req: MessageCreate, request: Request = None):
    from .zhijun.provider import ProviderError
    from .chat_imports import require_conversation
    from .uploads import _device_scope_of
    store = require_conversation(conversation_id, _device_scope_of(request))
    refs = [r.model_dump() for r in req.materialRefs]
    known = {(r["materialId"], r["version"]) for r in store.refs(conversation_id)}
    if any((r["materialId"], r["version"]) not in known for r in refs):
        raise _error(400, "ATTACHMENT_NOT_LINKED", "请先把文件加入当前对话")
    from .stores.routing_store import RoutingStore
    managed = RoutingStore(_ontology_store()).mode(conversation_id)["mode"] != "legacy"
    if req.localOnly and not managed:
        from .stores.alignment_store import AlignmentStore
        from .stores.ontology_store import OntologyStore
        AlignmentStore(OntologyStore.instance()).status(conversation_id, local_only=True, status="paused",
            detail="这段对话选择了仅本地处理；派生内容不自动外发")
    generator = run_turn(conversation_id, req.content, depth=req.depth, mode=req.mode,
                         material_refs=refs, local_only=req.localOnly, route_revision=req.routeRevision,
                         omit_sources=req.omitSources, request_id=req.requestId, retry_user_id=req.retryUserMessageId,
                         reply_assistance=req.replyAssistance, charter_exception_id=req.charterExceptionId)
    try:
        first = next(generator)
    except TurnError as exc:
        raise _error(exc.status_code, exc.code, exc.message) from None
    except ProviderError as exc:
        raise _error(exc.status_code, exc.code, str(exc)) from None
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


def confirm_draft(conversation_id: str, req: DraftConfirm, request: Request = None):
    from .chat_imports import require_conversation
    from .uploads import _device_scope_of
    require_conversation(conversation_id, _device_scope_of(request))
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
    from .zhijun.decision_suggestions import suggest
    from . import learning_routes
    from .zhijun import reply_assistance
    built = APIRouter(prefix=_PREFIX, tags=_TAGS)
    write_dependencies = [Depends(write_guard)] if write_guard is not None else []
    built.add_api_route("", create_conversation, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("", list_conversations, methods=["GET"])
    from . import routing_routes
    built.add_api_route("/routing/default", routing_routes.default_state, methods=["GET"])
    built.add_api_route("/routing/default", routing_routes.set_default, methods=["PUT"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}", get_conversation, methods=["GET"])
    built.add_api_route("/{conversation_id}", update_conversation, methods=["PATCH"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/outcomes", get_outcomes, methods=["GET"])
    built.add_api_route("/{conversation_id}", delete_conversation, methods=["DELETE"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/messages", post_message, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/reply-assistance", reply_assistance.suggest, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/reply-assistance", reply_assistance.latest, methods=["GET"])
    built.add_api_route("/{conversation_id}/routing", routing_routes.state, methods=["GET"])
    built.add_api_route("/{conversation_id}/routing", routing_routes.set_mode, methods=["PUT"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/routing/preview", routing_routes.preview, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/routing/charter-exception", routing_routes.charter_exception, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/routing/grant", routing_routes.grant, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/routing/default-consent", routing_routes.set_default_consent, methods=["PUT"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/routing/handling", routing_routes.set_handling, methods=["PUT"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/routing/revoke", routing_routes.revoke, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/routing/audits", routing_routes.audits, methods=["GET"])
    built.add_api_route("/{conversation_id}/routing/pending/{revision}", routing_routes.pending_preview, methods=["GET"])
    built.add_api_route("/{conversation_id}/routing/resume", routing_routes.resume, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/messages/{message_id}/receipt", get_receipt, methods=["GET"])
    built.add_api_route("/{conversation_id}/decision-draft", get_draft, methods=["GET"])
    built.add_api_route("/{conversation_id}/decision-draft/suggestions", suggest, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/decision-draft/confirm", confirm_draft, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/decision-draft/discard", discard_draft, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/outcome", record_outcome, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/{conversation_id}/learning", learning_routes.state, methods=["GET"])
    for path, endpoint in (("start", learning_routes.start), ("suggest", learning_routes.suggest),
                           ("propose", learning_routes.propose), ("resolve", learning_routes.resolve)):
        built.add_api_route("/{conversation_id}/learning/" + path, endpoint, methods=["POST"], dependencies=write_dependencies)
    return built


router = _build_router()


def configure_write_guard(guard) -> None:
    """为所有写路由注入 server.py 的 loopback + CSRF 防护。"""
    global router
    router = _build_router(guard)
