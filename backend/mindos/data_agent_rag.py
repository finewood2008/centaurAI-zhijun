"""Short-lived orchestration for the trusted Data Agent RAG V2 client.

The application credential and confirmation tokens stay inside the worker
process.  Renderer-facing errors contain only redacted hit previews and an
opaque interaction id that names this in-memory state.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass

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


_entries: dict[str, _Entry] = {}
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
        "status": ("sensitive_confirmation_required" if entry.state == "sensitive_confirmation_required"
                   else "sensitive_check_unavailable"),
        "hits": hits,
        "detectionNotice": _public_notice(entry.data.get("detectionNotice")),
        "passedCount": len(entry.passed),
        "canReadOriginal": "mindos.sensitive.original.read" in capabilities,
        "riskAvailable": risk_available,
    }


def _pending_error(interaction_id: str, entry: _Entry) -> HTTPException:
    code = ("RAG_SENSITIVE_CONFIRMATION_REQUIRED" if entry.state == "sensitive_confirmation_required"
            else "RAG_SENSITIVE_CHECK_INCOMPLETE")
    detail = ("资料包含敏感内容，请选择交付方式" if entry.state == "sensitive_confirmation_required"
              else "部分资料的敏感检测尚未完成，请选择如何继续")
    return HTTPException(409, {"code": code, "detail": detail, "ragV2": _public_pending(interaction_id, entry)})


def _client_error(exc) -> HTTPException:
    status = int(getattr(exc, "status", 503) or 503)
    code = str(getattr(exc, "code", "RAG_V2_UNAVAILABLE"))
    retryable = bool(getattr(exc, "retryable", status in {408, 429, 503}))
    trace_id = getattr(exc, "trace_id", None)
    public = {"code": code, "detail": "Data Agent RAG V2 暂时无法完成请求", "retryable": retryable}
    if isinstance(trace_id, str) and trace_id:
        public["traceId"] = trace_id
    return HTTPException(status, public)


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
    if state == "no_results" and items:
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
        if (prior is None or (allowed_materials and value.get("materialId") not in allowed_materials)
                or value.get("materialId") != prior["materialId"]
                or value.get("materialVersion") != prior["materialVersion"]
                or data["policyVersion"] != prior["policyVersion"]
                or data["detectorRevision"] != prior["detectorRevision"]
                or not isinstance(value.get("evidenceExpiresAt"), str)
                or not value["evidenceExpiresAt"].endswith(("Z", "+00:00"))
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


def search(query: str, material_ids: list[str], interaction_id: str, *, top_k: int = 5) -> list[dict]:
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
        if entry and entry.fingerprint != fingerprint:
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
            return _resolve_delivered(material_ids, delivered)
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
        try:
            data = _client().search(
                query, top_k=top_k, material_ids=material_ids, interaction_id=interaction_id,
            )
            state, passed = _search_result(data, material_ids, interaction_id)
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(502, {
                "code": "RAG_V2_CONTRACT_INVALID",
                "detail": "Data Agent 返回的数据不符合 RAG V2 合同",
            }) from None
        except Exception as exc:
            raise _client_error(exc) from None
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
    )
    with _lock:
        _entries[interaction_id] = entry
    raise _pending_error(interaction_id, entry)


def search_materials(query: str, material_ids: list[str], interaction_id: str,
                     *, top_k: int = 5) -> list[dict]:
    """Search an arbitrary protected material set through <=100-id requests."""
    material_ids = list(dict.fromkeys(material_ids))
    if len(material_ids) <= 100:
        return search(query, material_ids, interaction_id, top_k=top_k)
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
        for item in search(query, material_ids[offset:offset + 100], batch_interaction, top_k=top_k):
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


def decide(interaction_id: str, action: str) -> dict:
    """Resolve a pending confirmation without exposing tokens or evidence text."""
    with _lock:
        _prune(time.monotonic())
        entry = _entries.get(interaction_id)
    if entry is None or entry.delivered is not None:
        raise HTTPException(409, {"code": "RAG_CONFIRMATION_EXPIRED", "detail": "资料确认已失效，请重新检索"})
    if action == "cancel":
        with _lock:
            _entries.pop(interaction_id, None)
        return {"status": "cancelled", "interactionId": interaction_id}
    if action == "retry":
        with _lock:
            _entries.pop(interaction_id, None)
        return {"status": "retry", "interactionId": interaction_id}
    try:
        if action == "continue-passed":
            if not entry.passed:
                raise HTTPException(409, {"code": "RAG_NO_PASSED_EVIDENCE", "detail": "当前没有已完成检测的片段"})
            delivered = entry.passed
        elif action in {"masked", "original"}:
            confirmation = entry.data.get("confirmation") or {}
            token = confirmation.get("confirmToken")
            if not isinstance(token, str):
                raise HTTPException(409, {"code": "RAG_CONFIRMATION_EXPIRED", "detail": "资料确认已失效，请重新检索"})
            key = "zj-" + hashlib.sha256(
                (interaction_id + ":" + action + ":" + token).encode()
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
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(502, {"code": "RAG_V2_CONTRACT_INVALID", "detail": "Data Agent 返回的数据不符合 RAG V2 合同"}) from None
    except Exception as exc:
        if (action == "risk-release" or getattr(exc, "code", None) in {
                "CONFIRM_TOKEN_EXPIRED", "CONFIRM_TOKEN_CONSUMED", "CONFIRM_CONTEXT_CHANGED"
        }):
            # A risk token is intentionally one-shot, including when its
            # response is lost. Expired/consumed/stale normal confirmations
            # likewise require a fresh Search instead of replaying old state.
            with _lock:
                _entries.pop(interaction_id, None)
        raise _client_error(exc) from None
    if action in {"masked", "original"}:
        notice = entry.data.get("detectionNotice") or {}
        if type(notice.get("withheldCount")) is int and notice["withheldCount"] > 0:
            # Complete ordinary confirmation first, then ask separately what
            # to do with detector failures. Confirmed text stays server-side
            # and is only delivered after the second explicit decision.
            with _lock:
                entry.state = "sensitive_check_incomplete"
                entry.data = {"detectionNotice": notice}
                entry.passed = list(delivered)
                entry.created_at = time.monotonic()
            return {"status": "pending", "interactionId": interaction_id,
                    "evidenceCount": len(delivered)}
    with _lock:
        entry.delivered = list(delivered)
        entry.created_at = time.monotonic()
    return {"status": "ready", "interactionId": interaction_id, "evidenceCount": len(delivered)}


def reset_for_tests(client=None) -> None:
    global _client_override, _capabilities_cache
    with _lock:
        _entries.clear()
        _client_override = client
        _capabilities_cache = (0.0, set())
