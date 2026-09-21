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
_SAFE_ETAG = r'^"(?:[1-9][0-9]{0,9}|builtin:[1-9][0-9]{0,9}:[0-9]{1,10})"$'
_DETECTOR_REVISION = r"^sensitive-detector-v[0-9]+:[0-9a-f]{16,64}$"
_SAFE_ERRORS = {
    "AUTHENTICATION_REQUIRED": "规则管理凭据不可用，请检查知君 App 状态。",
    "CAPABILITY_DENIED": "当前知君 App 未获此项管理权限，请联系管理员。",
    "CUSTOM_RULE_DUPLICATE": "已有完全重复的规则，请先核对已有规则。",
    "CUSTOM_RULE_CATALOG_TOO_LARGE": "检测提示容量不足，请缩短说明和示例，或停用不再使用的规则。",
    "SENSITIVE_RULE_CONFLICT": "规则已变化，请重新读取详情后再保存。",
    "RULE_REVISION_REQUIRED": "缺少规则并发令牌，请重新读取详情。",
    "BUILTIN_RULE_FIELD_IMMUTABLE": "该内置规则的字段不可修改，请按允许字段调整。",
    "BUILTIN_RULE_PROTECTED": "修改会放宽系统安全底线，已拒绝保存。",
    "BUILTIN_RULE_IMMUTABLE": "内置规则不能按自定义规则删除或修改。",
    "SENSITIVE_RULE_REVISION_CHANGED": "扫描目标已变化，请刷新状态并重新确认。",
    "SENSITIVE_ROLLOUT_RETRY_NOT_ALLOWED": "当前扫描状态不允许重试，请刷新状态。",
    "IDEMPOTENCY_KEY_REUSED": "操作标识已用于其他修订，请刷新并重新确认。",
    "SENSITIVE_SCAN_DISABLED": "历史扫描服务未启用，请联系管理员。",
    "SENSITIVE_ROLLOUT_UNAVAILABLE": "历史扫描服务暂时不可用，请稍后重试。",
    "SENSITIVE_RULE_STORAGE_UNAVAILABLE": "规则存储暂时不可用，请稍后重试。",
    "SENSITIVE_RULE_STATUS_UNAVAILABLE": "规则状态暂时不可读，请稍后刷新。",
}
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
    expectedEtag: str = Field(max_length=80, pattern=_SAFE_ETAG)
    rule: RuleFields


class RuleDelete(Strict):
    expectedEtag: str = Field(max_length=80, pattern=_SAFE_ETAG)


class Masking(Strict):
    strategy: Literal["full", "fixed", "keep_edges"]
    prefixCharacters: int = Field(ge=0, le=32)
    suffixCharacters: int = Field(ge=0, le=32)
    replacement: str = Field(min_length=1, max_length=32)


class BuiltinRuleFields(Strict):
    displayName: Optional[str] = Field(default=None, min_length=2, max_length=50)
    enabled: Optional[bool] = None
    description: Optional[str] = Field(default=None, min_length=10, max_length=500)
    examples: Optional[list[Annotated[str, Field(min_length=1, max_length=200)]]] = Field(default=None, max_length=10)
    counterExamples: Optional[list[Annotated[str, Field(min_length=1, max_length=200)]]] = Field(default=None, max_length=10)
    deliveryMode: Optional[Literal["confirm", "always_mask", "block"]] = None
    allowOriginalAfterConfirm: Optional[bool] = None
    masking: Optional[Masking] = None
    acknowledgeSimilarRuleId: Optional[str] = Field(
        default=None, min_length=1, max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
    )

    @model_validator(mode="after")
    def has_change(self):
        if not self.model_fields_set.intersection({
            "displayName", "enabled", "description", "examples", "counterExamples",
            "deliveryMode", "allowOriginalAfterConfirm", "masking",
        }):
            raise ValueError("at least one editable field is required")
        return self


class BuiltinRuleUpdate(Strict):
    expectedEtag: str = Field(max_length=80, pattern=_SAFE_ETAG)
    rule: BuiltinRuleFields


class BuiltinRuleReset(Strict):
    expectedEtag: str = Field(max_length=80, pattern=_SAFE_ETAG)
    acknowledgeSimilarRuleId: Optional[str] = Field(
        default=None, min_length=1, max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
    )


