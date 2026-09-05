"""一轮对话：门 → 落用户消息 → 组装上下文 → 流式生成 → 落助手消息与回执 → 入队抽取。

产出 ``(event_name, data)`` 序列，由路由层编码为 SSE。事件顺序固定：
``meta → provenance → token* → extraction → message_done``；出错时以 ``error`` 收尾。
开始流式前的失败（会话不存在 / 并发冲突 / 通道不可用）以 ``TurnError`` 抛出，路由层映射为 HTTP 状态。
"""
from __future__ import annotations

import logging
import uuid
from typing import Iterator

from ..stores.conversation_store import ConversationStore
from ..stores.ontology_store import OntologyStore
from . import context as context_module
from . import deliberate, extract, jobs, memory
from .gate import conversation_locks, provider_gate
from .provider import ONBOARDING_QUESTIONS, ChatProvider, ChatRequest, Done, ProviderError, TextDelta, Usage, build_provider

logger = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 4000
BRIEF_MAX_TOKENS = 1024
DEEP_MAX_TOKENS = 4096
INTERACTIVE_GATE_TIMEOUT = 2.0


class TurnError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _charter(scope="global") -> dict | None:
    try:
        from ..stores.growth_store import GrowthStore

        return GrowthStore.instance().current_charter(scope=scope)
    except Exception:  # noqa: BLE001 - 章程缺失不阻塞对话
        return None


def _decision_for(conversation: dict) -> dict | None:
    decision_id = conversation.get("decisionId")
    if not decision_id:
        return None
    try:
        from ..stores.growth_store import GrowthStore

        return GrowthStore.instance().get_decision(decision_id)
    except Exception:  # noqa: BLE001
        return None


def _onboarding_turn_number(conv_store: ConversationStore, conversation_id: str, user_turns: int) -> int:
    """新流程由 assistant 直接问第 1 问；旧流程由一条伪 user 消息触发，二者都兼容。"""
    messages = conv_store.list_messages(conversation_id)
    has_opening = any(
        item.get("role") == "assistant" and (item.get("meta") or {}).get("kind") == "onboarding_open"
        for item in messages
    )
    controls = sum(item.get("role") == "user" and (item.get("meta") or {}).get("replyAssistance", {}).get("kind") == "control" for item in messages)
    return user_turns - controls + (1 if has_opening else 0)


