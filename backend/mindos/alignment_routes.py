"""Local authenticated calibration and explicit profile-service consent."""
from __future__ import annotations

from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .chat_imports import require_conversation, service_info
from .stores.alignment_store import AlignmentStore
from .stores.conversation_store import ConversationStore
from .stores.ontology_store import OntologyStore, OntologyConflictError, OntologyNotFoundError, OntologyError
from .uploads import _device_scope_of
from .zhijun import alignment
from .zhijun.provider import build_provider, ProviderError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Review(Strict):
    requestId: str = Field(min_length=16, max_length=100)
    expectedRevision: int = Field(ge=0)
    claimVersion: str = Field(min_length=64, max_length=64)
    evidenceVersion: str = Field(min_length=64, max_length=64)
    action: Literal["calibrate", "defer", "clear"]
    level: int | None = Field(default=None, ge=0, le=4, strict=True)
    framing: Literal["long_term", "context_only", "aspirational"] = "long_term"
    note: str = Field(default="", max_length=500)
    proposalId: str | None = Field(default=None, max_length=80)
    conversationId: str | None = Field(default=None, max_length=80)


class Proposal(Strict):
    conversationId: str = Field(min_length=1, max_length=80)
    messageId: str = Field(min_length=1, max_length=80)
    feedback: str = Field(default="", max_length=1000)


class Ref(Strict):
    claimId: str = Field(min_length=1, max_length=80)
    fingerprint: str = Field(min_length=64, max_length=64)


class Consent(Strict):
    serviceId: str | None = Field(default=None, max_length=100)
    refs: list[Ref] = Field(default_factory=list, max_length=50)
    localOnly: bool = False


def mapped(exc):
    code = 404 if isinstance(exc, OntologyNotFoundError) else 409 if isinstance(exc, OntologyConflictError) else 400
    return HTTPException(code, {"code": "ALIGNMENT_CONFLICT" if code == 409 else "ALIGNMENT_INVALID", "detail": str(exc)})


def review(claim_id: str, req: Review, request: Request):
    convs, onto = ConversationStore.instance(), OntologyStore.instance()
    existing = onto.get_claim(claim_id)
    if not existing or not alignment.visible(existing, convs, _device_scope_of(request)):
        raise HTTPException(404, "理解不存在")
    if req.conversationId:
        require_conversation(req.conversationId, _device_scope_of(request))
    try:
        claim = AlignmentStore(onto).review(claim_id, req.model_dump())
    except OntologyError as exc:
        raise mapped(exc) from None
    from .zhijun.jobs import enqueue_projection
    enqueue_projection(store=onto)
    if req.conversationId:
        # Metadata-only message: records provenance without copying a new deep
        # profile into ordinary chat text, summaries or extraction.
        mid = "alignment_" + req.requestId
        if not convs.get_message(mid):
            src = alignment.source(claim, convs, _device_scope_of(request))
            convs.append_message(req.conversationId, "system", "你更新了这条理解的自我贴合度。", message_id=mid,
                                 meta={"kind": "alignment_review", "alignmentSources": [src]})
        AlignmentStore(onto).status(req.conversationId, status="calibrated", detail="已按你的校准更新；记录事实仍然保留")
    return claim


def propose(claim_id: str, req: Proposal, request: Request):
    require_conversation(req.conversationId, _device_scope_of(request))
    onto, convs = OntologyStore.instance(), ConversationStore.instance()
    claim = onto.get_claim(claim_id)
    message = convs.get_message(req.messageId)
    if not claim or not alignment.visible(claim, convs, _device_scope_of(request)) or not message or message["conversationId"] != req.conversationId:
        raise HTTPException(404, "理解或会话消息不存在")
    # Explicitly supplied feedback is persisted as user evidence, not model text.
    if req.feedback:
        feedback_id = "alignment_feedback_" + alignment.digest([claim_id, req.model_dump()])[:24]
        if not convs.get_message(feedback_id):
            from .zhijun.routing import Router
            routing = Router(onto, convs, req.conversationId)
            ancestry = [s["ref"] for s in routing.resolve(routing.ref("claim", claim_id)) if s["key"] != "claim:" + claim_id]
            feedback = convs.append_message(req.conversationId, "user", req.feedback, message_id=feedback_id,
                meta={"kind": "alignment_feedback", "calibrationOf": claim_id, "routingSources": ancestry,
                      "routingOrigin": {"service": ""}, "localOnlyDerived": True})
            onto.add_evidence(claim_id, [{"kind": "conversation_turn", "conversation_id": req.conversationId,
                "message_id": feedback["id"], "quote": req.feedback, "stance": "background"}])
    AlignmentStore(onto).status(req.conversationId, status="queued", detail="校准提议已排队；按任务核对处理方和权限，你可以继续聊天")
    job_id = onto.enqueue_job("alignment", req.messageId, payload={**req.model_dump(), "claimId": claim_id},
                              input_hash=alignment.digest(req.model_dump()), priority=3)
    return {"state": "queued", "jobId": job_id}


