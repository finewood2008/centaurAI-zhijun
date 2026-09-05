"""知君成长闭环 MVP API。

人生章程、判断、结果和复盘均为用户显式提交的应用状态。本模块不会把这些记录
自动提升为个人本体 Claim，也不会校验或展开 EvidenceRef，避免形成第二事实源。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .stores.growth_store import (
    DECISION_STATUSES,
    GrowthConflictError,
    GrowthStore,
)


_PREFIX = "/api/mindos/growth"
_TAGS = ["mindos-growth"]


def _clean_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label}不能为空")
    return cleaned


def _clean_optional_text(value: str) -> str:
    return value.strip()


def _clean_string_list(values: list[str], label: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw).strip()
        if not item:
            raise ValueError(f"{label}不能包含空项")
        if len(item) > 500:
            raise ValueError(f"{label}单项不能超过 500 字符")
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _clean_evidence_refs(values: list[str]) -> list[str]:
    """Allow bounded provenance receipts without truncating their permission chain."""
    kinds = {"material", "claim", "message", "draft", "charter", "charter_draft", "charter_document",
             "charter_clause", "charter_workspace", "reply_assist", "decision", "episode"}
    helper_keys = {"kind", "routingSources", "conversationId", "task", "revision", "charterBasis",
                   "possibleAssistance", "draftId", "sourceRevisions"}
    result, seen, total_bytes, total_refs = [], set(), 0, 0
    for raw in values:
        item = raw.strip()
        if not item:
            raise ValueError("evidenceRefs 不能包含空项")
        if item in seen:
            continue
        size = len(item.encode("utf-8"))
        if size > 128 * 1024:
            raise ValueError("结构化来源单项不能超过 128 KiB；请保留完整来源而非截断")
        try:
            data = json.loads(item) if item.startswith("{") else None
        except (ValueError, RecursionError):
            data = None
        structured = isinstance(data, dict) and data.get("kind") in ("routing", "helper_lineage")
        if not structured:
            if len(item) > 500:
                raise ValueError("普通 evidenceRefs 单项不能超过 500 字符")
        else:
            allowed = {"kind", "routingSources"} if data["kind"] == "routing" else helper_keys
            refs = data.get("routingSources")
            if set(data) - allowed or not isinstance(refs, list) or len(refs) > 1024:
                raise ValueError("结构化来源格式不正确，最多保留 1024 个来源引用")
            for ref in refs:
                if not isinstance(ref, dict) or set(ref) - {"kind", "id", "version", "materialVersion"} or ref.get("kind") not in kinds:
                    raise ValueError("来源引用格式或类型不正确")
                if not isinstance(ref.get("id"), str) or not ref["id"] or len(ref["id"]) > 256:
                    raise ValueError("来源标识不正确")
                if "version" in ref and (not isinstance(ref["version"], str) or not ref["version"] or len(ref["version"]) > 128):
                    raise ValueError("来源版本不正确")
                if "materialVersion" in ref and (type(ref["materialVersion"]) is not int or ref["materialVersion"] < 1):
                    raise ValueError("资料版本不正确")
            for key in ("conversationId", "task", "draftId"):
                if key in data and (not isinstance(data[key], str) or not data[key] or len(data[key]) > 256):
                    raise ValueError("辅助来源标识不正确")
            if data.get("revision") is not None and (type(data["revision"]) is not int or data["revision"] < 0):
                raise ValueError("辅助来源修订不正确")
            if "possibleAssistance" in data and type(data["possibleAssistance"]) is not bool:
                raise ValueError("辅助来源标记不正确")
            if data.get("charterBasis") is not None:
                if not isinstance(data["charterBasis"], dict):
                    raise ValueError("章程依据格式不正确")
                DecisionCreate._charter_basis(data["charterBasis"])
            revisions = data.get("sourceRevisions", [])
            if not isinstance(revisions, list) or len(revisions) > 1024 or any(type(r) is not int or r < 0 for r in revisions):
                raise ValueError("辅助来源修订列表不正确")
            total_refs += len(refs)
        total_bytes += size
        if total_bytes > 256 * 1024 or total_refs > 1024:
            raise ValueError("来源总量超出核对范围；请保留完整来源，不要删减或截断")
        seen.add(item)
        result.append(item)
    return result


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CharterCreate(_StrictModel):
    vision: str = Field(default="", max_length=2000)
    roles: list[str] = Field(default_factory=list, max_length=30)
    principles: list[str] = Field(default_factory=list, max_length=50)
    boundaries: list[str] = Field(default_factory=list, max_length=50)
    goals: list[str] = Field(default_factory=list, max_length=50)
    challengeStyle: str = Field(default="", max_length=1000)
    quietDomains: list[str] = Field(default_factory=list, max_length=50)
    expectedVersion: int | None = Field(default=None, ge=0)
    requestId: str | None = Field(default=None, min_length=8, max_length=100)

    @field_validator("vision")
    @classmethod
    def _vision(cls, value: str) -> str:
        return value.strip()

    @field_validator("challengeStyle")
    @classmethod
    def _challenge_style(cls, value: str) -> str:
        return value.strip()

    @field_validator(
        "roles", "principles", "boundaries", "goals", "quietDomains"
    )
    @classmethod
    def _charter_lists(cls, value: list[str], info) -> list[str]:
        return _clean_string_list(value, info.field_name)


class DecisionCreate(_StrictModel):
    title: str = Field(min_length=1, max_length=300)
    context: str = Field(min_length=1, max_length=10000)
    options: list[str] = Field(min_length=1, max_length=30)
    choice: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=10000)
    confidence: int = Field(ge=0, le=100)
    expectedOutcome: str = Field(min_length=1, max_length=5000)
    reviewAt: datetime | None
    relatedEntityIds: list[str] = Field(max_length=100)
    evidenceRefs: list[str] = Field(max_length=100)
    charterBasis: dict | None = None

    @field_validator("charterBasis")
    @classmethod
    def _charter_basis(cls, value):
        if value is None:
            return value
        if set(value) - {"charterId", "version", "scope", "clauseIds"}:
            raise ValueError("章程依据包含未知字段")
        if "charterId" not in value or type(value.get("version")) is not int or value["version"] < 0:
            raise ValueError("章程依据需要明确的标识和版本")
        if value["charterId"] is not None and (not isinstance(value["charterId"], str) or not value["charterId"] or len(value["charterId"]) > 100):
            raise ValueError("章程标识不正确")
        if "scope" in value and (not isinstance(value["scope"], str) or len(value["scope"]) > 200):
            raise ValueError("设备范围不正确")
        ids = value.get("clauseIds", [])
        if not isinstance(ids, list) or len(ids) > 80 or any(not isinstance(i, str) or len(i) > 100 for i in ids):
            raise ValueError("章程条款引用不正确")
        return value

    @field_validator("title", "context", "choice", "rationale", "expectedOutcome")
    @classmethod
    def _required_text(cls, value: str, info) -> str:
        return _clean_text(value, info.field_name)

    @field_validator("options", "relatedEntityIds", "evidenceRefs")
    @classmethod
    def _decision_lists(cls, value: list[str], info) -> list[str]:
        if info.field_name == "evidenceRefs":
            return _clean_evidence_refs(value)
        return _clean_string_list(value, info.field_name)

    @field_validator("reviewAt")
    @classmethod
    def _aware_review_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewAt 必须包含时区")
        return value.astimezone(timezone.utc)

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_is_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("confidence 必须是 0 到 100 的数字")
        return value


class OutcomeCreate(_StrictModel):
    result: str = Field(min_length=1, max_length=10000)
    notes: str = Field(max_length=10000)
    evidenceRefs: list[str] = Field(max_length=100)

    @field_validator("result")
    @classmethod
    def _result(cls, value: str) -> str:
        return _clean_text(value, "结果")

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str) -> str:
        return _clean_optional_text(value)

    @field_validator("evidenceRefs")
    @classmethod
    def _evidence_refs(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value, "evidenceRefs")


class ReviewCreate(_StrictModel):
    decisionId: str = Field(min_length=1, max_length=100)
    reflection: str = Field(min_length=1, max_length=10000)
    lessons: list[str] = Field(min_length=1, max_length=50)
    nextAction: str = Field(min_length=1, max_length=5000)

    @field_validator("decisionId", "reflection", "nextAction")
    @classmethod
    def _review_text(cls, value: str, info) -> str:
        return _clean_text(value, info.field_name)

    @field_validator("lessons")
    @classmethod
    def _lessons(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value, "lessons")


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_charter(request: Request = None):
    from .uploads import _device_scope_of
    from .stores.charter_draft_store import CharterDraftStore
    scope = _device_scope_of(request)
    history = GrowthStore.instance().charter_history(scope)
    return {**history, "workspace": CharterDraftStore().latest_workspace(scope)}


def create_charter(req: CharterCreate, request: Request = None):
    try:
        from .uploads import _device_scope_of
        current = GrowthStore.instance().current_charter(_device_scope_of(request))
        from .stores.charter_draft_store import FIELDS
        payload = req.model_dump()
        prior = (current or {}).get("metadata", {}).get("fields", {})
        payload["metadata"] = {"scope": _device_scope_of(request), "origin": "manual", "fields": {
            f: {"state": "confirmed" if payload.get(f) else "pending", "sources": prior.get(f, {}).get("sources", [])} for f in FIELDS}}
        return GrowthStore.instance().create_charter(payload)
    except GrowthConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def list_decisions(status: str = ""):
    normalized = status.strip()
    if normalized and normalized not in DECISION_STATUSES:
        raise HTTPException(
            400,
            "status 只能是 open、outcome_recorded 或 reviewed",
        )
    items = GrowthStore.instance().list_decisions(normalized or None)
    return {"items": items, "total": len(items)}


_UNSPECIFIED_CHARTER = object()


def create_decision(req: DecisionCreate, *, charter_basis=_UNSPECIFIED_CHARTER, scope="global"):
    payload = req.model_dump()
    payload["reviewAt"] = _utc_iso(req.reviewAt)
    payload["scope"] = scope
    if charter_basis is not _UNSPECIFIED_CHARTER:
        payload["charterBasis"] = charter_basis
    elif "charterBasis" not in req.model_fields_set:
        payload.pop("charterBasis", None)
    try:
        return GrowthStore.instance().create_decision(payload)
    except GrowthConflictError as exc:
        raise HTTPException(409, str(exc)) from None


def create_decision_endpoint(req: DecisionCreate, request: Request):
    from .uploads import _device_scope_of
    return create_decision(req, scope=_device_scope_of(request))


def record_outcome(decision_id: str, req: OutcomeCreate):
    try:
        decision = GrowthStore.instance().record_outcome(
            decision_id, req.model_dump()
        )
    except GrowthConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    if decision is None:
        raise HTTPException(404, "判断不存在")
    return decision


def create_review(req: ReviewCreate):
    try:
        result = GrowthStore.instance().create_review(req.model_dump())
    except GrowthConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    if result is None:
        raise HTTPException(404, "判断不存在")
    # 知君：复盘经验 → 原则候选（单向、失败不影响复盘本身）。
    try:
        from .zhijun.growth_hooks import on_review

        on_review(result.get("review") or {}, result.get("decision"))
    except Exception:  # noqa: BLE001
        pass
    return result


def get_today():
    return GrowthStore.instance().today()


def _build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=_TAGS)
    write_dependencies = [Depends(write_guard)] if write_guard is not None else []

    built.add_api_route("/charter", get_charter, methods=["GET"])
    built.add_api_route(
        "/charter",
        create_charter,
        methods=["POST"],
        dependencies=write_dependencies,
    )
    built.add_api_route("/decisions", list_decisions, methods=["GET"])
    built.add_api_route(
        "/decisions",
        create_decision_endpoint,
        methods=["POST"],
        dependencies=write_dependencies,
    )
    built.add_api_route(
        "/decisions/{decision_id}/outcome",
        record_outcome,
        methods=["POST"],
        dependencies=write_dependencies,
    )
    built.add_api_route(
        "/reviews",
        create_review,
        methods=["POST"],
        dependencies=write_dependencies,
    )
    built.add_api_route("/today", get_today, methods=["GET"])
    return built


router = _build_router()


def configure_write_guard(guard) -> None:
    """为所有写路由注入 server.py 的 loopback + CSRF 防护。"""
    global router
    router = _build_router(guard)