def run_turn(
    conversation_id: str,
    content: str,
    *,
    depth: str = "brief",
    mode: str = "chat",
    provider: ChatProvider | None = None,
    conv_store: ConversationStore | None = None,
    ontology: OntologyStore | None = None,
    material_refs: list[dict] | None = None,
    local_only: bool = False,
    existing_user_message_id: str | None = None,
    import_id: str | None = None,
    route_revision: str | None = None,
    omit_sources: bool = False,
    request_id: str | None = None,
    retry_user_id: str | None = None,
    reply_assistance=None,
    charter_exception_id: str | None = None,
) -> Iterator[tuple[str, dict]]:
    conv_store = conv_store or ConversationStore.instance()
    ontology = ontology or OntologyStore.instance()
    content = (content or "").strip()
    depth = "deep" if depth == "deep" else "brief"
    if mode not in ("chat", "deliberate"):
        raise TurnError(400, "BAD_MODE", "mode 只能是 chat 或 deliberate")
    if not content and material_refs:
        content = "请简短介绍这些文件的内容，然后问我想继续了解什么。"
    if not content:
        raise TurnError(400, "EMPTY_CONTENT", "内容不能为空")
    if len(content) > MAX_CONTENT_CHARS:
        raise TurnError(400, "CONTENT_TOO_LONG", f"内容不能超过 {MAX_CONTENT_CHARS} 字")
    conversation = conv_store.get_conversation(conversation_id)
    if conversation is None:
        raise TurnError(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    if not conversation_locks.acquire(conversation_id):
        raise TurnError(409, "TURN_IN_FLIGHT", "这段对话还有一轮正在生成，请稍等")
    try:
        from ..stores.routing_store import RoutingStore
        import os
        if provider is None and not route_revision and not request_id and os.environ.get("ZHIJUN_PROVIDER") == "fake" and RoutingStore(ontology).mode(conversation_id)["mode"] == "legacy":
            try:
                provider = build_provider()
            except ProviderError as exc:
                raise TurnError(exc.status_code, exc.code, str(exc)) from exc
        from .charter_policy import scope_policy
        from .alignment import scope_for
        has_charter = bool(scope_policy(scope_for(conversation_id, conv_store)).get("charterId"))
        if has_charter or reply_assistance is not None or provider is None or route_revision or request_id or RoutingStore(ontology).mode(conversation_id)["mode"] != "legacy":
            yield from _run_routed(conversation, content, depth, mode, ontology, conv_store,
                                   material_refs or [], local_only, route_revision, omit_sources,
                                   request_id, retry_user_id or existing_user_message_id, import_id, provider, reply_assistance, charter_exception_id)
            return
        try:
            from ..chat_imports import choose_provider, protected_conversation, local_provider
            from ..stores.alignment_store import AlignmentStore
            local_only = local_only or bool(AlignmentStore(ontology).status(conversation_id)["local_only"])
            provider = provider or (local_provider() if local_only else build_provider())
            protected = protected_conversation(conversation_id, conv_store)
            if material_refs or protected:
                provider = choose_provider(conversation_id, material_refs or [], provider,
                                           local_only=local_only, conversations=conv_store)
            from . import alignment
            provider = alignment.select_provider(conversation_id, content, provider, ontology, conv_store)
        except ProviderError as exc:
            raise TurnError(exc.status_code, exc.code, str(exc)) from exc
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, INTERACTIVE_GATE_TIMEOUT):
            raise TurnError(429, "PROVIDER_BUSY", "模型正忙，请稍后再试")
        try:
            yield from _run_locked(
                conversation=conversation,
                content=content,
                depth=depth,
                mode=mode,
                provider=provider,
                channel=channel,
                conv_store=conv_store,
                ontology=ontology,
                material_refs=material_refs or [],
                protected=protected,
                existing_user_message_id=existing_user_message_id,
                import_id=import_id,
            )
        finally:
            provider_gate.release(channel)
    finally:
        conversation_locks.release(conversation_id)


