"""Short-lived orchestration for the trusted Data Agent RAG V2 client.

The application credential and confirmation tokens stay inside the worker
process. Renderer-facing choices contain policy-deliverable previews and an
opaque interaction id, never confirmation tokens or application credentials.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException


_TTL_SECONDS = 15 * 60
_MAX_ENTRIES = 128
_lock = threading.RLock()


@dataclass
class _Entry:
    fingerprint: str
    created_at: float
    state: str
    data: dict
    passed: list[dict]
    origin_fence: tuple[int, str]
    allowed_material_ids: tuple[str, ...]
    delivered: list[dict] | None = None
    review_required: bool = False
    review_context: dict = field(default_factory=dict)
    user_selected: bool = False
    delivery_mode: str | None = None
    decision_lock: object = field(default_factory=threading.Lock, repr=False)
    actual_search_id: str = ""


_entries: dict[str, _Entry] = {}
_inflight_searches: set[str] = set()
_client_override = None
_capabilities_cache: tuple[float, set[str]] = (0.0, set())


def _client(*, short_timeout: bool = False):
    if _client_override is not None:
        return _client_override
    from zhijun_worker.data_agent_rag_v2 import configured_client

    return configured_client(timeout=5 if short_timeout else 180)


def _prune(now: float) -> None:
    stale = [key for key, value in _entries.items() if now - value.created_at > _TTL_SECONDS]
    for key in stale:
        _entries.pop(key, None)
    while len(_entries) >= _MAX_ENTRIES:
        oldest = min(_entries, key=lambda key: _entries[key].created_at)
        _entries.pop(oldest, None)


def _fingerprint(query: str, material_ids: list[str], top_k: int) -> str:
    raw = query.strip() + "\0" + str(top_k) + "\0" + "\0".join(material_ids)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_notice(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "code", "message", "retrievedCount", "checkedCount", "withheldCount",
        "titleOmittedCount", "retryable", "riskExpiresAt", "riskEligibleCount", "riskMessage",
    }
    return {key: item for key, item in value.items() if key in allowed and item is not None}


def _validate_notice(value) -> None:
    required = {"code", "message", "retrievedCount", "checkedCount", "withheldCount", "retryable"}
    optional = {"titleOmittedCount", "riskToken", "riskExpiresAt", "riskEligibleCount", "riskMessage"}
    if (not isinstance(value, dict) or not required <= set(value)
            or not set(value) <= required | optional
            or value["code"] not in {"SENSITIVE_CHECK_INCOMPLETE", "SENSITIVE_TITLE_CHECK_INCOMPLETE"}
            or not isinstance(value["message"], str) or not 1 <= len(value["message"]) <= 500
            or type(value["retryable"]) is not bool):
        raise ValueError("RAG_V2_NOTICE_INVALID")
    for key in ("retrievedCount", "checkedCount", "withheldCount", "titleOmittedCount",
                "riskEligibleCount"):
        count = value.get(key, 0)
        if type(count) is not int or not 0 <= count <= 20:
            raise ValueError("RAG_V2_NOTICE_INVALID")
    if (value["retrievedCount"] < 1
            or value["checkedCount"] + value["withheldCount"] != value["retrievedCount"]
            or (value["withheldCount"] == 0 and value.get("titleOmittedCount", 0) == 0)
            or value.get("titleOmittedCount", 0) > value["checkedCount"]
            or (value["code"] == "SENSITIVE_TITLE_CHECK_INCOMPLETE" and value["withheldCount"] > 0)):
        raise ValueError("RAG_V2_NOTICE_INVALID")
    token = value.get("riskToken")
    if token is not None and (not isinstance(token, str) or not token.startswith("scf_")
                              or not 36 <= len(token) <= 256):
        raise ValueError("RAG_V2_NOTICE_INVALID")
    for key in ("riskExpiresAt", "riskMessage"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise ValueError("RAG_V2_NOTICE_INVALID")


def _public_pending(interaction_id: str, entry: _Entry) -> dict:
    interaction_id = entry.actual_search_id if entry.review_required else interaction_id
    if entry.state == "materials_confirmation_required":
        return {
            "interactionId": interaction_id, "status": entry.state, "hits": [],
            "passedCount": len(entry.passed), "canReadOriginal": False, "riskAvailable": False,
            "items": [{
                "previewId": _preview_id(interaction_id, value),
                **{key: value[key] for key in ("materialId", "materialVersion", "title", "locator",
                                               "containsSensitive", "verificationStatus")},
                "preview": value["text"][:500],
                "previewTruncated": len(value["text"]) > 500,
                "textLength": len(value["text"]),
            } for value in entry.passed],
            "query": entry.review_context.get("query", ""),
            "scopeLabel": entry.review_context.get("scopeLabel", "已授权资料"),
            "outcome": entry.review_context.get("outcome", "ok"),
            "deliveryMode": entry.delivery_mode,
        }
    confirmation = entry.data.get("confirmation") if isinstance(entry.data, dict) else None
    hits = []
    for value in (confirmation or {}).get("hits", []):
        if not isinstance(value, dict):
            continue
        hit = {key: value[key] for key in ("category", "materialId", "redactedPreview", "field", "chunkRef")
               if key in value and value[key] is not None}
        if "locator" in value:
            hit["location"] = value["locator"]
        hits.append(hit)
    global _capabilities_cache
    now = time.monotonic()
    with _lock:
        cached_at, capabilities = _capabilities_cache
    if now - cached_at > 60:
        try:
            capabilities = set(_client(short_timeout=True).capabilities().get("enabledCapabilities", []))
        except Exception:
            capabilities = set()
        with _lock:
            _capabilities_cache = (now, capabilities)
    notice = entry.data.get("detectionNotice") if isinstance(entry.data, dict) else None
    risk_token = notice.get("riskToken") if isinstance(notice, dict) else None
    risk_available = (
        isinstance(risk_token, str) and risk_token.startswith("scf_")
        and type(notice.get("riskEligibleCount")) is int
        and notice["riskEligibleCount"] > 0
        and "mindos.sensitive.original.read" in capabilities
    )
    return {
        "interactionId": interaction_id,
        "query": entry.review_context.get("query", ""),
        "scopeLabel": entry.review_context.get("scopeLabel", "已授权资料"),
        "status": ("sensitive_confirmation_required" if entry.state == "sensitive_confirmation_required"
                   else "sensitive_check_unavailable"),
        "hits": hits,
        "detectionNotice": _public_notice(entry.data.get("detectionNotice")),
        "passedCount": len(entry.passed),
        "canReadOriginal": "mindos.sensitive.original.read" in capabilities,
        "riskAvailable": risk_available,
    }


def _pending_error(interaction_id: str, entry: _Entry) -> HTTPException:
    if entry.state == "materials_confirmation_required":
        return HTTPException(409, {"code": "RAG_MATERIAL_REVIEW_REQUIRED",
                                  "detail": "请选择用于本轮回答的材料",
                                  "ragV2": _public_pending(interaction_id, entry)})
    code = ("RAG_SENSITIVE_CONFIRMATION_REQUIRED" if entry.state == "sensitive_confirmation_required"
            else "RAG_SENSITIVE_CHECK_INCOMPLETE")
    detail = ("资料包含敏感内容，请选择交付方式" if entry.state == "sensitive_confirmation_required"
              else "部分资料的敏感检测尚未完成，请选择如何继续")
    return HTTPException(409, {"code": code, "detail": detail, "ragV2": _public_pending(interaction_id, entry)})


def _client_error(exc) -> HTTPException:
    status = getattr(exc, "status", None)
    status = status if type(status) is int and 400 <= status <= 599 else 503
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) is None:
        code = "RAG_V2_UNAVAILABLE"
    retryable = bool(getattr(exc, "retryable", status in {408, 429, 503}))
    trace_id = getattr(exc, "trace_id", None)
    messages = {
        "SENSITIVE_ORIGINAL_CAPABILITY_DENIED": "当前应用没有领取敏感原文的能力，请选择脱敏文本并检查应用能力配置",
        "SENSITIVE_ORIGINAL_POLICY_DENIED": "当前资料策略禁止领取原文，请改用脱敏确认",
        "CONFIRM_TOKEN_EXPIRED": "资料确认已过期，请重新检索后确认",
        "CONFIRM_TOKEN_CONSUMED": "资料确认令牌已使用，请重试原确认请求或重新检索",
        "CONFIRM_CONTEXT_CHANGED": "材料、权限或敏感策略已变化，请重新检索后确认",
        "AUTH_CONTEXT_UNAVAILABLE": "应用授权上下文暂不可用，请使用原凭据稍后重试；持续失败时检查服务端配置",
        "SENSITIVE_CHECK_UNAVAILABLE": "资料检测服务暂不可用，请稍后重新检索",
        "SENSITIVE_RULE_STORAGE_UNAVAILABLE": "敏感规则存储暂不可用，请保留原请求稍后重试",
        "SENSITIVE_RULE_STATUS_UNAVAILABLE": "敏感规则状态暂不可读，不影响继续使用当前生效规则，请稍后查询",
    }
    defaults = {
        401: "Data Agent 应用认证失败，请检查服务端保存的长期凭据及启用状态；不要自动创建或轮换凭据",
        403: "当前应用没有执行此操作的能力，请检查应用授权配置",
        422: "检索请求的字段、类型或范围无效，请修正参数后重试",
        429: "Data Agent 请求过于频繁，请稍后重试",
        503: "Data Agent 服务暂不可用，请稍后重试",
    }
    public = {"code": code, "detail": messages.get(code, defaults.get(status, "Data Agent RAG V2 暂时无法完成请求")),
              "retryable": retryable}
    if isinstance(trace_id, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", trace_id):
        public["traceId"] = trace_id
    retry_after = getattr(exc, "retry_after", None)
    headers = None
    if type(retry_after) is int and retry_after >= 0:
        public["retryAfter"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return HTTPException(status, public, headers=headers)


def _risk_result_unknown(error: HTTPException) -> HTTPException:
    """An uncertain one-shot response must not advertise a replayable action."""
    return HTTPException(error.status_code, {
        **error.detail,
        "code": "RAG_RISK_RESULT_UNKNOWN",
        "detail": "无法确认本次风险领取结果，不能重试原确认；请按等待提示稍后重新检索并明确确认风险",
        "retryable": False,
        "requiresFreshSearch": True,
    }, headers=error.headers)


def _locator(value) -> bool:
    allowed = {"page", "paragraph", "section", "table", "cell", "startMs", "endMs"}
    if not isinstance(value, dict) or not set(value) <= allowed:
        return False
    for key in ("page", "paragraph", "table"):
        if value.get(key) is not None and (type(value[key]) is not int or value[key] < 1):
            return False
    for key, maximum in (("section", 200), ("cell", 80)):
        if value.get(key) is not None and (
                not isinstance(value[key], str) or not 1 <= len(value[key]) <= maximum):
            return False
    for key in ("startMs", "endMs"):
        if value.get(key) is not None and (type(value[key]) is not int or value[key] < 0):
            return False
    return value.get("endMs") is None or (
        value.get("startMs") is not None and value["endMs"] >= value["startMs"]
    )


def _items(values, material_ids: list[str]) -> list[dict]:
    allowed = set(material_ids)
    result = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            raise ValueError("RAG_V2_EVIDENCE_INVALID")
        required = ("evidenceRef", "materialId", "materialVersion", "title", "text", "score", "locator",
                    "containsSensitive", "verificationStatus", "policyVersion", "detectorRevision")
        if set(value) != set(required):
            raise ValueError("RAG_V2_EVIDENCE_INVALID")
        if (not isinstance(value["evidenceRef"], str)
                or re.fullmatch(r"erv2_[A-Za-z0-9_-]{32,251}", value["evidenceRef"]) is None
                or not isinstance(value["materialId"], str) or not 1 <= len(value["materialId"]) <= 128
                or (allowed and value["materialId"] not in allowed)
                or type(value["materialVersion"]) is not int
                or not 1 <= value["materialVersion"] <= 2_147_483_647
                or not isinstance(value["title"], str) or not 1 <= len(value["title"]) <= 200
                or not isinstance(value["text"], str) or not 1 <= len(value["text"]) <= 8000
                or isinstance(value["score"], bool) or not isinstance(value["score"], (int, float))
                or not 0 <= value["score"] <= 1 or not _locator(value["locator"])
                or type(value["containsSensitive"]) is not bool
                or value["verificationStatus"] not in {"verified", "unverified"}
                or type(value["policyVersion"]) is not int or value["policyVersion"] < 1
                or not isinstance(value["detectorRevision"], str)
                or not 1 <= len(value["detectorRevision"]) <= 128):
            raise ValueError("RAG_V2_EVIDENCE_INVALID")
        result.append({key: value[key] for key in required})
    if len(result) > 20:
        raise ValueError("RAG_V2_EVIDENCE_INVALID")
    return result


def _search_result(data: dict, material_ids: list[str], interaction_id: str) -> tuple[str, list[dict]]:
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        raise ValueError("RAG_V2_SEARCH_INVALID")
    state = data["status"]
    if state not in {"ok", "no_results", "sensitive_content_blocked",
                     "sensitive_confirmation_required", "sensitive_check_unavailable"}:
        raise ValueError("RAG_V2_SEARCH_INVALID")
    if state == "sensitive_confirmation_required":
        if not set(data) <= {"status", "confirmation", "detectionNotice"}:
            raise ValueError("RAG_V2_SEARCH_INVALID")
        confirmation = data.get("confirmation")
        required_confirmation = {"confirmToken", "expiresAt", "interactionId", "policyVersion",
                                 "detectorRevision", "hits"}
        if (not isinstance(confirmation, dict) or set(confirmation) != required_confirmation
                or confirmation.get("interactionId") != interaction_id
                or not isinstance(confirmation.get("confirmToken"), str)
                or not confirmation["confirmToken"].startswith("scf_")
                or not 36 <= len(confirmation["confirmToken"]) <= 256
                or not isinstance(confirmation.get("expiresAt"), str)
                or not confirmation["expiresAt"].endswith(("Z", "+00:00"))
                or type(confirmation.get("policyVersion")) is not int
                or confirmation["policyVersion"] < 1
                or not isinstance(confirmation.get("detectorRevision"), str)
                or not 1 <= len(confirmation["detectorRevision"]) <= 128
                or not isinstance(confirmation.get("hits"), list)
                or not 1 <= len(confirmation["hits"]) <= 100):
            raise ValueError("RAG_V2_SEARCH_INVALID")
        allowed_materials = set(material_ids)
        for hit in confirmation["hits"]:
            if (not isinstance(hit, dict)
                    or not {"category", "materialId", "locator", "redactedPreview"} <= set(hit)
                    or not set(hit) <= {"category", "materialId", "locator", "redactedPreview", "field", "chunkRef"}
                    or not all(isinstance(hit.get(key), str) and hit[key] for key in ("category", "materialId", "redactedPreview"))
                    or (allowed_materials and hit["materialId"] not in allowed_materials)
                    or not 1 <= len(hit["category"]) <= 80
                    or not 1 <= len(hit["materialId"]) <= 128
                    or not 1 <= len(hit["redactedPreview"]) <= 800
                    or hit.get("field", "body") not in {"body", "title"}
                    or (hit.get("chunkRef") is not None and (
                        not isinstance(hit["chunkRef"], str)
                        or re.fullmatch(r"chk_[a-f0-9]{24}", hit["chunkRef"]) is None
                    ))
                    or not _locator(hit.get("locator"))):
                raise ValueError("RAG_V2_SEARCH_INVALID")
        if data.get("detectionNotice") is not None:
            _validate_notice(data["detectionNotice"])
        return state, []
    required = {"status", "items", "returnedCount", "policyVersion", "detectorRevision"}
    if not required <= set(data) or not set(data) <= required | {"detectionNotice"}:
        raise ValueError("RAG_V2_SEARCH_INVALID")
    items = _items(data["items"], material_ids)
    if data.get("detectionNotice") is not None:
        _validate_notice(data["detectionNotice"])
    if (type(data["returnedCount"]) is not int or data["returnedCount"] != len(items)
            or type(data["policyVersion"]) is not int or data["policyVersion"] < 1
            or not isinstance(data["detectorRevision"], str) or not data["detectorRevision"]):
        raise ValueError("RAG_V2_SEARCH_INVALID")
    if any(item["verificationStatus"] != "verified"
           or item["policyVersion"] != data["policyVersion"]
           or item["detectorRevision"] != data["detectorRevision"]
           for item in items):
        raise ValueError("RAG_V2_SEARCH_INVALID")
    if state in {"no_results", "sensitive_content_blocked"} and items:
        raise ValueError("RAG_V2_SEARCH_INVALID")
    if (state == "no_results" and data.get("detectionNotice") is not None
            and data["detectionNotice"]["retrievedCount"] > 0):
        raise ValueError("RAG_V2_SEARCH_INVALID")
    if state == "sensitive_check_unavailable" and items:
        raise ValueError("RAG_V2_SEARCH_INVALID")
    if state == "sensitive_check_unavailable" and (
            data.get("detectionNotice") is None or data["detectionNotice"]["checkedCount"] != 0):
        raise ValueError("RAG_V2_SEARCH_INVALID")
    return state, items


def _resolved_items(data: dict, material_ids: list[str], original: list[dict]) -> list[dict]:
    if (not isinstance(data, dict) or set(data) != {"items", "policyVersion", "detectorRevision"}
            or type(data["policyVersion"]) is not int or data["policyVersion"] < 1
            or not isinstance(data["detectorRevision"], str) or not data["detectorRevision"]):
        raise ValueError("RAG_V2_RESOLVE_INVALID")
    values = data["items"]
    if not isinstance(values, list) or not 1 <= len(values) <= 10 or len(values) != len(original):
        raise ValueError("RAG_V2_RESOLVE_INVALID")
    expected = {item["evidenceRef"]: item for item in original}
    allowed_materials = set(material_ids)
    required = {"evidenceRef", "evidenceExpiresAt", "materialId", "materialVersion", "title", "text",
                "locator", "containsSensitive", "verificationStatus"}
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("RAG_V2_RESOLVE_INVALID")
        prior = expected.get(value.get("evidenceRef"))
        try:
            expires = datetime.fromisoformat(str(value.get("evidenceExpiresAt", "")).replace("Z", "+00:00"))
            valid_expiry = expires.tzinfo is not None and expires > datetime.now(timezone.utc)
        except ValueError:
            valid_expiry = False
        if (prior is None or (allowed_materials and value.get("materialId") not in allowed_materials)
                or value.get("materialId") != prior["materialId"]
                or value.get("materialVersion") != prior["materialVersion"]
                or data["policyVersion"] != prior["policyVersion"]
                or data["detectorRevision"] != prior["detectorRevision"]
                or not isinstance(value.get("evidenceExpiresAt"), str)
                or not value["evidenceExpiresAt"].endswith(("Z", "+00:00"))
                or not valid_expiry
                or not isinstance(value.get("title"), str) or not 1 <= len(value["title"]) <= 200
                or not isinstance(value.get("text"), str) or not 1 <= len(value["text"]) <= 8000
                or not _locator(value.get("locator"))
                or value.get("containsSensitive") is not prior["containsSensitive"]
                or value.get("verificationStatus") != prior["verificationStatus"]):
            raise ValueError("RAG_V2_RESOLVE_INVALID")
        result.append({
            "evidenceRef": value["evidenceRef"], "materialId": value["materialId"],
            "materialVersion": value["materialVersion"], "title": value["title"], "text": value["text"],
            "score": prior["score"], "locator": value["locator"],
            "containsSensitive": value["containsSensitive"],
            "verificationStatus": value["verificationStatus"],
            "policyVersion": data["policyVersion"], "detectorRevision": data["detectorRevision"],
        })
    if [item["evidenceRef"] for item in result] != [item["evidenceRef"] for item in original]:
        raise ValueError("RAG_V2_EVIDENCE_ORDER_INVALID")
    return result


def _resolve_delivered(material_ids: list[str], delivered: list[dict]) -> list[dict]:
    """Resolve at most ten refs per contract while preserving one result fence."""
    result = []
    fence = None
    for offset in range(0, len(delivered), 10):
        original = delivered[offset:offset + 10]
        data = _client().evidence_resolve([item["evidenceRef"] for item in original])
        current_fence = (data.get("policyVersion"), data.get("detectorRevision")) \
            if isinstance(data, dict) else None
        if fence is not None and current_fence != fence:
            raise ValueError("RAG_V2_EVIDENCE_FENCE_CHANGED")
        fence = current_fence
        result.extend(_resolved_items(data, material_ids, original))
    return result


def _preview_id(interaction_id: str, value: dict) -> str:
    return "prv_" + hashlib.sha256((interaction_id + "\0" + value["evidenceRef"]).encode()).hexdigest()[:24]


def _review_resolve(material_ids: list[str], values: list[dict]) -> list[dict]:
    resolved = _resolve_delivered(material_ids, values)
    if any(any(new[key] != old[key] for key in (
            "text", "title", "locator", "containsSensitive", "verificationStatus"))
           for old, new in zip(values, resolved)):
        raise HTTPException(409, {"code": "RAG_REVIEW_CONTEXT_CHANGED",
                                  "detail": "材料内容已变化，请重新检索并确认"})
    return resolved


def search(query: str, material_ids: list[str], interaction_id: str, *, top_k: int = 5,
           require_review: bool = False, review_context: dict | None = None) -> list[dict]:
    """Keep a stable local review handle while serializing its Search attempts."""
    with _lock:
        if interaction_id in _inflight_searches:
            raise HTTPException(409, {"code": "RAG_SEARCH_IN_PROGRESS", "detail": "正在检索此轮资料，请稍候"})
        _inflight_searches.add(interaction_id)
    try:
        return _search(query, material_ids, interaction_id, top_k=top_k,
                       require_review=require_review, review_context=review_context)
    finally:
        with _lock:
            _inflight_searches.discard(interaction_id)


def _search(query: str, material_ids: list[str], interaction_id: str, *, top_k: int,
            require_review: bool, review_context: dict | None) -> list[dict]:
    """Return only deliverable evidence, or raise a redacted user-choice state."""
    query = query.strip()
    material_ids = list(dict.fromkeys(material_ids))
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise HTTPException(422, {"code": "RAG_TOP_K_INVALID", "detail": "检索数量必须在 1 到 20 之间"})
    fingerprint = _fingerprint(query, material_ids, top_k)
    now = time.monotonic()
    pending_entry = None
    with _lock:
        _prune(now)
        entry = _entries.get(interaction_id)
        if entry and (entry.fingerprint != fingerprint or entry.review_required != require_review):
            _entries.pop(interaction_id, None)
            entry = None
        if entry and entry.delivered is not None:
            delivered = list(entry.delivered)
        else:
            delivered = None
        if entry and delivered is None:
            pending_entry = entry
    if pending_entry is not None:
        # Prompt rendering may refresh capabilities over HTTP; never hold the
        # global interaction lock while doing network I/O.
        raise _pending_error(interaction_id, pending_entry)
    if delivered is not None:
        if not delivered:
            return []
        try:
            return (_review_resolve(material_ids, delivered) if entry.review_required
                    else _resolve_delivered(material_ids, delivered))
        except HTTPException:
            with _lock:
                _entries.pop(interaction_id, None)
            raise
        except ValueError:
            with _lock:
                _entries.pop(interaction_id, None)
            raise HTTPException(502, {
                "code": "RAG_V2_CONTRACT_INVALID",
                "detail": "Data Agent 返回的数据不符合 RAG V2 合同",
            }) from None
        except Exception as exc:
            if getattr(exc, "status", None) not in {404, 409, 410}:
                raise _client_error(exc) from None
            with _lock:
                _entries.pop(interaction_id, None)
    for search_attempt in range(2):
        # This names an actual REST invocation, not a conversation/tool handle.
        # Every retry (including stale evidence recovery) gets a new identifier.
        prefix = interaction_id.split(":", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,88}", prefix):
            raise HTTPException(422, {"code": "RAG_INTERACTION_ID_INVALID", "detail": "检索会话标识无效"})
        actual_search_id = prefix + ":search:" + uuid.uuid4().hex
        try:
            data = _client().search(
                query, top_k=top_k, material_ids=material_ids, interaction_id=actual_search_id,
            )
            state, passed = _search_result(data, material_ids, actual_search_id)
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(502, {
                "code": "RAG_V2_CONTRACT_INVALID",
                "detail": "Data Agent 返回的数据不符合 RAG V2 合同",
            }) from None
        except Exception as exc:
            raise _client_error(exc) from None
        if require_review and state in {"ok", "no_results", "sensitive_content_blocked"} and not data.get("detectionNotice"):
            context = {"query": query, "scopeLabel": "指定资料" if material_ids else "已授权资料",
                       "outcome": state}
            if isinstance(review_context, dict) and isinstance(review_context.get("scopeLabel"), str):
                context["scopeLabel"] = review_context["scopeLabel"][:200]
            entry = _Entry(fingerprint, now, "materials_confirmation_required", {},
                           passed if state == "ok" else [],
                           (data["policyVersion"], data["detectorRevision"]), tuple(material_ids),
                           review_required=True, review_context=context, actual_search_id=actual_search_id)
            with _lock:
                _entries[interaction_id] = entry
            raise _pending_error(interaction_id, entry)
        if state in {"no_results", "sensitive_content_blocked"}:
            return []
        if state == "ok" and not data.get("detectionNotice"):
            # Search refs are short-lived capabilities. Resolve them under the
            # same policy/detector fence before any text can enter a model
            # request. A stale ref between Search and resolve gets one bounded
            # fresh Search; no stale result or token is replayed.
            try:
                resolved = _resolve_delivered(material_ids, passed) if passed else []
            except ValueError:
                raise HTTPException(502, {
                    "code": "RAG_V2_CONTRACT_INVALID",
                    "detail": "Data Agent 返回的数据不符合 RAG V2 合同",
                }) from None
            except Exception as exc:
                if getattr(exc, "status", None) in {404, 409, 410} and search_attempt == 0:
                    continue
                raise _client_error(exc) from None
            with _lock:
                _entries[interaction_id] = _Entry(
                    fingerprint, now, state, {}, passed,
                    (data["policyVersion"], data["detectorRevision"]),
                    tuple(material_ids), passed,
                    actual_search_id=actual_search_id,
                )
            return resolved
        break
    if state not in {"sensitive_confirmation_required", "sensitive_check_unavailable", "ok"}:
        raise HTTPException(502, {"code": "RAG_V2_CONTRACT_INVALID", "detail": "Data Agent 返回了未知检索状态"})
    pending_state = state if state != "ok" else "sensitive_check_incomplete"
    fence_source = data["confirmation"] if state == "sensitive_confirmation_required" else data
    entry = _Entry(
        fingerprint, now, pending_state, data, passed,
        (fence_source["policyVersion"], fence_source["detectorRevision"]),
        tuple(material_ids),
        review_required=require_review,
        actual_search_id=actual_search_id,
        review_context={"query": query,
                        "scopeLabel": str((review_context or {}).get("scopeLabel") or
                                          ("指定资料" if material_ids else "已授权资料"))[:200],
                        "outcome": "ok"},
    )
    with _lock:
        _entries[interaction_id] = entry
    raise _pending_error(interaction_id, entry)


def search_materials(query: str, material_ids: list[str], interaction_id: str,
                     *, top_k: int = 5, require_review: bool = False,
                     review_context: dict | None = None) -> list[dict]:
    """Search an arbitrary protected material set through <=100-id requests."""
    material_ids = list(dict.fromkeys(material_ids))
    if len(material_ids) <= 100:
        return search(query, material_ids, interaction_id, top_k=top_k,
                      require_review=require_review, review_context=review_context)
    batch_ids = []
    merged = []
    fence = None
    for index, offset in enumerate(range(0, len(material_ids), 100)):
        suffix = ":batch:" + str(index)
        batch_interaction = interaction_id + suffix
        if len(batch_interaction) > 128:
            marker = ":" + hashlib.sha256(interaction_id.encode()).hexdigest()[:16]
            batch_interaction = interaction_id[:128 - len(marker) - len(suffix)] + marker + suffix
        batch_ids.append(batch_interaction)
        for item in search(query, material_ids[offset:offset + 100], batch_interaction, top_k=top_k,
                           require_review=require_review, review_context=review_context):
            current = (item["policyVersion"], item["detectorRevision"])
            if fence is not None and current != fence:
                with _lock:
                    for key in batch_ids:
                        _entries.pop(key, None)
                raise HTTPException(409, {
                    "code": "RAG_V2_CONTEXT_CHANGED",
                    "detail": "资料策略在检索期间发生变化，请重新检索",
                })
            fence = current
            merged.append(item)
    unique = {item["evidenceRef"]: item for item in merged}
    return sorted(unique.values(), key=lambda item: (-item["score"], item["evidenceRef"]))[:top_k]


def decide(interaction_id: str, action: str, selected_preview_ids: list[str] | None = None) -> dict:
    """Serialize one-shot decisions per interaction, never across workspaces."""
    with _lock:
        _prune(time.monotonic())
        entry = _entries.get(interaction_id)
        if entry is not None and entry.review_required:
            # New review flows accept only the exact renderer-facing Search
            # attempt, never the stable internal cache key from an old prompt.
            entry = None
        if entry is None:
            match = next(((key, value) for key, value in _entries.items()
                          if value.review_required and value.actual_search_id == interaction_id), None)
            if match is not None:
                interaction_id, entry = match
    if entry is None:
        raise HTTPException(409, {"code": "RAG_CONFIRMATION_EXPIRED", "detail": "资料确认已失效，请重新检索"})
    if not entry.decision_lock.acquire(blocking=False):
        raise HTTPException(409, {"code": "RAG_DECISION_IN_PROGRESS", "detail": "正在处理资料确认，请稍候"})
    try:
        return _decide(interaction_id, action, selected_preview_ids, entry)
    finally:
        entry.decision_lock.release()


def _decide(interaction_id: str, action: str, selected_preview_ids: list[str] | None,
            expected_entry: _Entry) -> dict:
    """Resolve a pending confirmation without exposing tokens or evidence text."""
    with _lock:
        _prune(time.monotonic())
        entry = _entries.get(interaction_id)
    if entry is None or entry is not expected_entry or entry.delivered is not None:
        raise HTTPException(409, {"code": "RAG_CONFIRMATION_EXPIRED", "detail": "资料确认已失效，请重新检索"})
    public_id = entry.actual_search_id if entry.review_required else interaction_id
    if action == "cancel":
        with _lock:
            _entries.pop(interaction_id, None)
        return {"status": "cancelled", "interactionId": public_id}
    if action == "retry":
        with _lock:
            _entries.pop(interaction_id, None)
        return {"status": "retry", "interactionId": public_id}
    try:
        if selected_preview_ids is not None and action != "use-selected":
            raise HTTPException(422, {"code": "RAG_SELECTION_INVALID", "detail": "此操作不接受材料选择"})
        if action == "without-materials":
            delivered = []
        elif action == "use-selected":
            available = {_preview_id(public_id, value): value for value in entry.passed}
            if (not entry.review_required or entry.state != "materials_confirmation_required"
                    or not isinstance(selected_preview_ids, list) or not selected_preview_ids
                    or any(not isinstance(value, str) for value in selected_preview_ids)
                    or len(set(selected_preview_ids)) != len(selected_preview_ids)
                    or not set(selected_preview_ids) <= set(available)):
                raise HTTPException(422, {"code": "RAG_SELECTION_INVALID", "detail": "请选择当前列表中的材料"})
            selected = set(selected_preview_ids)
            delivered = [value for key, value in available.items() if key in selected]
            delivered = _review_resolve(list(entry.allowed_material_ids), delivered)
        elif entry.state == "materials_confirmation_required":
            raise HTTPException(409, {"code": "RAG_MATERIAL_REVIEW_REQUIRED", "detail": "请先选择用于本轮回答的材料"})
        elif action == "continue-passed":
            if not entry.passed:
                raise HTTPException(409, {"code": "RAG_NO_PASSED_EVIDENCE", "detail": "当前没有已完成检测的片段"})
            delivered = entry.passed
        elif action in {"masked", "original"}:
            confirmation = entry.data.get("confirmation") or {}
            token = confirmation.get("confirmToken")
            if (not isinstance(token, str) or not entry.actual_search_id
                    or confirmation.get("interactionId") != entry.actual_search_id):
                raise HTTPException(409, {"code": "RAG_CONFIRMATION_EXPIRED", "detail": "资料确认已失效，请重新检索"})
            key = "zj-" + hashlib.sha256(
                (entry.actual_search_id + ":" + action + ":" + token).encode()
            ).hexdigest()[:48]
            result = _client().confirm(token, desensitize=action == "masked", idempotency_key=key)
            if (not isinstance(result, dict) or set(result) != {
                    "status", "desensitized", "containsSensitive", "policyVersion",
                    "detectorRevision", "items"
                } or result.get("status") != "ok"
                    or result.get("desensitized") is not (action == "masked")
                    or result.get("containsSensitive") is not (action == "original")
                    or type(result.get("policyVersion")) is not int or result["policyVersion"] < 1
                    or not isinstance(result.get("detectorRevision"), str)
                    or not result["detectorRevision"]
                    or (result["policyVersion"], result["detectorRevision"]) != entry.origin_fence):
                raise ValueError("RAG_V2_CONFIRM_INVALID")
            delivered = _items(result.get("items", []), list(entry.allowed_material_ids))
            if (any(item["verificationStatus"] != "verified"
                    or item["policyVersion"] != result["policyVersion"]
                    or item["detectorRevision"] != result["detectorRevision"]
                    or (action == "masked" and item["containsSensitive"])
                    for item in delivered)):
                raise ValueError("RAG_V2_CONFIRM_INVALID")
        elif action == "risk-release":
            notice = entry.data.get("detectionNotice") or {}
            token = notice.get("riskToken")
            if (not isinstance(token, str) or type(notice.get("riskEligibleCount")) is not int
                    or notice["riskEligibleCount"] < 1):
                raise HTTPException(403, {"code": "RAG_RISK_RELEASE_DENIED", "detail": "当前没有可风险放行的片段"})
            result = _client().confirm_unverified(token)
            if (not isinstance(result, dict) or set(result) != {
                    "status", "verificationStatus", "riskAccepted", "desensitized",
                    "containsSensitive", "message", "policyVersion", "detectorRevision", "items"
                } or result.get("status") != "unverified_original_released"
                    or result.get("verificationStatus") != "unverified"
                    or result.get("riskAccepted") is not True
                    or result.get("desensitized") is not False
                    or result.get("containsSensitive") is not True
                    or not isinstance(result.get("message"), str)
                    or not 1 <= len(result["message"]) <= 500
                    or type(result.get("policyVersion")) is not int or result["policyVersion"] < 1
                    or not isinstance(result.get("detectorRevision"), str)
                    or not result["detectorRevision"]
                    or (result["policyVersion"], result["detectorRevision"]) != entry.origin_fence):
                raise ValueError("RAG_V2_UNVERIFIED_CONTRACT_INVALID")
            released = _items(result.get("items", []), list(entry.allowed_material_ids))
            if (len(released) != notice["riskEligibleCount"]
                    or any(item["verificationStatus"] != "unverified" or not item["containsSensitive"]
                           or item["policyVersion"] != result["policyVersion"]
                           or item["detectorRevision"] != result["detectorRevision"]
                           for item in released)):
                raise ValueError("RAG_V2_UNVERIFIED_CONTRACT_INVALID")
            delivered = sorted([*entry.passed, *released], key=lambda item: -item["score"])
            if len(delivered) > 20 or len({item["evidenceRef"] for item in delivered}) != len(delivered):
                raise ValueError("RAG_V2_UNVERIFIED_CONTRACT_INVALID")
        else:
            raise HTTPException(400, {"code": "RAG_DECISION_INVALID", "detail": "未知的资料确认操作"})
    except HTTPException as exc:
        if action == "use-selected":
            # Invalid selection can be corrected; changed evidence cannot be reused.
            if exc.status_code == 409:
                with _lock:
                    _entries.pop(interaction_id, None)
        raise
    except ValueError:
        if action in {"risk-release", "use-selected"}:
            with _lock:
                _entries.pop(interaction_id, None)
        error = HTTPException(502, {"code": "RAG_V2_CONTRACT_INVALID", "detail": "Data Agent 返回的数据不符合 RAG V2 合同"})
        raise (_risk_result_unknown(error) if action == "risk-release" else error) from None
    except Exception as exc:
        if (action in {"risk-release", "use-selected"} or getattr(exc, "code", None) in {
                "CONFIRM_TOKEN_EXPIRED", "CONFIRM_TOKEN_CONSUMED", "CONFIRM_CONTEXT_CHANGED"
        }):
            # A risk token is intentionally one-shot, including when its
            # response is lost. Expired/consumed/stale normal confirmations
            # likewise require a fresh Search instead of replaying old state.
            with _lock:
                _entries.pop(interaction_id, None)
        error = _client_error(exc)
        raise (_risk_result_unknown(error) if action == "risk-release" else error) from None
    if action in {"masked", "original"}:
        notice = entry.data.get("detectionNotice") or {}
        if type(notice.get("withheldCount")) is int and notice["withheldCount"] > 0:
            # Complete ordinary confirmation first, then ask separately what
            # to do with detector failures. Confirmed text stays server-side
            # and is only delivered after the second explicit decision.
            with _lock:
                if _entries.get(interaction_id) is not entry:
                    raise HTTPException(409, {"code": "RAG_CONFIRMATION_EXPIRED", "detail": "资料确认已失效，请重新检索"})
                entry.state = "sensitive_check_incomplete"
                entry.data = {"detectionNotice": notice}
                entry.passed = list(delivered)
                entry.created_at = time.monotonic()
            return {"status": "pending", "interactionId": public_id,
                    "evidenceCount": len(delivered)}
    with _lock:
        if _entries.get(interaction_id) is not entry:
            raise HTTPException(409, {"code": "RAG_CONFIRMATION_EXPIRED", "detail": "资料确认已失效，请重新检索"})
        if entry.review_required and action not in {"use-selected", "without-materials"}:
            entry.state = "materials_confirmation_required"
            entry.passed = list(delivered)
            entry.data = {}
            entry.delivery_mode = action
            return {"status": "pending", "interactionId": public_id, "evidenceCount": len(delivered)}
        entry.delivered = list(delivered)
        entry.user_selected = action == "use-selected"
        entry.created_at = time.monotonic()
    return {"status": "ready", "interactionId": public_id, "evidenceCount": len(delivered)}


def reviewed_evidence(conversation_id: str, *, interaction_id: str) -> list[dict] | None:
    """Freshly validate the entire selection as one indivisible evidence fence.

    Callers may share this result inside one synchronous authorization boundary;
    it is never a reusable grant or a process-wide cached proof of permission.
    """
    if not isinstance(interaction_id, str) or not interaction_id.startswith(conversation_id + ":"):
        return None
    with _lock:
        _prune(time.monotonic())
        entry = _entries.get(interaction_id)
        if not entry or not entry.review_required or not entry.user_selected or not entry.delivered:
            return None
        values = list(entry.delivered)
    try:
        items = _review_resolve(list(entry.allowed_material_ids), values)
    except Exception:
        with _lock:
            if _entries.get(interaction_id) is entry:
                _entries.pop(interaction_id, None)
        return None
    with _lock:
        if (_entries.get(interaction_id) is not entry or not entry.user_selected
                or entry.delivered != values or time.monotonic() - entry.created_at > _TTL_SECONDS):
            return None
    return items


def reviewed_material(conversation_id: str, material_id: str, version: int,
                      *, interaction_id: str | None = None) -> dict | None:
    """Compatibility adapter over a fresh full-selection validation."""
    with _lock:
        _prune(time.monotonic())
        candidates = [key for key, entry in _entries.items()
                      if key.startswith(conversation_id + ":")
                      and (interaction_id is None or key == interaction_id)
                      and entry.review_required and entry.user_selected and entry.delivered
                      and any(value["materialId"] == material_id and value["materialVersion"] == version
                              for value in entry.delivered)]
    for key in candidates:
        resolved = reviewed_evidence(conversation_id, interaction_id=key)
        if resolved is None:
            return None
        items = [value for value in resolved
                  if value["materialId"] == material_id and value["materialVersion"] == version]
        if not items:
            continue
        return {"title": items[0]["title"], "materialId": material_id,
                "materialVersion": version, "items": items}
    return None


def reset_for_tests(client=None) -> None:
    global _client_override, _capabilities_cache
    with _lock:
        _entries.clear()
        _inflight_searches.clear()
        _client_override = client
        _capabilities_cache = (0.0, set())
