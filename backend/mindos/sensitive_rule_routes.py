"""Renderer-safe facade for Data Agent V2 sensitive-rule management."""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from zhijun_worker.data_agent_rag_v2 import (
    MAX_RETRY_AFTER_SECONDS, DataAgentRagV2Error, configured_client, sensitive_rule_status,
)


_PREFIX = "/api/mindos/settings/sensitive-rules"
_SAFE_CODE = re.compile(r"[A-Z0-9_]{1,128}")
_SAFE_TRACE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SAFE_RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
_client_override = None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RuleFields(Strict):
    name: str = Field(min_length=2, max_length=50)
    description: str = Field(min_length=10, max_length=500)
    examples: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=10,
    )
    counterExamples: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=10,
    )
    enabled: bool
    deliveryMode: Literal["confirm", "always_mask", "block"]
    allowOriginalAfterConfirm: bool
    acknowledgeSimilarRuleId: Optional[str] = Field(
        default=None, min_length=1, max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
    )

    @field_validator("name", "description")
    @classmethod
    def clean_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = value.strip()
        minimum = 2 if info.field_name == "name" else 10
        if len(cleaned) < minimum or any(ord(char) < 32 for char in cleaned):
            raise ValueError("invalid sensitive rule text")
        return cleaned

    @field_validator("examples", "counterExamples")
    @classmethod
    def clean_examples(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            cleaned = value.strip()
            if not cleaned or any(ord(char) < 32 for char in cleaned):
                raise ValueError("invalid sensitive rule example")
            if cleaned not in result:
                result.append(cleaned)
        return result

    @model_validator(mode="after")
    def coherent_delivery(self):
        if self.allowOriginalAfterConfirm and self.deliveryMode != "confirm":
            raise ValueError("only confirm mode can release original content")
        return self


class RuleCreate(Strict):
    requestId: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$",
    )
    rule: RuleFields


class RuleUpdate(Strict):
    requestId: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$",
    )
    expectedRevision: int = Field(ge=1, le=2_147_483_647)
    rule: RuleFields


class RuleDelete(Strict):
    expectedRevision: int = Field(ge=1, le=2_147_483_647)


def _client():
    return _client_override if _client_override is not None else configured_client()


def _client_error(exc: DataAgentRagV2Error) -> HTTPException:
    status = exc.status if type(exc.status) is int and 400 <= exc.status <= 599 else 503
    code = exc.code if isinstance(exc.code, str) and _SAFE_CODE.fullmatch(exc.code) else "SENSITIVE_RULE_UNAVAILABLE"
    detail = {
        "code": code,
        "detail": "敏感规则服务暂时无法完成请求",
        "retryable": bool(exc.retryable),
    }
    if (code == "CUSTOM_RULE_SIMILAR" and status == 409
            and isinstance(exc.similar_rule_id, str)
            and _SAFE_RULE_ID.fullmatch(exc.similar_rule_id)):
        detail["similarRuleId"] = exc.similar_rule_id
        detail["detail"] = "发现相似规则，确认后可继续保存"
    if isinstance(exc.trace_id, str) and _SAFE_TRACE.fullmatch(exc.trace_id):
        detail["traceId"] = exc.trace_id
    headers = {}
    if type(exc.retry_after) is int and exc.retry_after >= 0:
        retry_after = min(exc.retry_after, MAX_RETRY_AFTER_SECONDS)
        detail["retryAfter"] = retry_after
        headers["Retry-After"] = str(retry_after)
    return HTTPException(status, detail, headers=headers)


def _call(operation):
    try:
        return operation()
    except DataAgentRagV2Error as exc:
        raise _client_error(exc) from None


def list_rules():
    return _call(lambda: _client().list_sensitive_rules())


def get_status():
    try:
        result = _call(lambda: sensitive_rule_status(_client().get_sensitive_rule_status()))
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def get_rule(rule_id: str = Path(
    min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
)):
    return _call(lambda: _client().get_sensitive_rule(rule_id))


def create_rule(body: RuleCreate):
    return _call(lambda: _client().create_sensitive_rule(
        body.rule.model_dump(), idempotency_key=body.requestId,
    ))


def update_rule(body: RuleUpdate, rule_id: str = Path(
    min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
)):
    return _call(lambda: _client().update_sensitive_rule(
        rule_id, body.rule.model_dump(), revision=body.expectedRevision,
    ))


def delete_rule(body: RuleDelete, rule_id: str = Path(
    min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
)):
    return _call(lambda: _client().delete_sensitive_rule(
        rule_id, revision=body.expectedRevision,
    ))


def build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=["sensitive-rules"])
    writes = [Depends(write_guard)] if write_guard is not None else []
    built.add_api_route("", list_rules, methods=["GET"])
    built.add_api_route("/status", get_status, methods=["GET"])
    built.add_api_route("/{rule_id}", get_rule, methods=["GET"])
    built.add_api_route("/custom", create_rule, methods=["POST"], dependencies=writes)
    built.add_api_route("/custom/{rule_id}", update_rule, methods=["PUT"], dependencies=writes)
    built.add_api_route("/custom/{rule_id}", delete_rule, methods=["DELETE"], dependencies=writes)
    return built


router = build_router()


def configure_write_guard(guard) -> None:
    global router
    router = build_router(guard)


__all__ = ["build_router", "configure_write_guard", "router"]