def _run_routed(conversation, content, depth, mode, ontology, conv_store, refs, local_only,
                revision, omit, request_id, retry_id, import_id, provider, reply_assistance=None, charter_exception_id=None):
    from fastapi import HTTPException
    from ..stores.alignment_store import digest
    from .routing import Router, GuardedProvider, prepare_chat, fail
    from . import context_lookup
    cid = conversation["id"]
    user_id = retry_id or ("msg_route_" + digest([cid, request_id])[:24] if request_id else None)
    old_user = conv_store.get_message(user_id) if user_id else None
    if old_user and (old_user["conversationId"] != cid or old_user["role"] != "user" or (old_user["content"] != content and not import_id)):
        fail("RETRY_CHANGED", "重试必须引用原来的用户消息；新内容请另发一轮")
    if retry_id and not old_user:
        fail("MESSAGE_NOT_FOUND", "重试消息已删除")
    assistant_id = "msg_reply_" + import_id if import_id else ("msg_route_reply_" + digest([cid, user_id])[:24] if user_id else "msg_" + uuid.uuid4().hex[:12])
    previous = conv_store.get_message(assistant_id)
    if previous and previous["status"] == "complete":
        yield "meta", {"conversationId": cid, "messageId": assistant_id, "userMessageId": user_id,
                       "provider": previous["provider"], "model": previous["model"], "external": previous["external"], "replayed": True}
        yield "provenance", (previous.get("meta") or {}).get("routingProvenance", {})
        yield "token", {"t": previous["content"]}
        yield "message_done", {"messageId": assistant_id, "status": "complete", "seq": previous["seq"]}
        return
    router = Router(ontology, conv_store, cid, provider=provider)
    from .reply_assistance import resolve_input
    expression, expression_refs = resolve_input(router, reply_assistance, content, retry_user_id=user_id if old_user else None)
    plan = prepare_chat(router, content, depth=depth, mode=mode, material_refs=refs,
                        local=local_only, omit=omit, retry_user_id=user_id if old_user else None, reply_assistance=reply_assistance,
                        request_id=request_id, charter_exception_id=charter_exception_id)
    request = ChatRequest(**plan.preview["request"])
    guarded = GuardedProvider(router, plan.provider, "chat", plan.refs, revision=revision,
                              excluded=plan.preview["excluded"])
    guarded.check(request)  # Before writing an optimistic message / acquiring model slot.
    channel = "external" if guarded.external else "local"
    if not provider_gate.acquire(channel, INTERACTIVE_GATE_TIMEOUT):
        raise TurnError(429, "PROVIDER_BUSY", "模型正忙，请稍后重试；没有切换服务")
    try:
        current_refs = [] if omit else refs
        origin = {"service": plan.preview["service"]["id"] if guarded.external else "", "modeRevision": router.mode["revision"]}
        if old_user and import_id:
            old_user = conv_store.update_message(old_user["id"], meta={**(old_user.get("meta") or {}),
                "materialRefs": current_refs, "routingOrigin": origin, "routingSources": expression_refs})
        user = old_user or conv_store.append_message(cid, "user", content, message_id=user_id,
                    meta={"materialRefs": current_refs, "routingOrigin": origin, "routingSources": expression_refs,
                          **({"replyAssistance": expression} if expression else {})})
        user_id = user["id"]
        def receipt_meta():
            source_refs = [s["ref"] for s in plan.preview["sources"]]
            source_refs.extend(s["ref"] for s in router.resolve(router.ref("message", user_id)))
            return {"routingSources": list({(s["kind"], s["id"], s.get("version")): s for s in source_refs}.values()),
                "charterBasis": plan.preview.get("charterBasis"),
                "onboardingTopic": request.debug.get("onboardingTopic"),
                "charterTopic": request.debug.get("charterTopic"),
                "routingOrigin": origin,
                "localOnlyDerived": not guarded.external or any(s["kind"] != "message" for s in source_refs),
                "routingProvenance": {**plan.assembled.provenance, "contextPlan": {
                    **plan.assembled.provenance.get("contextPlan", {}), "delivery": "prepared", "providedRefs": [], "citedRefs": []}}, "materialRefs": current_refs,
                "replyTo": user_id, "importId": import_id, "depth": depth, "turnMode": mode,
                "requestId": request_id, "contextStage": plan.assembled.provenance.get("contextPlan", {}).get("stage", "initial")}
        meta = receipt_meta()
        if not previous:
            conv_store.append_message(cid, "assistant", "生成中断时可重试，不会重复发送用户消息。", message_id=assistant_id,
                                      status="aborted", provider=guarded.name, model=guarded.model, external=guarded.external, meta=meta)
        else:
            conv_store.update_message(assistant_id, status="aborted", meta=meta)
        yield "meta", {"conversationId": cid, "messageId": assistant_id, "userMessageId": user_id,
                       "provider": guarded.name, "model": guarded.model, "external": guarded.external,
                       "mode": conversation.get("mode", "chat"), "turnMode": mode, "depth": depth,
                       "routing": plan.assembled.provenance["routing"]}
        def pending_provenance():
            return {**plan.assembled.provenance, "contextPlan": {
                **plan.assembled.provenance.get("contextPlan", {}), "delivery": "prepared", "providedRefs": [], "citedRefs": []}}
        yield "provenance", pending_provenance()
        buffer, usage, status, err = [], None, "aborted", None
        answer_started = False
        try:
            if context_lookup.eligible(plan, content, depth, mode, request_id=request_id, omit=omit,
                                       charter_exception_id=charter_exception_id):
                yield "context_phase", {"stage": "lookup", "message": "正在补查相关经历与资料"}
                lookup_result = context_lookup.run(plan, request_id=request_id,
                    fingerprint_value=plan.assembled.provenance["contextPlan"]["lookupFingerprint"])
                meta["contextStage"] = lookup_result["stage"]
                next_plan = prepare_chat(router, content, depth=depth, mode=mode, material_refs=refs,
                    local=local_only, omit=omit, retry_user_id=user_id, reply_assistance=reply_assistance,
                    request_id=request_id, charter_exception_id=charter_exception_id)
                # Reassembly must not silently adopt a changed default model or
                # grant while the optional lookup was completing. Check the
                # original binding before replacing it with the rebuilt plan.
                guarded.check(request)
                plan = next_plan
                request = ChatRequest(**plan.preview["request"])
                meta = receipt_meta()
                if plan.provider.external != guarded.external:
                    fail("ROUTE_CHANGED", "补查资料需要改用你选择的处理方式，请核对后继续", plan.preview)
                guarded = GuardedProvider(router, plan.provider, "chat", plan.refs,
                    revision=plan.preview["revision"], excluded=plan.preview["excluded"])
                guarded.check(request)
                if lookup_result["state"] == "unavailable":
                    yield "context_phase", {"stage": "lookup_unavailable", "message": lookup_result["notice"]}
                yield "provenance", pending_provenance()
            for event in guarded.stream(request):
                if isinstance(event, TextDelta) and event.text:
                    buffer.append(event.text)
                    yield "token", {"t": event.text}
                elif isinstance(event, Usage):
                    usage = {"inputTokens": event.input_tokens, "outputTokens": event.output_tokens}
            guarded.assert_current()
            status = "complete" if "".join(buffer).strip() else "error"
            if status == "error":
                err = {"code": "EMPTY_REPLY", "message": "模型没有返回内容，可重试在线或改用本地", "retryable": True}
        except (ProviderError, HTTPException, ValueError) as exc:
            status = "error"
            detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else {}
            err = {"code": detail.get("code", getattr(exc, "code", "CONTEXT_LOOKUP_FAILED")),
                   "message": detail.get("detail") or (str(exc) if isinstance(exc, ProviderError) else "补查暂未完成，原消息已保留，可重试"),
                   "retryable": True}
            if detail.get("preview"):
                err["preview"] = detail["preview"]
            if meta["contextStage"] == "supplemented":
                err["stage"] = "supplemented"
                meta["contextPending"] = {"code": err["code"], "stage": "supplemented"}
        finally:
            answer_started = guarded.dispatched
            context = context_lookup.citation_receipt(plan.assembled.provenance.get("contextPlan"), "".join(buffer))
            context["delivery"] = "provided" if answer_started else "awaiting_authorization" if err and err.get("preview") else "paused"
            if not answer_started:
                context["providedRefs"], context["citedRefs"] = [], []
            plan.assembled.provenance["contextPlan"] = context
            meta["routingProvenance"] = plan.assembled.provenance
            message = conv_store.update_message(assistant_id, content="".join(buffer) or (err or {}).get("message", "生成已暂停，可重试"),
                        status=status, usage=usage, meta=meta, provider=guarded.name, model=guarded.model, external=guarded.external)
            a = plan.assembled
            conv_store.save_receipt(message_id=assistant_id, conversation_id=cid, provider=guarded.name, model=guarded.model,
                external=guarded.external, confirmed_claim_ids=a.confirmed_ids if answer_started else [], working_claim_ids=a.working_ids if answer_started else [],
                material_chunk_keys=a.material_chunk_keys if answer_started else [], retracted_notice_count=0, prompt_chars=a.prompt_chars if answer_started else 0)
        if status != "complete":
            yield "provenance", plan.assembled.provenance
            yield "error", {**(err or {}), "messageId": assistant_id, "userMessageId": user_id,
                            "requestId": request_id,
                            "options": ["retry_online", "use_local"]}
            return
        yield "provenance", plan.assembled.provenance
        if mode == "deliberate":
            job_id = jobs.enqueue_draft(cid, assistant_id, store=ontology)
            yield "decision_draft", {"state": "queued", "jobId": job_id, "draftId": None, "revision": None, "status": "draft", "fields": None, "changedFields": []}
        if not current_refs and (not expression or expression["kind"] != "control"):
            from .charter import enqueue as enqueue_charter
            enqueue_charter(cid, user_id, content, ontology=ontology, local_only=not guarded.external)
        memory_allowed = memory.extraction_allowed(ontology, conv_store, cid, content)
        preceding = [m for m in conv_store.list_messages(cid) if m["role"] == "assistant" and m["seq"] < conv_store.get_message(user_id)["seq"] and m["status"] == "complete"]
        extraction_ok, extraction_reason = extract.should_extract(content, preceding[-1]["content"] if preceding else None)
        if not current_refs and (not expression or expression["kind"] != "control") and jobs.extraction_enabled() and extraction_ok and memory_allowed:
            job_id = jobs.enqueue_extraction(cid, user_id, store=ontology)
            yield "extraction", {"state": "queued", "jobId": job_id}
        else:
            yield "extraction", {"state": "skipped", "reason": "file_discussion" if current_refs else "memory_policy" if not memory_allowed else extraction_reason if not extraction_ok else "disabled", "jobId": None}
        yield "message_done", {"messageId": assistant_id, "status": "complete", "usage": usage, "receiptId": assistant_id, "seq": message["seq"]}
    finally:
        provider_gate.release(channel)