class RolloutStart(Strict):
    requestId: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    expectedDetectorRevision: str = Field(min_length=38, max_length=128, pattern=_DETECTOR_REVISION)
    confirmHistoricalScan: Literal[True]


class RolloutRetry(Strict):
    requestId: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    expectedDetectorRevision: str = Field(min_length=38, max_length=128, pattern=_DETECTOR_REVISION)
    confirmRetry: Literal[True]


def _client():
    return _client_override if _client_override is not None else configured_client()


def _client_error(exc: DataAgentRagV2Error) -> HTTPException:
    status = exc.status if type(exc.status) is int and 400 <= exc.status <= 599 else 503
    code = exc.code if isinstance(exc.code, str) and _SAFE_CODE.fullmatch(exc.code) else "SENSITIVE_RULE_UNAVAILABLE"
    detail = {
        "code": code,
        "detail": _SAFE_ERRORS.get(code, "敏感规则服务暂时无法完成请求"),
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


def get_capabilities():
    return JSONResponse(_call(lambda: _client().get_sensitive_capabilities()),
                        headers={"Cache-Control": "no-store"})


def get_status():
    try:
        result = _call(lambda: sensitive_rule_status(_client().get_sensitive_rule_status()))
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def get_rollout_status():
    return JSONResponse(_call(lambda: _client().get_sensitive_rollout_status()),
                        headers={"Cache-Control": "no-store"})


def start_rollout(body: RolloutStart):
    result = _call(lambda: _client().start_sensitive_rollout(
        body.expectedDetectorRevision, idempotency_key=body.requestId,
    ))
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})


def retry_rollout(body: RolloutRetry):
    result = _call(lambda: _client().retry_sensitive_rollout(
        body.expectedDetectorRevision, idempotency_key=body.requestId,
    ))
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})


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
        rule_id, body.rule.model_dump(), etag=body.expectedEtag,
    ))


def delete_rule(body: RuleDelete, rule_id: str = Path(
    min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
)):
    return _call(lambda: _client().delete_sensitive_rule(
        rule_id, etag=body.expectedEtag,
    ))


def update_builtin_rule(body: BuiltinRuleUpdate, rule_id: str = Path(
    min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
)):
    return _call(lambda: _client().update_builtin_sensitive_rule(
        rule_id, body.rule.model_dump(exclude_unset=True), etag=body.expectedEtag,
    ))


def reset_builtin_rule(body: BuiltinRuleReset, rule_id: str = Path(
    min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
)):
    return _call(lambda: _client().reset_builtin_sensitive_rule(
        rule_id, etag=body.expectedEtag,
        acknowledge_similar_rule_id=body.acknowledgeSimilarRuleId,
    ))


def build_router(write_guard=None) -> APIRouter:
    built = APIRouter(prefix=_PREFIX, tags=["sensitive-rules"])
    writes = [Depends(write_guard)] if write_guard is not None else []
    built.add_api_route("", list_rules, methods=["GET"])
    built.add_api_route("/capabilities", get_capabilities, methods=["GET"])
    built.add_api_route("/status", get_status, methods=["GET"])
    built.add_api_route("/rollout/status", get_rollout_status, methods=["GET"])
    built.add_api_route("/rollout/start", start_rollout, methods=["POST"], dependencies=writes)
    built.add_api_route("/rollout/retry", retry_rollout, methods=["POST"], dependencies=writes)
    built.add_api_route("/custom", create_rule, methods=["POST"], dependencies=writes)
    built.add_api_route("/custom/{rule_id}", update_rule, methods=["PUT"], dependencies=writes)
    built.add_api_route("/custom/{rule_id}", delete_rule, methods=["DELETE"], dependencies=writes)
    built.add_api_route("/built-in/{rule_id}", update_builtin_rule, methods=["PUT"], dependencies=writes)
    built.add_api_route("/built-in/{rule_id}/reset", reset_builtin_rule, methods=["POST"], dependencies=writes)
    built.add_api_route("/{rule_id}", get_rule, methods=["GET"])
    return built


router = build_router()


def configure_write_guard(guard) -> None:
    global router
    router = build_router(guard)


__all__ = ["build_router", "configure_write_guard", "router"]
