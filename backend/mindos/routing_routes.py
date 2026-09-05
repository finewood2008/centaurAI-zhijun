"""Scoped local UI for task previews, explicit opt-in and revocable consent."""
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .chat_imports import require_conversation, service_info
from .stores.conversation_store import ConversationStore
from .stores.ontology_store import OntologyStore
from .stores.routing_store import RoutingStore
from .stores.chat_import_store import ChatImportStore
from .uploads import _device_scope_of
from .zhijun.provider import build_provider, ProviderError
from .zhijun.routing import Router, check_service, fail, prepare_chat
from .zhijun.reply_assistance import ReplyInput


class Controls(BaseModel):
    routeRevision: str | None = Field(default=None, max_length=64)
    previewOnly: bool = False
    localOnly: bool = False
    charterExceptionId: str | None = Field(default=None, max_length=100)


class Mode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["online", "local"]
    acknowledge: bool = False
    freshContext: bool = False
    expectedRevision: int = Field(ge=0)
    serviceId: str = Field(default="", max_length=64)


class Grant(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    keys: list[str] = Field(default_factory=list, max_length=200)


class Revoke(BaseModel):
    key: str | None = Field(default=None, max_length=200)


class DefaultConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    includeFiles: bool = False
    includeCharter: bool = False
    acknowledge: bool = False
    serviceId: str = Field(default="", max_length=64)
    expectedRevision: int = Field(ge=0)


class Handling(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    action: Literal["omit", "local"] = "omit"
    serviceId: str = Field(default="", max_length=64)
    expectedRevision: int = Field(ge=0)


class Preview(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    depth: Literal["brief", "deep"] = "brief"
    mode: Literal["chat", "deliberate"] = "chat"
    materialRefs: list[dict] = Field(default_factory=list, max_length=5)
    localOnly: bool = False
    omitSources: bool = False
    retryUserMessageId: str | None = Field(default=None, max_length=100)
    replyAssistance: ReplyInput | None = None
    requestId: str | None = Field(default=None, max_length=100)
    charterExceptionId: str | None = Field(default=None, max_length=100)


class CharterException(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    exceptionKey: str = Field(min_length=64, max_length=64)
    acknowledge: bool = False


def router_for(cid, request):
    if cid == "default":
        return Router(OntologyStore.instance(), ConversationStore.instance(), "scope:" + _device_scope_of(request))
    require_conversation(cid, _device_scope_of(request))
    return Router(OntologyStore.instance(), ConversationStore.instance(), cid)


def active_pending(r):
    from .stores.growth_store import GrowthStore
    from .stores.charter_draft_store import CharterDraftStore
    # Preserve old job/consent audit data, but don't keep prompting users to
    # resume automatic charter changes after their first confirmation.
    confirmed = GrowthStore.instance().current_charter(scope=r.scope) is not None
    active_workspace = CharterDraftStore().active_workspace(r.cid, r.scope)
    latest_jobs = r.store.conversation_jobs(r.cid)
    job_tasks = {job["kind"] for job in latest_jobs}
    pending = {p["task_key"]: {**p, "count": 1, "reason": "consent_required", "messageIds": [], "reasons": [{"code": "consent_required", "count": 1, "detail": p["detail"]}]}
               for p in r.store.pending(r.cid) if p["task_key"] not in job_tasks}
    for job in latest_jobs:
        result = job.get("result") or {}
        if not r.store.recoverable_job(job):
            continue
        failed = job["state"] == "failed"
        if failed:
            # Do not expose raw provider errors: they may contain request details.
            cause = {"PROVIDER_TIMEOUT": "模型等待超时", "PROVIDER_BUSY": "模型通道繁忙",
                "PROVIDER_UNAVAILABLE": "模型服务暂时不可用", "PROVIDER_MISCONFIGURED": "模型连接设置需要检查",
                "INVALID_JSON_REPLY": "模型返回的整理内容不完整", "EMPTY_REPLY": "模型没有返回整理内容"}.get(job.get("errorCode"), "本次整理未完成")
            result = {"reason": "task_failed", "detail": cause + "，原对话仍保留。可重试；继续前会重新核对当前模型、资料权限与人生章程。"}
        task = job["kind"]
        item = pending.setdefault(task, {"conversation_id": r.cid, "task_key": task, "preview_id": "",
            "count": 0, "failedCount": 0, "reason": result.get("reason", "consent_required"), "detail": "", "messageIds": [], "updated_at": "", "reasons": []})
        item["count"] += 1
        item["failedCount"] += int(failed)
        message_id = job["payload"].get("messageId")
        if message_id and message_id not in item["messageIds"]:
            item["messageIds"].append(message_id)
        if result.get("previewId"):
            item["preview_id"] = result["previewId"]
        item["reason"] = result.get("reason", "consent_required")
        item["detail"] = result.get("detail") or ("后台任务缺少当前用途授权，已暂停" if item["reason"] == "consent_required" else "后台任务的来源或处理规则已变化，请重新核对")
        reason = next((entry for entry in item["reasons"] if entry["code"] == item["reason"]), None)
        if reason:
            reason["count"] += 1
        else:
            item["reasons"].append({"code": item["reason"], "count": 1, "detail": item["detail"]})
        item["updated_at"] = job["updatedAt"]
    values = []
    for item in pending.values():
        if confirmed and not active_workspace and item["task_key"] == "charter_draft":
            continue
        if len(item["reasons"]) > 1:
            item["reason"] = "multiple_reasons"
            item["detail"] = "；".join(f"{entry['count']} 项：{entry['detail']}" for entry in item["reasons"])
        item["state"] = "failed" if item.get("failedCount") == item["count"] else "mixed" if item.get("failedCount") else "paused"
        item["previewExpired"] = not bool(item["preview_id"] and r.store.get_preview(item["preview_id"], r.cid))
        values.append(item)
    return values


def handling_state(store, scope, service):
    value = store.handling(scope)
    return {**value, "active": value["enabled"] and value["service"] == (service or {}).get("id"),
            "serviceChanged": value["enabled"] and value["service"] != (service or {}).get("id")}


def set_handling(conversation_id: str, req: Handling, request: Request):
    r = router_for(conversation_id, request)
    service = r.store.handling(r.scope)["service"]
    if req.enabled:
        provider = build_provider()
        check_service(provider)
        info = service_info(provider)
        if not provider.external or req.serviceId != info["id"]:
            fail("HANDLING_SERVICE_CHANGED", "服务已变化，请核对后保存默认处理方式")
        service = info["id"]
    try:
        r.store.set_handling(r.scope, enabled=req.enabled, action=req.action, service=service, expected_revision=req.expectedRevision)
    except ValueError as exc:
        fail("HANDLING_CHANGED", str(exc))
    return default_state(request) if conversation_id == "default" else state(conversation_id, request)


def state(conversation_id: str, request: Request):
    r = router_for(conversation_id, request)
    try:
        p = build_provider()
        check_service(p)
        service, error = service_info(p), ""
    except Exception as exc:
        service, error = None, str(exc)
    return {"mode": r.mode, "service": service, "error": error,
            "handlingPreference": handling_state(r.store, r.scope, service),
            "defaultAuthorization": policy_state(r.store, r.scope, service),
            "pending": active_pending(r),
            "notice": "在线模式会发送日常消息；文件、画像和受保护历史另行授权。已发送内容无法收回。"}


def set_mode(conversation_id: str, req: Mode, request: Request):
    r = router_for(conversation_id, request)
    if r.mode["revision"] != req.expectedRevision:
        fail("ROUTE_CHANGED", "模式已更新，请刷新")
    service = ""
    cutoff = r.mode["cutoff"]
    if req.mode == "online":
        p = build_provider()
        check_service(p)
        service = service_info(p)["id"]
        if not p.external or not req.acknowledge or req.serviceId != service:
            fail("ONLINE_OPT_IN_REQUIRED", "请明确确认当前在线服务；不能自动启用")
        if r.mode["mode"] in ("legacy", "local") and r.convs.count_messages(conversation_id) and not req.freshContext:
            fail("FRESH_CONTEXT_REQUIRED", "旧会话保留本地保护；请明确开启不携带旧历史的在线上下文")
        if req.freshContext:
            messages = r.convs.list_messages(conversation_id)
            cutoff = messages[-1]["seq"] if messages else 0
    r.store.set_mode(conversation_id, req.mode, service, cutoff)
    return state(conversation_id, request)


def preview(conversation_id: str, req: Preview, request: Request):
    r = router_for(conversation_id, request)
    known = {(x["materialId"], x["version"]) for x in ChatImportStore(r.convs).refs(conversation_id)}
    if any((x.get("materialId"), x.get("version")) not in known for x in req.materialRefs):
        fail("ATTACHMENT_NOT_LINKED", "请先把文件加入当前对话")
    try:
        return prepare_chat(r, req.content, depth=req.depth, mode=req.mode, material_refs=req.materialRefs,
                            local=req.localOnly, omit=req.omitSources, retry_user_id=req.retryUserMessageId, reply_assistance=req.replyAssistance,
                            request_id=req.requestId, charter_exception_id=req.charterExceptionId).preview
    except ProviderError as exc:
        from fastapi import HTTPException
        raise HTTPException(exc.status_code, {"code": exc.code, "detail": str(exc), "options": ["retry_online", "use_local"]}) from None


def grant(conversation_id: str, req: Grant, request: Request):
    r = router_for(conversation_id, request)
    p = r.store.get_preview(req.revision, r.cid)
    if not p:
        fail("PREVIEW_EXPIRED", "预览已过期，请重新核对")
    r.authorize(p, req.keys)
    return {"granted": req.keys}


def charter_exception(conversation_id: str, req: CharterException, request: Request):
    from .zhijun import charter_policy
    from .zhijun.provider import ChatRequest
    r = router_for(conversation_id, request)
    preview = r.store.get_preview(req.revision, r.cid)
    if not preview or not req.acknowledge:
        fail("CHARTER_EXCEPTION_REQUIRED", "请明确确认本次临时例外；旧预览或未确认请求不能生效")
    if r.mode != preview["mode"]:
        fail("ROUTE_CHANGED", "处理模式已变化，请重新核对本轮例外")
    provider = r.provider()
    if service_info(provider)["id"] != preview["service"]["id"]:
        fail("ONLINE_SERVICE_CHANGED", "接收服务已变化，请重新核对")
    fresh = r.prepare(preview["purpose"], ChatRequest(**preview["request"]),
                      [source["ref"] for source in preview["sources"]], provider, excluded=preview.get("excluded"))
    return charter_policy.authorize_exception(r, fresh, req.exceptionKey)


def revoke(conversation_id: str, req: Revoke, request: Request):
    r = router_for(conversation_id, request)
    r.store.revoke(r.scope, req.key)
    return {"revoked": True, "notice": "已停止后续使用；无法收回已经发送的内容"}


def policy_state(store, scope, service):
    p = store.policy(scope)
    return {**p, "active": p["enabled"] and p["service"] == (service or {}).get("id"),
            "serviceChanged": p["enabled"] and p["service"] != (service or {}).get("id")}


def set_default_consent(conversation_id: str, req: DefaultConsent, request: Request):
    r = router_for(conversation_id, request)
    policy = r.store.policy(r.scope)
    service, name = policy["service"], policy["serviceName"]
    if req.enabled:
        provider = build_provider()
        check_service(provider)
        info = service_info(provider)
        if not req.acknowledge or not provider.external or req.serviceId != info["id"]:
            fail("DEFAULT_CONSENT_REQUIRED", "请明确同意当前服务、适用用途，以及现在和今后所需的相关文字范围")
        service, name = info["id"], info["name"]
    from .zhijun.routing import PURPOSES
    try:
        r.store.set_policy(r.scope, enabled=req.enabled, service=service, service_name=name,
                           include_files=req.includeFiles if req.enabled else policy["includeFiles"],
                           include_charter=req.includeCharter if req.enabled else policy["includeCharter"],
                           purposes=list(PURPOSES) if req.enabled else policy["purposes"],
                           expected_revision=req.expectedRevision)
    except ValueError as exc:
        fail("DEFAULT_CONSENT_CHANGED", str(exc))
    return default_state(request) if conversation_id == "default" else state(conversation_id, request)


def audits(conversation_id: str, request: Request):
    r = router_for(conversation_id, request)
    import json
    with r.onto._connect() as db:
        rows = db.execute("SELECT * FROM routing_audits WHERE conversation_id=? ORDER BY id DESC LIMIT 50", (conversation_id,)).fetchall()
    return {"items": [{**dict(x), "sources": json.loads(x["sources_json"]), "usage": json.loads(x["usage_json"])} for x in rows]}


def default_state(request: Request):
    store = RoutingStore(OntologyStore.instance())
    try:
        provider = build_provider()
        check_service(provider)
        service, error = service_info(provider), ""
    except Exception as exc:
        service, error = None, str(exc)
    return {"mode": store.mode("default:" + _device_scope_of(request)), "service": service, "error": error,
            "handlingPreference": handling_state(store, _device_scope_of(request), service),
            "defaultAuthorization": policy_state(store, _device_scope_of(request), service),
            "pending": active_pending(router_for("default", request))}


class Resume(BaseModel):
    task: str = Field(max_length=100)
    localOnly: bool = False


def pending_preview(conversation_id: str, revision: str, request: Request):
    r = router_for(conversation_id, request)
    # Only an actual outstanding task may refresh an expired preview. Grants
    # still require the fresh version returned by prepare below.
    outstanding = active_pending(r)
    bound = any(item["preview_id"] == revision for item in outstanding)
    p = r.store.get_preview(revision, r.cid, include_expired=bound)
    if not p:
        fail("PREVIEW_EXPIRED", "后台预览已过期；请重新准备待办，由后台按当前内容再次核对，原消息仍保留")
    if p["purpose"] == "charter_draft" and not any(t["task_key"] == "charter_draft" for t in active_pending(r)):
        fail("TASK_CHANGED", "章程已确认，不再自动整理；如需修改，请从人生章程主动进入")
    from .zhijun.provider import ChatRequest
    # Refresh queued permissions without calling a model or accepting an old service.
    provider = r.provider()
    if service_info(provider) != p["service"] or r.mode != p["mode"]:
        fail("ROUTE_CHANGED", "处理模式或服务已变化，请重新准备待办；不会沿用旧服务或自动授权")
    sources = [source for old in p["sources"] for source in r.resolve(old["ref"])]
    r.check_lifecycle(sources)
    return r.prepare(p["purpose"], ChatRequest(**p["request"]), [s["ref"] for s in p["sources"]],
                     provider, excluded=p.get("excluded"), background=bound)


def resume(conversation_id: str, req: Resume, request: Request):
    r = router_for(conversation_id, request)
    outstanding = any(t["task_key"] == req.task for t in active_pending(r))
    if req.task.startswith("file_reply:"):
        if not outstanding:
            fail("TASK_CHANGED", "任务已恢复或不存在")
        imports = ChatImportStore(r.convs)
        batch = imports.get(req.task.split(":", 1)[1])
        if not batch or batch["conversation_id"] != r.cid:
            fail("TASK_CHANGED", "文件批次不可用")
        imports.update(batch["id"], "queued", local_only=req.localOnly)
        return {"state": "queued"}
    from .zhijun.routing import PURPOSES
    if req.task not in PURPOSES:
        fail("TASK_CHANGED", "未知后台任务")
    if req.task == "charter_draft" and not outstanding:
        fail("TASK_CHANGED", "章程编辑已结束；需要修改时请主动开始")
    if not r.store.conversation_jobs(r.cid, req.task):
        fail("TASK_CHANGED", "原任务不可用，请重新触发")
    job_ids = r.store.resume_jobs(r.cid, req.task, local_only=req.localOnly)
    return {"state": "queued", "jobIds": job_ids, "jobId": job_ids[0] if job_ids else None,
            "queuedCount": len(job_ids), "pendingCount": len(r.store.recoverable_jobs(r.cid, req.task))}


def set_default(req: Mode, request: Request):
    store = RoutingStore(OntologyStore.instance())
    owner = "default:" + _device_scope_of(request)
    if store.mode(owner)["revision"] != req.expectedRevision:
        fail("ROUTE_CHANGED", "设置已变化，请刷新")
    service_id = ""
    if req.mode == "online":
        p = build_provider()
        check_service(p)
        service_id = service_info(p)["id"]
        if not req.acknowledge or not p.external or req.serviceId != service_id:
            fail("ONLINE_OPT_IN_REQUIRED", "请明确确认新会话使用的在线服务")
    store.set_mode(owner, req.mode, service_id)
    return default_state(request)
