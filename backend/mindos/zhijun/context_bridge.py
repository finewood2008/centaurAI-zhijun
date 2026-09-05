"""Share evidence assembly across existing interactive task routes."""
from dataclasses import replace

from ..chat_imports import service_info
from ..stores.alignment_store import digest
from . import charter_policy
from .context_plan import build_context_plan, fit_context_plan


def fit_for_request(router, provider, plan, system, messages, max_tokens, schema=None):
    limit = getattr(provider, "_num_ctx", None)
    if provider.external or not isinstance(limit, (int, float)) or limit <= 0:
        return plan
    import json
    required = charter_policy.mandatory_context(charter_policy.scope_policy(router.scope),
        "\n".join(str(m.get("content", "")) for m in messages[-2:]))[0]
    used = sum(len(s.encode("utf-8")) for s in [required, system, *(str(m.get("content", "")) for m in messages)])
    if schema:
        used += len(json.dumps(schema, ensure_ascii=False).encode("utf-8"))
    old_refs = plan["refs"]
    fitted = fit_context_plan(plan, limit - used - max_tokens - 512 - 16 * len(messages))
    fitted["removedRefs"] = [r for r in old_refs if r not in fitted["refs"]]
    return fitted


def attach_task_context(router, purpose, request, refs, provider):
    if purpose not in ("reply_assistance", "draft_turn", "decision_suggestions", "learning"):
        return request, refs
    if request.debug.get("contextPlan"):
        return request, list({digest(r): r for r in [*refs, *request.debug["contextPlan"]["refs"]]}.values())
    from fastapi import HTTPException
    recent, permitted = router.convs.list_messages(router.cid)[-12:], []
    service = service_info(provider)["id"]
    for message in recent:
        if getattr(router, "context_before_seq", None) is not None and message["seq"] >= router.context_before_seq:
            continue
        if message["status"] != "complete" or (provider.external and message["seq"] <= router.mode["cutoff"]):
            continue
        closure = router.resolve(router.ref("message", message["id"]))
        try:
            router.check_lifecycle(closure)
        except HTTPException:
            continue
        if not any(s["blocked"] or (provider.external and not router.allowed(s, service, purpose)) for s in closure):
            permitted.append(message)
    query = request.debug.get("userText") or next((m["content"] for m in reversed(permitted) if m["role"] == "user"), "")
    if not query:
        return request, refs
    plan = build_context_plan(router, query, permitted, provider=provider, purpose=purpose)
    plan = fit_for_request(router, provider, plan, request.system, request.messages, request.max_tokens, request.json_schema)
    request = replace(request, system=request.system + ("\n\n" + plan["system"] if plan["system"] else ""),
                      debug={**request.debug, "contextPlan": plan})
    return request, list({digest(r): r for r in [*refs, *plan["refs"]]}.values())
