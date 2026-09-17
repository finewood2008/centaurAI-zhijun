"""Scoped reflection history and explicit user calibration."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .domain_scope import _device_scope_of
from .stores.conversation_store import ConversationStore
from .stores.ontology_store import OntologyStore, OntologyError, OntologyConflictError
from .stores.reflection_store import ReflectionStore
from .zhijun import reflections
from .zhijun.routing import Router


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["accepted", "contextual", "rejected", "observing", "retired"]
    note: str = Field(default="", max_length=1000)
    expectedRevision: int = Field(ge=1)
    requestId: str = Field(min_length=8, max_length=100)


def resources(request):
    onto, convs = OntologyStore.instance(), ConversationStore.instance()
    scope = _device_scope_of(request)
    return ReflectionStore(onto), Router(onto, convs, "scope:" + scope)


def listing(request: Request):
    ledger, router = resources(request)
    items = [reflections.public(item, ledger) for item in ledger.list(router.scope)
             if item["status"] != "retired" and reflections.valid(router, item)]
    return {"items": items}


def review(reflection_id: str, body: Feedback, request: Request):
    ledger, router = resources(request)
    item = ledger.get(reflection_id)
    if not item or item["scope"] != router.scope:
        raise HTTPException(404, "照见不存在")
    if body.action != "retired" and not reflections.valid(router, item):
        raise HTTPException(409, "照见的来源已变化，请刷新后核对")
    try:
        result = ledger.review(reflection_id, router.scope, body.model_dump())
    except OntologyError as exc:
        raise HTTPException(409 if isinstance(exc, OntologyConflictError) else 400, str(exc)) from None
    # Close the shared attention slot, without prompting the next backlog item.
    from .stores.memory_store import MemoryStore
    from .zhijun.memory import topic_for
    cid = item["conversationId"]
    ledger_memory = MemoryStore(router.onto)
    topic = topic_for(router.convs, cid, item["messageId"])
    slot = ledger_memory.slot(cid, topic)
    if slot and slot["kind"] == "reflection" and slot["target_id"] == item["id"]:
        ledger_memory.consume(cid, topic, "reflection", item["id"])
    return reflections.public(result, ledger)


def build_router(write_guard=None):
    router = APIRouter(prefix="/api/mindos/reflections", tags=["reflections"])
    router.add_api_route("", listing, methods=["GET"])
    router.add_api_route("/{reflection_id}/feedback", review, methods=["POST"],
                         dependencies=[Depends(write_guard)] if write_guard else [])
    return router