def get_state(conversation_id: str, request: Request):
    require_conversation(conversation_id, _device_scope_of(request))
    onto, convs = OntologyStore.instance(), ConversationStore.instance()
    messages = convs.list_messages(conversation_id)
    query = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    refs = alignment.candidates(conversation_id, query, onto, convs, _device_scope_of(request))
    try:
        provider = build_provider()
        service = service_info(provider)
    except ProviderError:
        provider, service = None, None
    items = []
    for ref in refs:
        claim = onto.get_claim(ref["claimId"])
        blocked = not claim or claim["trustState"] != "confirmed" or claim["privacyLevel"] in ("sensitive", "restricted") or ref.get("unavailableEvidence", False)
        current = alignment.source(claim, convs, _device_scope_of(request)) if claim else None
        if current and current["claimVersion"] != ref["claimVersion"]:
            blocked = True
        items.append({**ref, "blocked": blocked, "historical": bool(current and current["fingerprint"] != ref["fingerprint"]),
                      "allowed": bool(provider and alignment.allowed(ref, provider, onto, convs, _device_scope_of(request)))})
    proposals = [c for c in onto.list_claims(trust_states=("confirmed",), limit=2000)
                 if (c["selfAlignment"].get("proposal") or {}).get("conversationId") == conversation_id and alignment.visible(c, convs, _device_scope_of(request))]
    # Even in case of job retries, each rendered turn has at most one prompt.
    proposals = list({c["selfAlignment"]["proposal"]["messageId"]: c for c in proposals}.values())
    return {"proposals": proposals, "sources": items, "service": service,
            "state": AlignmentStore(onto).status(conversation_id)}


def consent(conversation_id: str, req: Consent, request: Request):
    require_conversation(conversation_id, _device_scope_of(request))
    onto, convs = OntologyStore.instance(), ConversationStore.instance()
    store = AlignmentStore(onto)
    if req.localOnly:
        store.status(conversation_id, local_only=True)
        return get_state(conversation_id, request)
    try:
        provider = build_provider()
    except ProviderError as exc:
        raise HTTPException(409, str(exc)) from None
    service = service_info(provider)
    if not service["external"] or service["id"] != req.serviceId:
        raise HTTPException(409, "外部服务已改变，请重新查看授权内容")
    if not req.refs:
        raise HTTPException(400, "请选择要授权的画像")
    refs, current_tokens = [], {}
    known_history = {(r["claimId"], r["fingerprint"]): r for r in alignment.history_sources(conversation_id, convs)}
    from .stores.chat_import_store import ChatImportStore
    for item in req.refs:
        c = onto.get_claim(item.claimId)
        if not c or not alignment.visible(c, convs, _device_scope_of(request)) or c["trustState"] != "confirmed" or c["privacyLevel"] in ("sensitive", "restricted"):
            raise HTTPException(409, "这条理解不可外发")
        current = alignment.source(c, convs, _device_scope_of(request))
        current_tokens[c["id"]] = current["fingerprint"]
        ref = current if current["fingerprint"] == item.fingerprint else known_history.get((c["id"], item.fingerprint))
        if current["unavailableEvidence"]:
            raise HTTPException(409, "引用资料不可用，只能在本地查看已有校准，不能继续外发")
        if not ref or ref["claimVersion"] != current["claimVersion"]:
            raise HTTPException(409, "画像或证据版本已变化，请重新确认")
        for e in ref["evidence"]:
            material = e.get("materialRef")
            if material and not ChatImportStore(convs).allowed(material, service["id"], material["snapshotId"]):
                raise HTTPException(409, "涉及文件尚未授权；请先在文件讨论中授权对应文件，或仅本地处理")
        refs.append(ref)
    store.grant(refs, service["id"], current_tokens)
    store.status(conversation_id, local_only=False)
    return get_state(conversation_id, request)


def revoke(claim_id: str, request: Request):
    onto, convs = OntologyStore.instance(), ConversationStore.instance()
    claim = onto.get_claim(claim_id)
    if not claim or not alignment.visible(claim, convs, _device_scope_of(request)):
        raise HTTPException(404, "理解不存在")
    AlignmentStore(onto).revoke(claim_id)
    return {"revoked": True}


def register(router, writes):
    router.add_api_route("/claims/{claim_id}/alignment", review, methods=["POST"], dependencies=writes)
    router.add_api_route("/claims/{claim_id}/alignment/proposals", propose, methods=["POST"], dependencies=writes)
    router.add_api_route("/claims/{claim_id}/alignment/revoke", revoke, methods=["POST"], dependencies=writes)
    router.add_api_route("/alignment/conversations/{conversation_id}", get_state, methods=["GET"])
    router.add_api_route("/alignment/conversations/{conversation_id}/consent", consent, methods=["POST"], dependencies=writes)
