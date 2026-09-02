"""知君成长闭环 MVP API。

人生章程、判断、结果和复盘均为用户显式提交的应用状态。本模块不会把这些记录
自动提升为个人本体 Claim，也不会校验或展开 EvidenceRef，避免形成第二事实源。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
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


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CharterCreate(_StrictModel):
    vision: str = Field(min_length=1, max_length=2000)
    roles: list[str] = Field(max_length=30)
    principles: list[str] = Field(max_length=50)
    boundaries: list[str] = Field(max_length=50)
    goals: list[str] = Field(max_length=50)
    challengeStyle: str = Field(min_length=1, max_length=1000)
    quietDomains: list[str] = Field(max_length=50)

    @field_validator("vision")
    @classmethod
    def _vision(cls, value: str) -> str:
        return _clean_text(value, "愿景")

    @field_validator("challengeStyle")
    @classmethod
    def _challenge_style(cls, value: str) -> str:
        return _clean_text(value, "挑战方式")

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

    @field_validator("title", "context", "choice", "rationale", "expectedOutcome")
    @classmethod
    def _required_text(cls, value: str, info) -> str:
        return _clean_text(value, info.field_name)

    @field_validator("options", "relatedEntityIds", "evidenceRefs")
    @classmethod
    def _decision_lists(cls, value: list[str], info) -> list[str]:
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


def get_charter():
    return GrowthStore.instance().charter_history()


def create_charter(req: CharterCreate):
    return GrowthStore.instance().create_charter(req.model_dump())


def list_decisions(status: str = ""):
    normalized = status.strip()
    if normalized and normalized not in DECISION_STATUSES:
        raise HTTPException(
            400,
            "status 只能是 open、outcome_recorded 或 reviewed",
        )
    items = GrowthStore.instance().list_decisions(normalized or None)
    return {"items": items, "total": len(items)}


def create_decision(req: DecisionCreate):
    payload = req.model_dump()
    payload["reviewAt"] = _utc_iso(req.reviewAt)
    return GrowthStore.instance().create_decision(payload)


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
        create_decision,
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