def _run_locked(
    *,
    conversation: dict,
    content: str,
    depth: str,
    mode: str,
    provider: ChatProvider,
    channel: str,
    conv_store: ConversationStore,
    ontology: OntologyStore,
    material_refs: list[dict] | None = None,
    protected: bool = False,
    existing_user_message_id: str | None = None,
    import_id: str | None = None,
) -> Iterator[tuple[str, dict]]:
    from . import alignment
    conversation_id = conversation["id"]
    user_message = conv_store.get_message(existing_user_message_id) if existing_user_message_id else None
    if existing_user_message_id and (not user_message or user_message["conversationId"] != conversation_id):
        raise TurnError(404, "MESSAGE_NOT_FOUND", "原文件消息不存在")
    user_message = user_message or conv_store.append_message(conversation_id, "user", content, meta={"materialRefs": material_refs or []})
    user_turns = conv_store.count_messages(conversation_id, role="user")
    onboarding_turn = _onboarding_turn_number(conv_store, conversation_id, user_turns) if conversation.get("mode") == "onboarding" else None
    decision = _decision_for(conversation)
    past_decisions: list[dict] = []
    # 商量 / 回访必带；普通聊天里只要这句话值得记（should_extract 通过）也带上——纯词面匹配，无模型开销。
    if mode == "deliberate" or conversation.get("mode") == "review" or extract.should_extract(content)[0]:
        try:
            from .history import similar_decisions

            past_decisions = similar_decisions(content, k=3, exclude_id=conversation.get("decisionId"))
        except Exception:  # noqa: BLE001 - 历史缺失不阻塞对话
            past_decisions = []
    recent = conv_store.recent_messages(conversation_id, context_module.RECENT_TURNS)
    if existing_user_message_id:
        # A delayed import answers its original request, not the last user message.
        recent = [m for m in recent if m["id"] != "msg_reply_" + str(import_id)]
        recent.append({"role": "user", "content": content})
    from ..stores.chat_import_store import ChatImportStore
    assembled = context_module.assemble(
        conversation=conversation,
        user_text=content,
        depth=depth,
        provider=provider,
        ontology=ontology,
        recent_messages=recent,
        user_turns=onboarding_turn or user_turns,
        summary=None if protected or alignment.protected(conversation_id, conv_store, ontology) else conv_store.latest_summary(conversation_id),
        # The old un-routed API cannot authorize deep-profile content.
        charter=None if provider.external else _charter(alignment.scope_for(conversation_id, conv_store)),
        turn_mode=mode,
        decision=decision,
        past_decisions=past_decisions,
        material_refs=material_refs or [],
        device_scope=ChatImportStore(conv_store).scope(conversation_id) or "global",
        conversation_store=conv_store,
    )
    from . import alignment
    if provider.external:
        from .source_policy import SourcePolicy
        policy = SourcePolicy(ontology, conv_store)
        if any(policy.claim_local(ontology.get_claim(cid)) for cid in assembled.confirmed_ids + assembled.working_ids):
            raise TurnError(409, "SOURCE_POLICY_CHANGED", "引用来源的隐私状态已变化，请重试；受保护内容不会外发")
        scope = alignment.scope_for(conversation_id, conv_store)
        outgoing = assembled.provenance.get("alignmentSources", []) + alignment.history_sources(conversation_id, conv_store)
        if any(not alignment.allowed(r, provider, ontology, conv_store, scope) for r in outgoing):
            raise TurnError(409, "ALIGNMENT_CONSENT_REQUIRED", "画像或授权已变化，请重试使用本地模型或重新授权")
    if provider.external and material_refs:
        from ..chat_imports import service_info
        privacy = ChatImportStore(conv_store)
        service = service_info(provider)
        for source in assembled.provenance["materials"]:
            if source.get("snapshotId") and not privacy.allowed(source, service["id"], source["snapshotId"]):
                raise TurnError(409, "ATTACHMENT_CONSENT_REQUIRED", "文件解析内容已变化，请重新确认外发授权")
    assistant_id = "msg_reply_" + import_id if import_id else f"msg_{uuid.uuid4().hex[:12]}"
    yield (
        "meta",
        {
            "messageId": assistant_id,
            "userMessageId": user_message["id"],
            "conversationId": conversation_id,
            "provider": provider.name,
            "model": provider.model,
            "external": bool(provider.external),
            "mode": conversation.get("mode") or "chat",
            "turnMode": mode,
            "depth": depth,
            "decisionId": conversation.get("decisionId"),
            # 建档：本轮是第几问（1–7），问完后为 8；前端据此高亮本体图对应扇面
            "onboardingStep": onboarding_turn,
        },
    )
    yield ("provenance", assembled.provenance)

    request = ChatRequest(
        system=assembled.system,
        messages=assembled.messages,
        max_tokens=DEEP_MAX_TOKENS if (depth == "deep" or mode == "deliberate") else BRIEF_MAX_TOKENS,
        temperature=0.4,
        # 商量 / 回访 / 深入是需要推理的轮次：effort 提到 medium。
        effort="medium" if (depth == "deep" or mode == "deliberate" or conversation.get("mode") == "review") else "low",
        debug=assembled.debug,
    )
    buffer: list[str] = []
    usage: dict | None = None
    stop_reason: str | None = None
    status = "complete"
    error_payload: dict | None = None
    finalized = False

    def _persist(final_status: str, text: str) -> dict:
        meta = {"depth": depth, "stopReason": stop_reason, "materialRefs": material_refs or [],
                "alignmentSources": assembled.provenance.get("alignmentSources", []),
                "localOnlyDerived": assembled.provenance.get("localOnlyDerived", False),
                "importId": import_id, "replyTo": existing_user_message_id,
                "attachmentProvenance": assembled.provenance if material_refs else None}
        existing = conv_store.get_message(assistant_id)
        if existing:
            message = conv_store.update_message(assistant_id, content=text, status=final_status, usage=usage,
                                                meta=meta, provider=provider.name, model=provider.model, external=bool(provider.external))
        else:
            message = conv_store.append_message(
                conversation_id,
                "assistant",
                text,
                message_id=assistant_id,
                status=final_status,
                provider=provider.name,
                model=provider.model,
                external=bool(provider.external),
                usage=usage,
                meta=meta,
            )
        conv_store.save_receipt(
            message_id=assistant_id,
            conversation_id=conversation_id,
            provider=provider.name,
            model=provider.model,
            external=bool(provider.external),
            confirmed_claim_ids=assembled.confirmed_ids,
            working_claim_ids=assembled.working_ids,
            material_chunk_keys=assembled.material_chunk_keys,
            retracted_notice_count=assembled.retracted_count,
            prompt_chars=assembled.prompt_chars,
            extraction_provider=provider.name if jobs.extraction_enabled() and not protected else None,
        )
        return message

    try:
        try:
            for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    if event.text:
                        buffer.append(event.text)
                        yield ("token", {"t": event.text})
                elif isinstance(event, Usage):
                    usage = {"inputTokens": event.input_tokens, "outputTokens": event.output_tokens}
                elif isinstance(event, Done):
                    stop_reason = event.stop_reason
        except ProviderError as exc:
            status = "error"
            error_payload = {"code": exc.code, "message": str(exc), "retryable": bool(exc.retryable)}
    except GeneratorExit:
        # 客户端中断：已生成的文本以 aborted 落库，不再产出事件。
        if not finalized:
            finalized = True
            _persist("aborted", "".join(buffer))
        raise

    text = "".join(buffer).strip()
    if status == "complete" and stop_reason == "refusal" and not text:
        status = "error"
        error_payload = {"code": "REFUSAL", "message": "模型拒绝了这次请求", "retryable": False}
    if status == "complete" and not text:
        status = "error"
        error_payload = {"code": "EMPTY_REPLY", "message": "模型没有返回内容", "retryable": True}

    finalized = True
    message = _persist(status, text if text else (error_payload or {}).get("message", ""))

    if status != "complete":
        yield ("error", {**(error_payload or {"code": "UNKNOWN", "message": "生成失败", "retryable": True}), "messageId": assistant_id})
        return

    alignment_private = alignment.protected(conversation_id, conv_store, ontology)
    # Automatic calibration follows newly admitted extraction results in the
    # worker, never every reply. Explicit calibration endpoints stay independent.
    if protected or alignment_private:
        # File discussions are not personal assertions. Do not feed their replies
        # into global extraction, summaries or decision drafts in the background.
        yield ("extraction", {"state": "skipped", "jobId": None, "reason": "private_profile" if alignment_private else "file_discussion"})
        yield ("message_done", {"messageId": assistant_id, "status": "complete", "usage": usage, "receiptId": assistant_id, "seq": message["seq"]})
        return

    if mode == "deliberate":
        # 商量模式：演示模型同步整理草稿；真实模型（推理型要几十秒）改为后台任务，前端轮询 GET /decision-draft。
        if provider.name == "fake":
            try:
                draft, changed = deliberate.run_draft(provider=provider, conv_store=conv_store, conversation_id=conversation_id, message_id=assistant_id)
                yield (
                    "decision_draft",
                    {"state": "ready", "draftId": draft["id"], "revision": draft["revision"], "status": draft["status"], "fields": draft["fields"], "changedFields": changed},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("判断草稿整理失败：%s", type(exc).__name__)
        else:
            job_id = jobs.enqueue_draft(conversation_id, assistant_id, store=ontology)
            yield ("decision_draft", {"state": "queued", "jobId": job_id, "draftId": None, "revision": None, "status": "draft", "fields": None, "changedFields": []})

    if conversation.get("mode") == "onboarding" and (onboarding_turn or 0) > len(ONBOARDING_QUESTIONS) and memory.automatic_allowed(ontology, conv_store, conversation_id):
        try:
            jobs.enqueue_first_observation(conversation_id, assistant_id, store=ontology)
        except Exception:  # noqa: BLE001
            pass

    preceding = [m for m in conv_store.list_messages(conversation_id) if m["role"] == "assistant" and m["seq"] < user_message["seq"] and m["status"] == "complete"]
    ok, reason = extract.should_extract(content, preceding[-1]["content"] if preceding else None)
    memory_allowed = memory.extraction_allowed(ontology, conv_store, conversation_id, content)
    if ok and jobs.extraction_enabled() and memory_allowed:
        job_id = jobs.enqueue_extraction(conversation_id, user_message["id"], store=ontology)
        yield ("extraction", {"state": "queued" if job_id else "skipped", "jobId": job_id, "reason": None if job_id else "duplicate"})
    else:
        yield ("extraction", {"state": "skipped", "jobId": None, "reason": reason if ok is False else "memory_policy" if not memory_allowed else "disabled"})
    yield (
        "message_done",
        {"messageId": assistant_id, "status": "complete", "usage": usage, "receiptId": assistant_id, "seq": message["seq"]},
    )
