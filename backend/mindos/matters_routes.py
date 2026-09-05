"""Explicit, device-scoped matter and work-product editing; never calls a model."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .chat_imports import require_conversation
from .stores.conversation_store import ConversationStore
from .stores.matters_store import MattersStore
from .stores.ontology_store import OntologyStore, OntologyConflictError
from .uploads import _device_scope_of


Status = Literal["active", "paused", "completed"]
ArtifactKind = Literal["communication", "decision_memo", "meeting_prep", "action_summary", "freeform"]


class Write(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class MatterCreate(Write):
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(default="", max_length=2000)
    context: str = Field(default="", max_length=6000)
    nextStep: str = Field(default="", max_length=2000)
    conversationId: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("title", "goal", "context", "nextStep", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class MatterEdit(Write):
    expectedRevision: int = Field(ge=1, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    goal: str | None = Field(default=None, max_length=2000)
    context: str | None = Field(default=None, max_length=6000)
    nextStep: str | None = Field(default=None, max_length=2000)
    outcome: str | None = Field(default=None, max_length=6000)
    status: Status | None = None
    decisionId: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("title", "goal", "context", "nextStep", "outcome", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_changes(self):
        fields = self.model_fields_set - {"requestId", "expectedRevision"}
        if not fields or any(getattr(self, key) is None for key in fields - {"decisionId"}):
            raise ValueError("请提供要保存的内容")
        return self


class Bind(Write):
    expectedRevision: int = Field(ge=0, strict=True)
    matterId: str | None = Field(min_length=1, max_length=100)


class ArtifactCreate(Write):
    conversationId: str = Field(min_length=1, max_length=100)
    messageId: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=120)
    kind: ArtifactKind = "freeform"


class ArtifactEdit(Write):
    expectedRevision: int = Field(ge=1, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    markdown: str | None = Field(default=None, min_length=1, max_length=50000)
    kind: ArtifactKind | None = None

    @field_validator("title", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("markdown")
    @classmethod
    def nonempty_markdown(cls, value):
        if value is not None and not value.strip():
            raise ValueError("正文不能只有空白")
        return value

    @model_validator(mode="after")
    def require_changes(self):
        fields = self.model_fields_set - {"requestId", "expectedRevision"}
        if not fields or any(getattr(self, key) is None for key in fields):
            raise ValueError("请提供要保存的正文或名称")
        return self


def resources(request):
    return MattersStore(OntologyStore.instance(), ConversationStore.instance()), _device_scope_of(request)


def require_matter(store, ident, scope):
    item = store.get(ident, scope)
    if not item:
        raise HTTPException(404, {"code": "MATTER_NOT_FOUND", "detail": "这件事不存在或不属于当前设备"})
    return item


def require_artifact(store, ident, scope):
    item = store.artifact(ident, scope)
    if not item:
        raise HTTPException(404, {"code": "ARTIFACT_NOT_FOUND", "detail": "这份成果不存在或不属于当前设备"})
    require_matter(store, item["matterId"], scope)
    return item


def apply(operation):
    try:
        return operation()
    except OntologyConflictError as exc:
        raise HTTPException(409, {"code": "WORK_REVISION_CONFLICT", "detail": str(exc)}) from None


def list_matters(request: Request, status: Literal["active", "paused", "completed", "all"] = "active"):
    store, scope = resources(request)
    items = store.list(scope, status)
    return {"items": items, "total": len(items)}


def get_matter(matter_id: str, request: Request):
    store, scope = resources(request)
    return require_matter(store, matter_id, scope)


def create_matter(body: MatterCreate, request: Request):
    store, scope = resources(request)
    if body.conversationId:
        require_conversation(body.conversationId, scope)
    return apply(lambda: store.create(scope, body.model_dump(exclude={"requestId", "conversationId"}), body.requestId, body.conversationId))


def edit_matter(matter_id: str, body: MatterEdit, request: Request):
    store, scope = resources(request)
    require_matter(store, matter_id, scope)
    if body.decisionId:
        from .stores.growth_store import GrowthStore
        from .zhijun.charter_policy import record_in_scope
        decision = GrowthStore.instance().get_decision(body.decisionId)
        if not decision or not record_in_scope(decision, store.conversations, scope):
            raise HTTPException(404, "关联判断不存在或不属于当前设备")
    changes = body.model_dump(exclude={"requestId", "expectedRevision"}, exclude_unset=True)
    return apply(lambda: store.update(matter_id, scope, changes, body.expectedRevision, body.requestId))


def get_binding(conversation_id: str, request: Request):
    store, scope = resources(request)
    require_conversation(conversation_id, scope)
    return store.binding(conversation_id, scope)


def bind_matter(conversation_id: str, body: Bind, request: Request):
    store, scope = resources(request)
    require_conversation(conversation_id, scope)
    if body.matterId:
        require_matter(store, body.matterId, scope)
    return apply(lambda: store.bind(conversation_id, scope, body.matterId, body.expectedRevision, body.requestId))


def list_artifacts(matter_id: str, request: Request):
    store, scope = resources(request)
    require_matter(store, matter_id, scope)
    items = store.artifacts(matter_id, scope)
    return {"items": items, "total": len(items)}


def get_artifact(artifact_id: str, request: Request):
    store, scope = resources(request)
    return require_artifact(store, artifact_id, scope)


def create_artifact(matter_id: str, body: ArtifactCreate, request: Request):
    store, scope = resources(request)
    matter = require_matter(store, matter_id, scope)
    require_conversation(body.conversationId, scope)
    message = store.conversations.get_message(body.messageId)
    if not message or message["conversationId"] != body.conversationId or message["role"] != "assistant" or message["status"] != "complete":
        raise HTTPException(409, "请选择这段对话里一条已经完成的知君回复")
    from .zhijun.context_lookup import strip_citation_markers
    from .zhijun.routing import Router
    from .zhijun.context_sources import message_ref
    router = Router(store.ontology, store.conversations, body.conversationId)
    # Capture the exact original message revision even if its older ancestry is
    # opaque. Local saving is allowed; Router will still block any unsafe reuse.
    source = message_ref(router, message)
    markdown = strip_citation_markers(message["content"])
    if not markdown.strip() or len(markdown) > 50000:
        raise HTTPException(409, "这条回复为空或过长，暂不能保存为一份成果")
    payload = {"title": body.title.strip() or matter["title"], "kind": body.kind, "markdown": markdown}
    return apply(lambda: store.save_artifact(matter_id, scope, payload, message, source, body.requestId))


def edit_artifact(artifact_id: str, body: ArtifactEdit, request: Request):
    store, scope = resources(request)
    require_artifact(store, artifact_id, scope)
    changes = body.model_dump(exclude={"requestId", "expectedRevision"}, exclude_unset=True)
    return apply(lambda: store.edit_artifact(artifact_id, scope, changes, body.expectedRevision, body.requestId))


def matter_history(matter_id: str, request: Request):
    store, scope = resources(request)
    require_matter(store, matter_id, scope)
    return {"items": store.history("matter", matter_id, scope)}


def artifact_history(artifact_id: str, request: Request):
    store, scope = resources(request)
    require_artifact(store, artifact_id, scope)
    return {"items": store.history("artifact", artifact_id, scope)}


def build_router(write_guard=None):
    router = APIRouter(prefix="/api/mindos", tags=["ongoing-matters"])
    guard = [Depends(write_guard)] if write_guard else []
    for path, handler in (("/matters", list_matters), ("/matters/{matter_id}", get_matter),
                          ("/matters/{matter_id}/history", matter_history), ("/matters/{matter_id}/artifacts", list_artifacts),
                          ("/artifacts/{artifact_id}", get_artifact), ("/artifacts/{artifact_id}/history", artifact_history),
                          ("/conversations/{conversation_id}/matter", get_binding)):
        router.add_api_route(path, handler, methods=["GET"])
    for path, handler, verb in (("/matters", create_matter, "POST"), ("/matters/{matter_id}", edit_matter, "PATCH"),
                                ("/matters/{matter_id}/artifacts", create_artifact, "POST"), ("/artifacts/{artifact_id}", edit_artifact, "PATCH"),
                                ("/conversations/{conversation_id}/matter", bind_matter, "PUT")):
        router.add_api_route(path, handler, methods=[verb], dependencies=guard)
    return router
