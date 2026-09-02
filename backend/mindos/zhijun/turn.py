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
from . import deliberate, extract, jobs
from .gate import conversation_locks, provider_gate
from .provider import ChatProvider, ChatRequest, Done, ProviderError, TextDelta, Usage, build_provider

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


def _charter() -> dict | None:
    try:
        from ..stores.growth_store import GrowthStore

        return GrowthStore.instance().current_charter()
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


def run_turn(
    conversation_id: str,
    content: str,
    *,
    depth: str = "brief",
    mode: str = "chat",
    provider: ChatProvider | None = None,
    conv_store: ConversationStore | None = None,
    ontology: OntologyStore | None = None,
) -> Iterator[tuple[str, dict]]:
    conv_store = conv_store or ConversationStore.instance()
    ontology = ontology or OntologyStore.instance()
    content = (content or "").strip()
    depth = "deep" if depth == "deep" else "brief"
    if mode not in ("chat", "deliberate"):
        raise TurnError(400, "BAD_MODE", "mode 只能是 chat 或 deliberate")
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
        try:
            provider = provider or build_provider()
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
            )
        finally:
            provider_gate.release(channel)
    finally:
        conversation_locks.release(conversation_id)


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
) -> Iterator[tuple[str, dict]]:
    conversation_id = conversation["id"]
    user_message = conv_store.append_message(conversation_id, "user", content)
    user_turns = conv_store.count_messages(conversation_id, role="user")
    decision = _decision_for(conversation)
    assembled = context_module.assemble(
        conversation=conversation,
        user_text=content,
        depth=depth,
        provider=provider,
        ontology=ontology,
        recent_messages=conv_store.recent_messages(conversation_id, context_module.RECENT_TURNS),
        user_turns=user_turns,
        summary=conv_store.latest_summary(conversation_id),
        charter=_charter(),
        turn_mode=mode,
        decision=decision,
    )
    assistant_id = f"msg_{uuid.uuid4().hex[:12]}"
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
        },
    )
    yield ("provenance", assembled.provenance)

    request = ChatRequest(
        system=assembled.system,
        messages=assembled.messages,
        max_tokens=DEEP_MAX_TOKENS if depth == "deep" else BRIEF_MAX_TOKENS,
        temperature=0.4,
        effort="medium" if depth == "deep" else "low",
        debug=assembled.debug,
    )
    buffer: list[str] = []
    usage: dict | None = None
    stop_reason: str | None = None
    status = "complete"
    error_payload: dict | None = None
    finalized = False

    def _persist(final_status: str, text: str) -> dict:
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
            meta={"depth": depth, "stopReason": stop_reason},
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
            extraction_provider=provider.name if jobs.extraction_enabled() else None,
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

    if mode == "deliberate":
        # 商量模式：同步整理判断草稿（同一通道再调一次，不产生新的出设备），失败不影响本轮。
        try:
            draft, changed = deliberate.run_draft(provider=provider, conv_store=conv_store, conversation_id=conversation_id, message_id=assistant_id)
            yield (
                "decision_draft",
                {"draftId": draft["id"], "revision": draft["revision"], "status": draft["status"], "fields": draft["fields"], "changedFields": changed},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("判断草稿整理失败：%s", type(exc).__name__)

    ok, reason = extract.should_extract(content)
    if ok and jobs.extraction_enabled():
        job_id = jobs.enqueue_extraction(conversation_id, user_message["id"], store=ontology)
        yield ("extraction", {"state": "queued" if job_id else "skipped", "jobId": job_id, "reason": None if job_id else "duplicate"})
    else:
        yield ("extraction", {"state": "skipped", "jobId": None, "reason": reason if ok is False else "disabled"})
    yield (
        "message_done",
        {"messageId": assistant_id, "status": "complete", "usage": usage, "receiptId": assistant_id, "seq": message["seq"]},
    )
