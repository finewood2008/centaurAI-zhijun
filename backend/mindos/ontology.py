"""知君本体路由：统计、理解列表 / 详情 / 手写新增、一键复核、待确认 inbox、实体、投影。

契约见 docs/development/zhijun-api-contract.md §3。状态机唯一入口在 OntologyStore.transition。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .stores.ontology_store import (
    ME_ENTITY_ID,
    SECTIONS,
    TRUST_STATES,
    OntologyConflictError,
    OntologyError,
    OntologyNotFoundError,
    OntologyStore,
    utc_now,
)
from .stores.conversation_store import ConversationStore
from .uploads import _device_scope_of
from .zhijun.alignment import visible
from .zhijun import projection
from .zhijun.confirm import review_claim as _review_claim
from .zhijun.jobs import enqueue_projection

_PREFIX = "/api/mindos/ontology"
_TAGS = ["zhijun-ontology"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimCreate(_StrictModel):
    content: str = Field(min_length=1, max_length=120)
    # 省略 section / layer 时由规则分类器判定（用户可在结果卡上改）。
    section: Literal["who", "people", "matters", "principles", "ways", "direction"] | None = None
    layer: Literal["self_declared", "aspirational"] | None = None
    predicate: str | None = Field(default=None, max_length=40)
    objectName: str | None = Field(default=None, max_length=80)
    objectType: Literal["person", "organization", "project", "place", "topic", "event", "term"] = "person"
    privacyLevel: Literal["public", "private", "sensitive", "restricted"] = "private"
    exportAllowed: bool = False


class ReviewRequest(_StrictModel):
    action: Literal["confirm", "partial", "context_only", "reject", "defer", "retract", "reaffirm"]
    editedContent: str | None = Field(default=None, max_length=120)
    contextRef: str | None = Field(default=None, max_length=120)
    note: str = Field(default="", max_length=500)
    surface: Literal["conversation", "ontology_page", "onboarding", "today"] = "ontology_page"
    conversationId: str | None = Field(default=None, max_length=64)
    messageId: str | None = Field(default=None, max_length=64)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "detail": message})


def _map(exc: Exception) -> HTTPException:
    if isinstance(exc, OntologyNotFoundError):
        return _error(404, "NOT_FOUND", str(exc))
    if isinstance(exc, OntologyConflictError):
        return _error(409, "CONFLICT", str(exc))
    if isinstance(exc, OntologyError):
        return _error(400, "BAD_REQUEST", str(exc))
    raise exc


def _store() -> OntologyStore:
    return OntologyStore.instance()


def _scope(request):
    return _device_scope_of(request) if request is not None else "global"


def _global_only(request):
    if _scope(request) != "global":
        raise _error(403, "GLOBAL_OPERATION", "此操作涉及主机全局记忆，请在本机管理端操作")


def _claims(request, *, limit=-1, **kwargs):
    scope, convs = _scope(request), ConversationStore.instance()
    # Scope/lifecycle checks precede the result cap; another device cannot crowd
    # a user's records out of the first page. No model or export grant is involved.
    items = [c for c in _store().list_claims(limit=-1, **kwargs) if visible(c, convs, scope)]
    with _store()._connect() as db:
        owners = {r[0]: r[1] for r in db.execute("SELECT id,device_scope FROM entities")}
    for item in items:
        # Legacy entities were shared. Their names/aliases are not device-local
        # evidence and must not leak through an otherwise local claim.
        for key, name_key in (("subjectEntityId", "subjectName"), ("objectEntityId", "objectName")):
            if item.get(key) != ME_ENTITY_ID and owners.get(item.get(key)) != scope:
                item[name_key] = None
    return items if limit < 0 else items[:limit]


def _require_claim(claim_id, request):
    claim = _store().get_claim(claim_id)
    if claim is None or not visible(claim, ConversationStore.instance(), _scope(request)):
        raise _error(404, "NOT_FOUND", "理解不存在")
    return claim


def _entities(request, claims=None):
    scope = _scope(request)
    claims = claims if claims is not None else _claims(request, trust_states=TRUST_STATES)
    counts = {}
    for c in claims:
        for eid in {c.get("subjectEntityId"), c.get("objectEntityId")} - {None}:
            if c["trustState"] in ("working", "confirmed"):
                counts[eid] = counts.get(eid, 0) + 1
    with _store()._connect() as db:
        owned = {r[0] for r in db.execute("SELECT id FROM entities WHERE device_scope=?", (scope,))}
    foreign_links = set()
    for claim in _store().list_claims(trust_states=TRUST_STATES, limit=-1):
        if not visible(claim, ConversationStore.instance(), scope):
            foreign_links.update({claim.get("subjectEntityId"), claim.get("objectEntityId")} - {None, ME_ENTITY_ID})
    # Non-global names created before scope-aware entity writes are deliberately
    # not relabelled. Their original text is still retained inside the claim.
    return [{**e, "claimCount": counts.get(e["id"], 0)} for e in _store().list_entities(limit=-1)
            if e["id"] == ME_ENTITY_ID or (e["id"] in owned and e["id"] not in foreign_links
                                          and (scope == "global" or e["id"] in counts))]


def _merge_visible(item, request):
    if not item:
        return False
    wanted = {item["fromEntityId"], item["intoEntityId"]}
    scope, convs = _scope(request), ConversationStore.instance()
    with _store()._connect() as db:
        owners = {r[0]: r[1] for r in db.execute("SELECT id,device_scope FROM entities")}
    if any(owners.get(eid) != scope for eid in wanted):
        return False
    return all(visible(c, convs, scope) for c in _store().list_claims(trust_states=TRUST_STATES, limit=-1)
               if wanted.intersection({c.get("subjectEntityId"), c.get("objectEntityId")}))


def _conflicts(request):
    allowed = {c["id"] for c in _claims(request, trust_states=TRUST_STATES)}
    return [item for item in _store().list_conflicts(limit=-1)
            if (item.get("claimA") or {}).get("id") in allowed and (item.get("claimB") or {}).get("id") in allowed]


def get_stats(request: Request = None):
    import hashlib
    claims = _claims(request, trust_states=TRUST_STATES)
    counts = {state: sum(c["trustState"] == state for c in claims) for state in TRUST_STATES}
    section_counts = {s: {state: sum(c["section"] == s and c["trustState"] == state for c in claims)
                          for state in ("working", "confirmed")} for s in SECTIONS}
    revision = hashlib.sha256(repr([(c["id"], c["updatedAt"], c["trustState"]) for c in claims]).encode()).hexdigest()[:12]
    return {"hasOntology": counts["confirmed"] > 0, "claims": counts, "bySection": section_counts,
            "entities": sum(e["id"] != ME_ENTITY_ID for e in _entities(request, claims)),
            "inbox": len(_claims(request, trust_states=("working",), include_hidden=False)),
            "revision": int(revision, 16), "proposals": list_proposals(request)["total"]}


def list_claims(
    request: Request,
    section: str | None = Query(None),
    trust: str = Query("confirmed"),
    limit: int = Query(200, ge=1, le=2000),
):
    states = tuple(s.strip() for s in trust.split(",") if s.strip())
    bad = [s for s in states if s not in TRUST_STATES]
    if bad:
        raise _error(400, "BAD_REQUEST", f"trust 不合法：{','.join(bad)}")
    if section is not None and section not in SECTIONS:
        raise _error(400, "BAD_REQUEST", f"section 不合法：{section}")
    try:
        items = _claims(request, section=section, trust_states=states or ("confirmed",), limit=limit)
    except OntologyError as exc:
        raise _map(exc) from None
    return {"items": items, "total": len(items)}


def get_claim(claim_id: str, request: Request = None):
    claim = _require_claim(claim_id, request)
    result = next((c for c in _claims(request, trust_states=(claim["trustState"],)) if c["id"] == claim_id), None)
    if result is None:
        raise _error(404, "NOT_FOUND", "理解不存在")
    return result


def create_claim(req: ClaimCreate, request: Request = None):
    store = _store()
    object_id = None
    section, layer, predicate = req.section, req.layer, req.predicate
    if section is None or layer is None:
        from .zhijun.provider import _fake_section

        guessed_section, guessed_layer, guessed_predicate = _fake_section(req.content)
        section = section or guessed_section
        layer = layer or ("aspirational" if guessed_layer == "aspirational" else "self_declared")
        predicate = predicate or (guessed_predicate if section == guessed_section else None)
    try:
        if req.objectName:
            object_id = store.upsert_entity(req.objectName, req.objectType, alias_source="user", device_scope=_scope(request))["id"]
        claim = store.create_claim(
            {
                "subject_entity_id": ME_ENTITY_ID,
                "object_entity_id": object_id,
                "predicate": predicate,
                "content": req.content,
                "section": section,
                "layer": layer,
                "confidence": 1.0,
                "privacy_level": req.privacyLevel,
                "export_allowed": req.exportAllowed,
                "device_scope": _scope(request),
            },
            [{"kind": "user_edit", "quote": req.content}],
            trust_state="confirmed",
            trust_origin="user_created",
            surface="ontology_page",
            note="用户手写",
        )
    except OntologyError as exc:
        raise _map(exc) from None
    try:
        enqueue_projection(store=store)
    except Exception:  # noqa: BLE001
        pass
    return claim


def review(claim_id: str, req: ReviewRequest, request: Request = None):
    try:
        with _store()._lock:
            _require_claim(claim_id, request)
            if req.conversationId:
                from .chat_imports import require_conversation
                require_conversation(req.conversationId, _scope(request))
            if req.messageId:
                message = ConversationStore.instance().get_message(req.messageId)
                if not req.conversationId or not message or message["conversationId"] != req.conversationId:
                    raise _error(404, "NOT_FOUND", "来源消息不存在")
            if req.contextRef and req.contextRef.startswith("conv_"):
                from .chat_imports import require_conversation
                require_conversation(req.contextRef, _scope(request))
            result = _review_claim(claim_id, action=req.action, surface=req.surface, edited_content=req.editedContent,
                context_ref=req.contextRef, note=req.note, conversation_id=req.conversationId, message_id=req.messageId)
            return {key: get_claim(value["id"], request) if value else value for key, value in result.items()}
    except OntologyError as exc:
        raise _map(exc) from None


def inbox(request: Request, limit: int = Query(20, ge=1, le=100)):
    items = _claims(request, trust_states=("working",), include_hidden=False, limit=limit)
    return {"items": items, "total": len(items)}


def list_entities(request: Request, entity_type: str | None = Query(None, alias="type")):
    items = [e for e in _entities(request) if not entity_type or e["type"] == entity_type]
    return {"items": items, "total": len(items)}


def get_projection(request: Request = None):
    markdown, exportable = projection.render(_store(), scope=_scope(request))
    return {"markdown": markdown, "exportableMarkdown": exportable, "generatedAt": utc_now()}


# ---------------------------------------------------------------- P3：裁决 / 整合 / 导出 / 全量删除
class MergeResolve(_StrictModel):
    accept: bool


class ConflictResolve(_StrictModel):
    keep: Literal["a", "b", "both"]


class ExportToggle(_StrictModel):
    allowed: bool


def set_export(claim_id: str, req: ExportToggle, request: Request = None):
    try:
        with _store()._lock:
            _require_claim(claim_id, request)
            claim = _store().set_export_allowed(claim_id, req.allowed)
    except OntologyError as exc:
        raise _map(exc) from None
    try:
        enqueue_projection(store=_store())
    except Exception:  # noqa: BLE001
        pass
    return get_claim(claim["id"], request)


def context_pack_status(request: Request = None):
    _global_only(request)
    from .zhijun import context_pack

    store = _store()
    exportable = context_pack.exportable_claims(store, limit=context_pack.HARD_MAX_CLAIMS)
    return {"exportable": len(exportable), "receipts": context_pack.receipt_summary(store), "items": exportable[:200]}


class PurgeRequest(_StrictModel):
    confirm: str = Field(min_length=1, max_length=40)
    includeConversations: bool = True


PURGE_PHRASE = "删除全部记忆"


def list_proposals(request: Request = None):
    store = _store()
    # Entity merging is a global legacy operation. Device users can resolve only
    # conflicts where both complete claim sources belong to their own scope.
    merges = [item for item in store.list_merge_proposals(limit=-1) if _merge_visible(item, request)] if _scope(request) == "global" else []
    conflicts = _conflicts(request)
    return {"merges": merges, "conflicts": conflicts, "total": len(merges) + len(conflicts)}


def resolve_merge(proposal_id: str, req: MergeResolve, request: Request = None):
    _global_only(request)
    try:
        with _store()._lock:
            if not _merge_visible(_store().get_merge_proposal(proposal_id), request):
                raise _error(404, "NOT_FOUND", "合并候选不存在")
            result = _store().resolve_merge_proposal(proposal_id, accept=req.accept)
    except OntologyError as exc:
        raise _map(exc) from None
    try:
        enqueue_projection(store=_store())
    except Exception:  # noqa: BLE001
        pass
    return result


def resolve_conflict(conflict_id: str, req: ConflictResolve, request: Request = None):
    try:
        with _store()._lock:
            if conflict_id not in {item["id"] for item in _conflicts(request)}:
                existing = _store().get_conflict(conflict_id)
                if existing and all((existing.get(k) or {}).get("id") in {c["id"] for c in _claims(request, trust_states=TRUST_STATES)} for k in ("claimA", "claimB")):
                    raise OntologyConflictError("矛盾对已处理")
                raise _error(404, "NOT_FOUND", "矛盾对不存在")
            result = _store().resolve_conflict(conflict_id, keep=req.keep)
    except OntologyError as exc:
        raise _map(exc) from None
    try:
        enqueue_projection(store=_store())
    except Exception:  # noqa: BLE001
        pass
    return result


def consolidate_now(request: Request = None):
    _global_only(request)
    from .zhijun import consolidate
    from .zhijun.provider import ProviderError, build_provider

    try:
        provider = build_provider()
    except ProviderError:
        provider = None
    return consolidate.run(store=_store(), provider=provider)


def export_ontology(request: Request, sections: str | None = Query(None), includeWorking: bool = Query(False)):
    wanted = tuple(s.strip() for s in (sections or "").split(",") if s.strip())
    bad = [s for s in wanted if s not in SECTIONS]
    if bad:
        raise _error(400, "BAD_REQUEST", f"section 不合法：{','.join(bad)}")
    claims = _claims(request, trust_states=("confirmed", "working") if includeWorking else ("confirmed",))
    claims = [c for c in claims if not wanted or c["section"] in wanted]
    allowed = {c["id"] for c in claims}
    events = [e for e in _store().review_events(limit=-1) if e["targetType"] == "claim" and e["targetId"] in allowed]
    return {"exportedAt": utc_now(), "schemaVersion": _store().meta_get("schema_version", "1"),
            "entities": _entities(request, claims), "claims": claims, "reviewEvents": events}


def purge_all(req: PurgeRequest, request: Request = None):
    _global_only(request)
    if req.confirm.strip() != PURGE_PHRASE:
        raise _error(400, "CONFIRM_MISMATCH", f"请输入「{PURGE_PHRASE}」以确认")
    result = {"ontology": _store().purge_all()}
    if req.includeConversations:
        from .stores.conversation_store import ConversationStore

        result["conversations"] = ConversationStore.instance().purge_all()
    try:
        from .zhijun import projection as projection_module

        projection_module.write_projection(_store())
    except Exception:  # noqa: BLE001
        pass
    return result


def _build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=_TAGS)
    write_dependencies = [Depends(write_guard)] if write_guard is not None else []
    from .alignment_routes import register
    register(built, write_dependencies)
    built.add_api_route("/proposals", list_proposals, methods=["GET"])
    built.add_api_route("/proposals/merges/{proposal_id}/resolve", resolve_merge, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/proposals/conflicts/{conflict_id}/resolve", resolve_conflict, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/consolidate", consolidate_now, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/export", export_ontology, methods=["GET"])
    built.add_api_route("/context-pack", context_pack_status, methods=["GET"])
    built.add_api_route("/claims/{claim_id}/export", set_export, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/purge", purge_all, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/stats", get_stats, methods=["GET"])
    built.add_api_route("/claims", list_claims, methods=["GET"])
    built.add_api_route("/claims", create_claim, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/claims/{claim_id}", get_claim, methods=["GET"])
    built.add_api_route("/claims/{claim_id}/review", review, methods=["POST"], dependencies=write_dependencies)
    built.add_api_route("/inbox", inbox, methods=["GET"])
    built.add_api_route("/entities", list_entities, methods=["GET"])
    built.add_api_route("/projection", get_projection, methods=["GET"])
    return built


router = _build_router()


def configure_write_guard(guard) -> None:
    global router
    router = _build_router(guard)
