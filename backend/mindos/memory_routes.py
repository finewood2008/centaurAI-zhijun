"""Device-scoped memory preferences and opt-in event outlines."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .stores.conversation_store import ConversationStore
from .stores.memory_store import MemoryStore
from .stores.ontology_store import OntologyStore, OntologyConflictError, OntologyError
from .uploads import _device_scope_of
from .zhijun import memory


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyInput(Strict):
    mode: Literal["important", "manual"]
    expectedRevision: int = Field(ge=0)


class DismissInput(Strict):
    topicId: str = Field(min_length=1, max_length=100)
    kind: Literal["claim", "alignment"]
    id: str = Field(min_length=1, max_length=100)
    discard: bool = False


class DraftReviewInput(Strict):
    draftId: str = Field(min_length=1, max_length=100)
    expectedRevision: int = Field(ge=1)
    action: Literal["save", "dismiss"]


class PendingDismissInput(Strict):
    claimId: str = Field(min_length=1, max_length=100)


def _error(exc):
    raise HTTPException(409 if isinstance(exc, OntologyConflictError) else 400,
                        {"code": "MEMORY_CONFLICT" if isinstance(exc, OntologyConflictError) else "MEMORY_INVALID",
                         "detail": str(exc)}) from None


def policy(request: Request):
    return MemoryStore(OntologyStore.instance()).policy(_device_scope_of(request))


def set_policy(body: PolicyInput, request: Request):
    try:
        return MemoryStore(OntologyStore.instance()).set_policy(_device_scope_of(request), body.mode, body.expectedRevision)
    except (OntologyError, ValueError) as exc:
        _error(exc)


def _require(cid, request):
    from .chat_imports import require_conversation
    require_conversation(cid, _device_scope_of(request))
    return OntologyStore.instance(), ConversationStore.instance()


def attention(conversation_id: str, request: Request):
    ontology, convs = _require(conversation_id, request)
    return memory.attention(ontology, convs, conversation_id)


def pending(conversation_id: str, request: Request):
    ontology, convs = _require(conversation_id, request)
    return memory.pending(ontology, convs, conversation_id)


def pending_dismiss(conversation_id: str, body: PendingDismissInput, request: Request):
    ontology, convs = _require(conversation_id, request)
    try:
        with ontology._lock:
            ledger = MemoryStore(ontology)
            entry = next((a for a in ledger.admissions(conversation_id) if a["claim_id"] == body.claimId), None)
            claim = ontology.get_claim(body.claimId)
            from .zhijun.alignment import scope_for, visible
            if (not entry or not claim or not visible(claim, convs, scope_for(conversation_id, convs))
                    or claim["trustState"] not in ("working", "retracted")):
                raise OntologyConflictError("记录状态已变化，未修改已确认的理解")
            if claim["trustState"] == "working":
                ontology.transition(body.claimId, "reject", surface="conversation", conversation_id=conversation_id,
                    note="用户主动撤下待核对候选；原对话保留，不代表否认原话事实")
            slot = ledger.slot(conversation_id, entry["topic_id"])
            if slot and slot["kind"] == "claim" and slot["target_id"] == body.claimId:
                ledger.consume(conversation_id, entry["topic_id"], "claim", body.claimId)
        return {"dismissed": True}
    except OntologyError as exc:
        _error(exc)


def dismiss(conversation_id: str, body: DismissInput, request: Request):
    ontology, _ = _require(conversation_id, request)
    try:
        ledger = MemoryStore(ontology)
        with ontology._lock:
            slot = ledger.slot(conversation_id, body.topicId)
            if not slot or slot["kind"] != body.kind or slot["target_id"] != body.id:
                raise OntologyConflictError("这条提醒已变化，请重新查看")
            if body.discard:
                admitted = any(a["claim_id"] == body.id for a in ledger.admissions(conversation_id, body.topicId))
                claim = ontology.get_claim(body.id)
                if body.kind != "claim" or not admitted or not claim or claim["trustState"] not in ("working", "retracted"):
                    raise OntologyConflictError("记录状态已变化，未修改已确认的理解")
                if claim["trustState"] == "working":
                    ontology.transition(body.id, "reject", surface="conversation", conversation_id=conversation_id,
                        note="用户选择不用记住：撤下候选，不代表否认原话事实；原对话保留")
            ledger.consume(conversation_id, body.topicId, body.kind, body.id)
        return {"dismissed": True}
    except OntologyError as exc:
        _error(exc)


def review_draft(conversation_id: str, body: DraftReviewInput, request: Request):
    ontology, convs = _require(conversation_id, request)
    try:
        return memory.review_draft(ontology, convs, conversation_id, body.draftId, body.expectedRevision, body.action)
    except OntologyError as exc:
        _error(exc)


def build_router(write_guard=None):
    router = APIRouter(prefix="/api/mindos", tags=["memory-policy"])
    guard = [Depends(write_guard)] if write_guard else []
    router.add_api_route("/memory-policy", policy, methods=["GET"])
    router.add_api_route("/memory-policy", set_policy, methods=["PUT"], dependencies=guard)
    base = "/conversations/{conversation_id}/memory"
    router.add_api_route(base + "/pending", pending, methods=["GET"])
    for suffix, handler in (("/attention", attention), ("/dismiss", dismiss), ("/draft-review", review_draft),
                            ("/pending-dismiss", pending_dismiss)):
        router.add_api_route(base + suffix, handler, methods=["POST"], dependencies=guard)
    return router
