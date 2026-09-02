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


def _extractive_summary(messages: list[dict], max_chars: int = 400) -> tuple[str, list[str]]:
    """无模型的抽取式摘要：取用户消息要点，供本地简版上下文使用。"""
    points: list[str] = []
    for message in messages:
        if message.get("role") != "user":
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


def _model_summary(messages: list[dict]) -> tuple[str, list[str], str]:
    """有真实模型时用模型出主题与待办；否则抽取式。返回 (summary, key_points, generated_by)。"""
    user_texts = [m["content"] for m in messages if m.get("role") == "user" and (m.get("content") or "").strip()]
    try:
        provider = build_provider()
    except ProviderError:
        provider = None
    if provider is None or provider.name == "fake":
        summary, points = _extractive_summary(messages)
        return summary, points, "extractive"
    from .provider import ChatRequest

    transcript = "\n".join(f"{'用户' if m['role'] == 'user' else '知君'}：{(m.get('content') or '').strip()[:300]}" for m in messages[-24:] if m.get("role") in ("user", "assistant"))
    request = ChatRequest(system=_SUMMARY_SYSTEM, messages=[{"role": "user", "content": transcript}], max_tokens=800, temperature=0.0, json_schema=_SUMMARY_SCHEMA, effort="low", debug={"task": "summary", "userTexts": user_texts})
    channel = "external" if provider.external else "local"
    if not provider_gate.acquire(channel, timeout=60.0):
        summary, points = _extractive_summary(messages)
        return summary, points, "extractive"
    try:
        raw = provider.complete_json(request)
    except (ProviderError, ValueError):
        summary, points = _extractive_summary(messages)
        return summary, points, "extractive"
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


def run_job(job: dict, *, store: OntologyStore, conv_store: ConversationStore) -> dict:
    kind = job["kind"]
    payload = job.get("payload") or {}
    if kind == "extract_turn":
        conversation_id = payload.get("conversationId")
        message_id = payload.get("messageId")
        message = conv_store.get_message(message_id) if message_id else None
        if message is None:
            return {"state": "skipped", "reason": "message_missing"}
        conversation = conv_store.get_conversation(conversation_id) or {}
        history = conv_store.list_messages(conversation_id)
        prev_assistant = None
        for item in history:
            if item["seq"] >= message["seq"]:
                break
            if item["role"] == "assistant":
                prev_assistant = item["content"]
        provider = build_provider()
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, timeout=30.0):
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
        user_turns = conv_store.count_messages(conversation_id, role="user")
        if user_turns and user_turns % _SUMMARY_EVERY_TURNS == 0:
            enqueue_summary(conversation_id, store=store)
        return result
    if kind == "summarize_conversation":
        conversation_id = payload.get("conversationId")
        messages = conv_store.list_messages(conversation_id)
        if not messages:
            return {"state": "skipped", "reason": "empty"}
        summary, points, generated_by = _model_summary(messages)
        saved = conv_store.save_summary(
            conversation_id,
            up_to_seq=messages[-1]["seq"],
            summary=summary,
            key_points=points,
            generated_by=generated_by,
        )
        return {"state": "done", "revision": saved["revision"]}
    if kind == "draft_turn":
        from . import deliberate

        provider = build_provider()
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, timeout=60.0):
            raise ProviderError("模型通道繁忙", status_code=429, code="PROVIDER_BUSY", retryable=True)
        try:
            draft, changed = deliberate.run_draft(provider=provider, conv_store=conv_store, conversation_id=payload.get("conversationId"), message_id=payload.get("messageId"))
        finally:
            provider_gate.release(channel)
        return {"state": "done", "draftId": draft["id"], "revision": draft["revision"], "changed": changed}
    if kind == "first_observation":
        provider = build_provider()
        channel = "external" if provider.external else "local"
        if not provider_gate.acquire(channel, timeout=60.0):
            raise ProviderError("模型通道繁忙", status_code=429, code="PROVIDER_BUSY", retryable=True)
        try:
            result = extract.first_observation(provider=provider, store=store, conversation_id=payload.get("conversationId"), message_id=payload.get("messageId"))
        finally:
            provider_gate.release(channel)
        return result
    if kind == "project":
        return projection.write_projection(store)
    if kind == "nudge_scan":
        from . import nudges

        return nudges.scan(conv_store=conv_store)
    if kind == "consolidate":
        from . import consolidate

        try:
            provider = build_provider()
        except ProviderError:
            provider = None
        return consolidate.run(store=store, conv_store=conv_store, provider=provider)
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
