"""本体后台 worker：单守护线程，租约领取，处理抽取 / 摘要 / 投影任务。

沿用 model_job_worker 的模式：启动先回收过期租约，再循环领取；失败按 transient / business 分类，
transient 有限重试。交互轮次不等待本 worker；前端通过 inbox 轮询看到新候选。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from ..stores.conversation_store import ConversationStore
from ..stores.ontology_store import OntologyStore
from . import extract, projection
from .gate import provider_gate
from .provider import ProviderError, build_provider

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 120.0
_IDLE_POLL_SECONDS = 1.0
_SUMMARY_EVERY_TURNS = 8


def extraction_enabled() -> bool:
    return os.environ.get("ZHIJUN_EXTRACTION", "1").strip().lower() not in ("0", "false", "no")


def enqueue_extraction(conversation_id: str, message_id: str, *, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job(
        "extract_turn",
        message_id,
        payload={"conversationId": conversation_id, "messageId": message_id},
        priority=5,
    )


def enqueue_projection(*, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("project", "profile", payload={}, priority=0)


def enqueue_summary(conversation_id: str, *, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("summarize_conversation", conversation_id, payload={"conversationId": conversation_id}, priority=1)


def enqueue_alignment(conversation_id: str, message_id: str, query: str, *, store=None):
    store = store or OntologyStore.instance()
    return store.enqueue_job("alignment", message_id, payload={"conversationId": conversation_id,
        "messageId": message_id, "query": query}, priority=3)


def _extractive_summary(messages: list[dict], max_chars: int = 400) -> tuple[str, list[str]]:
    """无模型的抽取式摘要：取用户消息要点，供本地简版上下文使用。"""
    points: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        if (message.get("meta") or {}).get("replyAssistance"):
            continue
        text = (message.get("content") or "").strip().replace("\n", " ")
        if len(text) >= 6:
            points.append(text[:80])
    summary = "；".join(points)[:max_chars]
    return summary, points[-8:]


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "open_loops": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "themes", "open_loops"],
    "additionalProperties": False,
}
_SUMMARY_SYSTEM = """你是知君的对话整理助手。把这段对话整理成：summary（≤ 200 字，只记事实、决定、偏好、待办）、themes（用户反复提到或在意的主题，≤ 6 条短语）、open_loops（用户说要做还没做的事，≤ 4 条）。不要编造。只输出 JSON。"""


def _model_summary(messages: list[dict], provider=None) -> tuple[str, list[str], str]:
    """有真实模型时用模型出主题与待办；否则抽取式。返回 (summary, key_points, generated_by)。"""
    user_texts = [m["content"] for m in messages if m.get("role") == "user" and (m.get("content") or "").strip()]
    try:
        provider = provider or build_provider()
    except ProviderError:
        provider = None
    if provider is None or provider.name == "fake":
        summary, points = _extractive_summary(messages)
        return summary, points, "extractive"
    from .provider import ChatRequest

    transcript = "\n".join(f"{'用户（AI 辅助表达，不作为独立人格证据）' if (m.get('meta') or {}).get('replyAssistance') else '用户' if m['role'] == 'user' else '知君'}：{(m.get('content') or '').strip()[:300]}" for m in messages[-24:] if m.get("role") in ("user", "assistant") and (m.get("meta") or {}).get("replyAssistance", {}).get("kind") != "control")
    from .routing import GuardedProvider
    if isinstance(provider, GuardedProvider):
        provider.refs = [provider.router.ref("message", m["id"]) for m in messages[-24:] if m.get("role") in ("user", "assistant")]
    request = ChatRequest(system=_SUMMARY_SYSTEM, messages=[{"role": "user", "content": transcript}], max_tokens=800, temperature=0.0, json_schema=_SUMMARY_SCHEMA, effort="low", debug={"task": "summary", "userTexts": user_texts})
    channel = "external" if provider.external else "local"
    if not provider_gate.acquire(channel, timeout=60.0, background=True):
        raise ProviderError("模型通道繁忙，摘要已暂停", code="PROVIDER_BUSY")
    try:
        raw = provider.complete_json(request)
    finally:
        provider_gate.release(channel)
    themes = [str(t).strip()[:40] for t in (raw.get("themes") or []) if str(t).strip()][:6]
    loops = ["待办：" + str(t).strip()[:40] for t in (raw.get("open_loops") or []) if str(t).strip()][:4]
    return str(raw.get("summary") or "")[:400], themes + loops, provider.name


def enqueue_draft(conversation_id: str, message_id: str | None, *, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("draft_turn", conversation_id, payload={"conversationId": conversation_id, "messageId": message_id}, priority=8)


def enqueue_first_observation(conversation_id: str, message_id: str | None, *, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("first_observation", conversation_id, payload={"conversationId": conversation_id, "messageId": message_id}, priority=4)


def enqueue_home_brief(source_hash: str, *, store: OntologyStore | None = None, scope="global") -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("home_brief", "today" if scope == "global" else "today:" + scope, payload={"sourceHash": source_hash, "scope": scope}, priority=2, input_hash=source_hash)


def _routing_pause(exc):
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    preview = detail.get("preview") or {}
    code = detail.get("code", "")
    if preview or code in {"SOURCE_UNAVAILABLE", "SOURCE_CHANGED", "SOURCE_LIMIT", "ROUTE_CHANGED", "ONLINE_SERVICE_CHANGED",
                           "CHARTER_CHANGED", "CHARTER_POLICY_CONFLICT", "CHARTER_CONTEXT_TOO_LARGE"}:
        reason = "source_unavailable" if preview.get("blocked") else "consent_required" if preview.get("missing") else code.lower() or "consent_required"
        return {"state": "paused", "reason": reason,
                "detail": "相关来源已失效或无法核验，请重新核对；不会绕过来源限制" if preview.get("blocked") else detail.get("detail", "后台任务等待核对"),
                **({"previewId": preview["revision"]} if preview.get("revision") else {})}
    return None


def run_job(job: dict, *, store: OntologyStore, conv_store: ConversationStore) -> dict:
    from fastapi import HTTPException
    from .routing import Router, GuardedProvider
    cid = (job.get("payload") or {}).get("conversationId")
    if cid:
        router = Router(store, conv_store, cid)
        def choose():
            return GuardedProvider(router, router.provider(bool(job.get("payload", {}).get("localOnly"))),
                                   job["kind"], router.history_refs(), background=True)
        try:
            from .charter_policy import scope_policy
            managed = bool(scope_policy(router.scope)["charterId"]) or router.mode["mode"] != "legacy" or any("routingSources" in (m.get("meta") or {}) for m in conv_store.list_messages(cid))
            result = _run_job(job, store=store, conv_store=conv_store, choose_provider=choose, managed=managed)
            if result.get("state") != "paused" and not router.store.paused_jobs(cid, job["kind"]):
                router.store.pending(cid, job["kind"], None)
            return result
        except HTTPException as exc:
            paused = _routing_pause(exc)
            if paused:
                return paused
            raise
    try:
        return _run_job(job, store=store, conv_store=conv_store)
    except HTTPException as exc:
        paused = _routing_pause(exc)
        if paused:
            return paused
        raise


def _run_job(job: dict, *, store: OntologyStore, conv_store: ConversationStore, choose_provider=build_provider, managed=False) -> dict:
    kind = job["kind"]
    payload = job.get("payload") or {}
    if kind == "charter_draft":
        from .charter import run_job as run_charter_job
        return run_charter_job(payload, store, conv_store)
    from . import alignment, memory
    if kind in ("alignment", "first_observation") and not memory.automatic_allowed(store, conv_store, payload.get("conversationId")):
        return {"state": "skipped", "reason": "memory_policy"}
    if kind == "alignment":
        return alignment.run_job(payload, store, conv_store)
    if not managed and payload.get("conversationId") and alignment.protected(payload["conversationId"], conv_store, store):
        return {"state": "skipped", "reason": "private_profile_requires_explicit_action"}
    from ..stores.chat_import_store import ChatImportStore
    imports = ChatImportStore(conv_store)
    if not managed and payload.get("conversationId") and imports.has_imports(payload["conversationId"]):
        return {"state": "skipped", "reason": "file_discussion_requires_explicit_action"}
    if kind == "extract_material" and job["ownerId"] in imports.protected_ids():
        return {"state": "skipped", "reason": "file_is_not_personal_assertion"}
    if kind == "extract_turn":
        conversation_id = payload.get("conversationId")
        message_id = payload.get("messageId")
        message = conv_store.get_message(message_id) if message_id else None
        if message is None:
            return {"state": "skipped", "reason": "message_missing"}
        if message.get("conversationId") != conversation_id:
            return {"state": "skipped", "reason": "message_conversation_mismatch"}
        if message.get("role") != "user" or message.get("status") != "complete":
            return {"state": "skipped", "reason": "not_completed_user_message"}
        input_origin = (message.get("meta") or {}).get("replyAssistance")
        if input_origin and input_origin.get("kind") == "control":
            return {"state": "skipped", "reason": "conversation_control"}
        if (message.get("meta") or {}).get("materialRefs"):
            return {"state": "skipped", "reason": "file_is_not_personal_assertion"}
        conversation = conv_store.get_conversation(conversation_id)
        if conversation is None:
            return {"state": "skipped", "reason": "conversation_missing"}
        # A queued task does not retain permission to create memories after the
        # user changes their preference. Check before provider selection or gate.
        if not memory.extraction_allowed(store, conv_store, conversation_id, message["content"]):
            return {"state": "skipped", "reason": "memory_policy"}
        history = conv_store.list_messages(conversation_id)
        prev_assistant = None
        for item in history:
            if item["seq"] >= message["seq"]:
                break
            if item["role"] == "assistant":
                prev_assistant = item["content"]
        provider = choose_provider()
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, timeout=30.0, background=True):
            raise ProviderError("模型通道繁忙", status_code=429, code="PROVIDER_BUSY", retryable=True)
        try:
            result = extract.run_extraction(
                provider=provider,
                store=store,
                conversation_id=conversation_id,
                message_id=message_id,
                user_text=message["content"],
                prev_assistant=prev_assistant,
                debug={"mode": conversation.get("mode")},
                input_origin=input_origin,
            )
        finally:
            provider_gate.release(channel)
        if result.get("created") or result.get("promoted"):
            enqueue_projection(store=store)
            try:
                from . import consolidate

                if consolidate.should_run(store):
                    enqueue_consolidate(store=store)
            except Exception:  # noqa: BLE001
                pass
            # Only a newly admitted memory can trigger automatic calibration.
            # Duplicate/suppressed/context-only drafts do not create extra work;
            # also honor a mode change while extraction was running.
            if memory.automatic_allowed(store, conv_store, conversation_id):
                assistants = [m for m in conv_store.list_messages(conversation_id)
                              if m["role"] == "assistant" and m["status"] == "complete" and m["seq"] > message["seq"]]
                if assistants:
                    enqueue_alignment(conversation_id, assistants[0]["id"], message["content"], store=store)
        user_turns = conv_store.count_messages(conversation_id, role="user")
        if user_turns and user_turns % _SUMMARY_EVERY_TURNS == 0:
            enqueue_summary(conversation_id, store=store)
        return result
    if kind == "summarize_conversation":
        conversation_id = payload.get("conversationId")
        messages = conv_store.list_messages(conversation_id)
        if not messages:
            return {"state": "skipped", "reason": "empty"}
        from .charter_policy import scope_policy, assert_current, check_action, basis
        from .alignment import scope_for
        policy = scope_policy(scope_for(conversation_id, conv_store))
        if not check_action(policy, "memory_auto")["allowed"]:
            return {"state": "skipped", "reason": "charter_memory_manual"}
        provider = choose_provider()
        summary, points, generated_by = _model_summary(messages, provider)
        from .routing import GuardedProvider
        refs = [{"kind": "message", "id": m["id"]} for m in messages]
        if isinstance(provider, GuardedProvider) and provider.last_preview:
            provider.assert_current()
            refs = [s["ref"] for s in provider.last_preview["sources"]]
        assert_current(policy)
        saved = conv_store.save_summary(
            conversation_id,
            up_to_seq=messages[-1]["seq"],
            summary=summary,
            key_points=points,
            generated_by=generated_by,
            meta={"routingSources": refs, "charterBasis": basis(policy)},
        )
        return {"state": "done", "revision": saved["revision"]}
    if kind == "draft_turn":
        from . import deliberate

        provider = choose_provider()
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, timeout=60.0, background=True):
            raise ProviderError("模型通道繁忙", status_code=429, code="PROVIDER_BUSY", retryable=True)
        try:
            draft, changed = deliberate.run_draft(provider=provider, conv_store=conv_store, conversation_id=payload.get("conversationId"), message_id=payload.get("messageId"))
        finally:
            provider_gate.release(channel)
        return {"state": "done", "draftId": draft["id"], "revision": draft["revision"], "changed": changed}
    if kind == "first_observation":
        provider = choose_provider()
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, timeout=60.0, background=True):
            raise ProviderError("模型通道繁忙", status_code=429, code="PROVIDER_BUSY", retryable=True)
        try:
            result = extract.first_observation(provider=provider, store=store, conversation_id=payload.get("conversationId"), message_id=payload.get("messageId"))
        finally:
            provider_gate.release(channel)
        return result
    if kind == "home_brief":
        from .. import zhijun_home

        return zhijun_home.generate_home_brief(str(payload.get("sourceHash") or job.get("inputHash") or ""), store=store, conv_store=conv_store, local_only=bool(payload.get("localOnly")), scope=payload.get("scope", "global"))
    if kind == "project":
        return projection.write_projection(store)
    if kind == "nudge_scan":
        from . import nudges

        return nudges.scan(conv_store=conv_store)
    if kind == "consolidate":
        from . import consolidate
        from .routing import Router
        routing = Router(store, conv_store, "scope:global")

        try:
            provider = routing.provider(bool(payload.get("localOnly")))
        except ProviderError:
            provider = None
        return consolidate.run(store=store, conv_store=conv_store, provider=provider, router=routing)
    if kind == "extract_material":
        from . import materials

        result = materials.run(job["ownerId"], store=store)
        if result.get("created"):
            enqueue_projection(store=store)
        return result
    return {"state": "skipped", "reason": f"unknown_kind:{kind}"}


def enqueue_consolidate(*, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("consolidate", "nightly", payload={}, priority=-1)


def enqueue_material_extraction(material_id: str, *, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("extract_material", material_id, payload={"materialId": material_id}, priority=2)


def enqueue_nudge_scan(*, store: OntologyStore | None = None) -> str | None:
    store = store or OntologyStore.instance()
    return store.enqueue_job("nudge_scan", "hourly", payload={}, priority=0)


_NUDGE_SCAN_INTERVAL = 3600.0


class OntologyWorker:
    _instance: "OntologyWorker | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._processed = 0

    @classmethod
    def instance(cls) -> "OntologyWorker":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    @property
    def processed(self) -> int:
        return self._processed

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="zhijun-ontology-worker", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        try:
            thread.join(timeout=wait)
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        store = OntologyStore.instance()
        conv_store = ConversationStore.instance()
        try:
            reclaimed = store.recover_expired_jobs()
            if reclaimed:
                logger.info("本体 worker 回收 %d 个租约过期任务", reclaimed)
        except Exception:  # noqa: BLE001
            pass
        last_scan = 0.0
        while not self._stop_event.is_set():
            if time.time() - last_scan >= _NUDGE_SCAN_INTERVAL:
                last_scan = time.time()
                try:
                    enqueue_nudge_scan(store=store)
                    from . import consolidate

                    if consolidate.should_run(store):
                        enqueue_consolidate(store=store)
                except Exception:  # noqa: BLE001
                    pass
            try:
                job = store.claim_next_job(owner, _LEASE_SECONDS)
            except Exception:  # noqa: BLE001
                job = None
            if job is None:
                self._stop_event.wait(_IDLE_POLL_SECONDS)
                continue
            self.process(job, owner, store=store, conv_store=conv_store)

    def process(self, job: dict, owner: str, *, store: OntologyStore, conv_store: ConversationStore) -> None:
        job_id = job["jobId"]
        try:
            result = run_job(job, store=store, conv_store=conv_store)
            store.finish_job(job_id, owner, result=result)
        except ProviderError as exc:
            store.fail_job(
                job_id,
                owner,
                failure_class="transient" if exc.retryable else "business",
                error_code=exc.code,
                error_detail=str(exc),
                retry=exc.retryable,
            )
            if exc.retryable:
                time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("本体任务失败 %s: %s", job.get("kind"), type(exc).__name__)
            store.fail_job(job_id, owner, failure_class="infrastructure", error_code=type(exc).__name__, error_detail=str(exc)[:300], retry=False)
        finally:
            self._processed += 1


def start_worker() -> None:
    OntologyWorker.instance().start()


def stop_worker() -> None:
    OntologyWorker.instance().stop()


def worker_running() -> bool:
    return OntologyWorker.instance().running


def drain(*, store: OntologyStore | None = None, conv_store: ConversationStore | None = None, max_jobs: int = 50) -> int:
    """测试 / 脚本用：同步处理完队列里的任务，返回处理条数。"""
    store = store or OntologyStore.instance()
    conv_store = conv_store or ConversationStore.instance()
    worker = OntologyWorker.instance()
    owner = f"drain-{uuid.uuid4().hex[:8]}"
    count = 0
    while count < max_jobs:
        job = store.claim_next_job(owner, _LEASE_SECONDS)
        if job is None:
            break
        worker.process(job, owner, store=store, conv_store=conv_store)
        count += 1
    return count
