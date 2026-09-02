"""知君本体路由：统计、理解列表 / 详情 / 手写新增、一键复核、待确认 inbox、实体、投影。

契约见 docs/development/zhijun-api-contract.md §3。状态机唯一入口在 OntologyStore.transition。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .stores.ontology_store import (
    ME_ENTITY_ID,
    SECTIONS,
    TRUST_STATES,
    OntologyConflictError,
    OntologyError,
    OntologyNotFoundError,
    OntologyStore,
)
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


def get_stats():
    stats = _store().stats()
    stats["proposals"] = len(_store().list_merge_proposals()) + len(_store().list_conflicts())
    return stats


def list_claims(
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
        items = _store().list_claims(section=section, trust_states=states or ("confirmed",), limit=limit)
    except OntologyError as exc:
        raise _map(exc) from None
    return {"items": items, "total": len(items)}


def get_claim(claim_id: str):
    claim = _store().get_claim(claim_id)
    if claim is None:
        raise _error(404, "NOT_FOUND", "理解不存在")
    return claim


def create_claim(req: ClaimCreate):
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
            object_id = store.upsert_entity(req.objectName, req.objectType, alias_source="user")["id"]
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


def review(claim_id: str, req: ReviewRequest):
    try:
        return _review_claim(
            claim_id,
            action=req.action,
            surface=req.surface,
            edited_content=req.editedContent,
            context_ref=req.contextRef,
            note=req.note,
            conversation_id=req.conversationId,
            message_id=req.messageId,
        )
    except OntologyError as exc:
        raise _map(exc) from None


def inbox(limit: int = Query(20, ge=1, le=100)):
    items = _store().inbox(limit=limit)
    return {"items": items, "total": len(items)}


def list_entities(entity_type: str | None = Query(None, alias="type")):
    items = _store().list_entities(entity_type)
    return {"items": items, "total": len(items)}


def get_projection():
    return projection.projection_payload(_store())


# ---------------------------------------------------------------- P3：裁决 / 整合 / 导出 / 全量删除
class MergeResolve(_StrictModel):
    accept: bool


class ConflictResolve(_StrictModel):
    keep: Literal["a", "b", "both"]


class ExportToggle(_StrictModel):
    allowed: bool


def set_export(claim_id: str, req: ExportToggle):
    try:
        claim = _store().set_export_allowed(claim_id, req.allowed)
    except OntologyError as exc:
        raise _map(exc) from None
    try:
        enqueue_projection(store=_store())
    except Exception:  # noqa: BLE001
        pass
    return claim


def context_pack_status():
    from .zhijun import context_pack

    store = _store()
    exportable = context_pack.exportable_claims(store, limit=context_pack.HARD_MAX_CLAIMS)
    return {"exportable": len(exportable), "receipts": context_pack.receipt_summary(store), "items": exportable[:200]}


class PurgeRequest(_StrictModel):
    confirm: str = Field(min_length=1, max_length=40)
    includeConversations: bool = True


PURGE_PHRASE = "删除全部记忆"


def list_proposals():
    store = _store()
    merges = store.list_merge_proposals()
    conflicts = store.list_conflicts()
    return {"merges": merges, "conflicts": conflicts, "total": len(merges) + len(conflicts)}


def resolve_merge(proposal_id: str, req: MergeResolve):
    try:
        result = _store().resolve_merge_proposal(proposal_id, accept=req.accept)
    except OntologyError as exc:
        raise _map(exc) from None
    try:
        enqueue_projection(store=_store())
    except Exception:  # noqa: BLE001
        pass
    return result


def resolve_conflict(conflict_id: str, req: ConflictResolve):
    try:
        result = _store().resolve_conflict(conflict_id, keep=req.keep)
    except OntologyError as exc:
        raise _map(exc) from None
    try:
        enqueue_projection(store=_store())
    except Exception:  # noqa: BLE001
        pass
    return result


def consolidate_now():
    from .zhijun import consolidate
    from .zhijun.provider import ProviderError, build_provider

    try:
        provider = build_provider()
    except ProviderError:
        provider = None
    return consolidate.run(store=_store(), provider=provider)


def export_ontology(sections: str | None = Query(None), includeWorking: bool = Query(False)):
    wanted = tuple(s.strip() for s in (sections or "").split(",") if s.strip())
    bad = [s for s in wanted if s not in SECTIONS]
    if bad:
        raise _error(400, "BAD_REQUEST", f"section 不合法：{','.join(bad)}")
    return _store().export_payload(sections=wanted or None, include_working=includeWorking)


def purge_all(req: PurgeRequest):
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
