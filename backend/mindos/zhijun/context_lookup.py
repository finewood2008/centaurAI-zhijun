"""One bounded, permission-checked lookup phase within an existing chat request.

Only local search hints are produced. They never become facts, profile changes,
permissions, network destinations, or executable commands.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace

from ..stores.alignment_store import digest
from ..stores.ontology_store import utc_now
from .provider import ChatRequest, ProviderError


LOOKUP_UNAVAILABLE_NOTICE = "额外补查暂未完成，本轮使用已读取且已授权的信息回答。"
_RETRYABLE_OUTPUT_CODES = {"EMPTY_REPLY", "INVALID_JSON_REPLY"}
_CITATION_MARKER_RE = re.compile(r"[ \t\u00a0]*(?:\[(?:p|m)\d+\])+")


def strip_citation_markers(text: str | None) -> str:
    """Remove per-turn audit IDs before old assistant prose becomes model input."""
    if not text:
        return ""
    return _CITATION_MARKER_RE.sub("", text)


def fingerprint(router, content, *, depth, mode, material_refs, local, omit):
    from .charter_policy import scope_policy, snapshot
    from ..stores.matters_store import MattersStore, source_version
    binding = MattersStore(router.onto, router.convs).binding(router.cid, router.scope)
    matter = binding["matter"]
    return digest([router.scope, content, depth, mode, material_refs or [], local, omit,
                   router.mode, snapshot(scope_policy(router.scope)),
                   binding["bindingRevision"], source_version(matter) if matter else None,
                   getattr(router.provider(local), "_base_url", ""), router.provider(local).model])


def cached(router, request_id, fingerprint_value):
    if not request_id:
        return None
    with router.onto._connect() as db:
        row = db.execute("SELECT fingerprint,payload_json FROM context_lookup_stages WHERE conversation_id=? AND request_id=?",
                         (router.cid, request_id)).fetchone()
    if not row or row["fingerprint"] != fingerprint_value:
        return None
    value = json.loads(row["payload_json"])
    return value if value.get("state") in {"complete", "unavailable"} else None


def _save(router, request_id, fingerprint_value, value):
    with router.onto._lock, router.onto._connect() as db:
        db.execute("INSERT OR REPLACE INTO context_lookup_stages VALUES(?,?,?,?,?)",
                   (router.cid, request_id, fingerprint_value, json.dumps(value, ensure_ascii=False), utc_now()))


def eligible(plan, content, depth, mode, *, request_id, omit=False, charter_exception_id=None):
    if not request_id or omit or charter_exception_id or not plan.provider.external:
        return False
    context = plan.assembled.provenance.get("contextPlan") or {}
    if context.get("stage") in {"supplemented", "lookup_unavailable"}:
        return False
    if context.get("focus", {}).get("continuation") and len(content.strip()) < 24:
        return False
    # Explicit tasks and observable evidence gaps, not a local model guessing intent.
    return (depth == "deep" or mode == "deliberate"
            or bool(re.search(r"比较|对比|回顾|这些年|以前.*现在|结合.*(?:经历|资料|判断)|不同情境", content))
            or bool(context.get("needsLookup")))


def normalize_queries(raw):
    if not isinstance(raw, dict) or set(raw) != {"queries"}:
        raise ValueError("补查规划不是结构化结果")
    value = raw.get("queries", [])
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("补查最多三个本地检索主题")
    result = []
    for item in value:
        if isinstance(item, dict):
            if set(item) - {"query", "entities", "time", "types"}:
                raise ValueError("补查只允许检索条件")
            if not isinstance(item.get("query"), str) or not isinstance(item.get("time", ""), str):
                raise ValueError("补查主题和时间必须为文字")
            if not isinstance(item.get("types", []), list) or any(t not in ("claim", "message", "material", "decision", "summary", "episode") for t in item.get("types", [])):
                raise ValueError("未知补查证据类型")
            text = item["query"].strip()
            entities = item.get("entities") or []
            if not isinstance(entities, list) or len(entities) > 3 or any(not isinstance(e, str) or len(e) > 80 for e in entities):
                raise ValueError("补查实体格式错误")
            text = " ".join([text, *entities[:3], str(item.get("time") or "")]).strip()
        elif isinstance(item, str):
            text = item.strip()
        else:
            raise ValueError("补查主题格式错误")
        if not text or len(text) > 240 or re.search(r"https?://|file://|\x00", text):
            raise ValueError("补查只接受短的本地搜索文字")
        if text not in result:
            result.append(text)
    return result


def run(plan, *, request_id, fingerprint_value):
    from .routing import GuardedProvider
    router = plan.router
    previous = cached(router, request_id, fingerprint_value)
    if previous is not None:
        return previous
    original = ChatRequest(**plan.preview["request"])
    instruction = ("\n\n本次仅规划一次本地补查，不回答用户、不编造事实、不修改个人理解。"
                   "只基于已提供且获准的当前情境，指出回答还需要的主题、实体或时间条件。"
                   '不需要补查就返回 {"queries":[]}；最多三个短搜索句。只能输出JSON：'
                   '{"queries":["搜索主题及必要实体/时间"]}。不要输出命令、网址、建议结论或新的个人事实。')
    request = replace(original, system=original.system + instruction, max_tokens=500,
                      temperature=0, effort="low", json_schema={"type": "object", "properties": {
                          "queries": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 240}}},
                          "required": ["queries"], "additionalProperties": False},
                      debug={**original.debug, "task": "context_lookup", "contextStage": "lookup"})
    preview = router.prepare("chat", request, plan.refs, plan.provider, excluded=plan.preview["excluded"])
    guarded = GuardedProvider(router, plan.provider, "chat", plan.refs,
                              revision=preview["revision"], excluded=plan.preview["excluded"])
    guarded.check(request)
    _save(router, request_id, fingerprint_value, {"state": "running"})
    try:
        for attempt in range(1, 3):
            # The same request/provider is retried; consent and versions are
            # checked before every dispatch and before saving either outcome.
            guarded.check(request)
            failure_code = None
            try:
                raw = guarded.complete_json(request)
                try:
                    queries = normalize_queries(raw)
                except ValueError as exc:
                    raise ProviderError("补查规划格式无效", status_code=502,
                                        code="INVALID_JSON_REPLY", retryable=True) from exc
            except ProviderError as exc:
                if exc.code not in _RETRYABLE_OUTPUT_CODES:
                    raise
                failure_code = exc.code
            # Never turn a revocation, changed source, or changed route into a
            # best-effort answer just because the model also returned bad JSON.
            guarded.check(request)
            if failure_code and attempt < 2:
                continue
            unavailable = failure_code is not None
            queries = [] if unavailable else queries
            value = {"state": "unavailable" if unavailable else "complete",
                     "queries": queries,
                     "stage": "lookup_unavailable" if unavailable else "supplemented",
                     "attempts": attempt,
                     "sources": [s["ref"] for s in guarded.last_preview["sources"]],
                     "charterBasis": guarded.charter_basis, "model": guarded.model}
            if unavailable:
                value.update(notice=LOOKUP_UNAVAILABLE_NOTICE, failureCode=failure_code)
            value["revision"] = digest([fingerprint_value, value])
            _save(router, request_id, fingerprint_value, value)
            return value
    except Exception:
        _save(router, request_id, fingerprint_value, {"state": "failed"})
        raise


def citation_receipt(context, text):
    context = dict(context or {})
    provided_list = list(dict.fromkeys(
        i["citationId"] for i in context.get("background", []) + context.get("evidence", [])
    ))
    provided = set(provided_list)
    seen = list(dict.fromkeys(re.findall(r"\[((?:p|m)[0-9]+)\]", text)))
    context["providedRefs"] = sorted(provided, key=lambda ident: (0 if ident.startswith("p") else 1, int(ident[1:])))
    context["citedRefs"] = [s for s in seen if s in provided]
    context["citationAudit"] = {"invalidRefs": [s for s in seen if s not in provided]}
    return context
