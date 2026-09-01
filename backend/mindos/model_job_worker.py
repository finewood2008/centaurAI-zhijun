"""模型任务单 worker（P2 §7.0.1）。

职责：
- `model_jobs` 唯一领取者：HTTP 路由只创建/查询/取消，执行完全收敛到这里；
- 首期并发度固定为 1（避免 pull/预热/卸载与材料推理争抢同一 Ollama），由单例守护线程保证；
- 条件领取（queued->running 原子更新）+ 每 10s 续租 + 同类型按目标模型去重；
- 任务在领取时获取运行时本地 Ollama 快照并持有到结束（§5.1.1 / §7.0.1 第 3 条），
  保存新地址/模型不迁移已运行任务；
- 启动时先回收过期租约；错误按类型有限重试（pull=3、load/unload=2）；
- 受控关闭：停止领取并在安全点退出；进行中的阻塞请求不强杀，交由下次启动以租约恢复。

本模块不持有 FastAPI Request；`server` 生命周期负责 start/stop。
"""
from __future__ import annotations

import os
import threading
import time
import uuid

from . import llm_transport, ollama_client
from .runtime_config_provider import LocalOllamaSnapshot, get_provider
from .stores.model_job_store import (
    MAX_ATTEMPTS,
    ModelJobStore,
    STATE_CANCEL_REQUESTED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
)

# 租约/心跳周期（与 store `_LOCK_CLEAR_SECONDS=60` 对齐）。
_LEASE_SECONDS = 60
_HEARTBEAT_SECONDS = 10.0
# 无任务时 worker 轮询间隔。
_IDLE_POLL_SECONDS = 1.0
# load/unload 单次调用的总超时使用材料处理快照超时；pull 不做总超时（下载可能很长）。
_PULL_TIMEOUT = None
_PROGRESS_UNSET = object()


def _classify_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return f"http_{code}"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "connection"
    return "unknown"


class _Heartbeat:
    """每 10s 续租一次的任务心跳；run() 返回后停止。"""

    def __init__(self, store: ModelJobStore, job_id: str, owner: str) -> None:
        self._store = store
        self._job_id = job_id
        self._owner = owner
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(_HEARTBEAT_SECONDS):
            try:
                self._store.renew(self._job_id, self._owner, _LEASE_SECONDS)
            except Exception:
                # 续租失败不中断任务；过期自然由下次启动 `recover_expired` 回收。
                pass


class ModelJobWorker:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def instance(cls) -> "ModelJobWorker":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ModelJobWorker()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """测试隔离：停止并清空单例。"""
        with cls._instance_lock:
            inst = cls._instance
            cls._instance = None
        if inst is not None:
            inst.stop()

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="model-job-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # 不强杀进行中的 Ollama 请求（§7.0.1 第 5 条）。长 pull 交由下次启动以租约
        # 恢复：仅做极短 join 让空闲/安全点的 worker 干净退出，避免拖长服务 shutdown。
        t = self._thread
        if t is None or t is threading.current_thread():
            return
        try:
            t.join(timeout=2.0)
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ---- 主循环 ----

    def _run(self) -> None:
        store = ModelJobStore.instance()
        owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        # 启动恢复：先回收过期租约（§7 重启恢复规则 1），再接收新任务。
        try:
            reclaimed = store.recover_expired()
            # §7 规则 2：recover_expired 将过期 running 任务重投为 queued 后，pull 任务
            # 需核验模型是否已完整存在——若已存在则直接成功，避免进程在下载完成后崩溃、
            # 重启后再次 pull 并消耗一次 attempts。
            self._resolve_recovered_pulls(store)
            if reclaimed:
                print(f"[model-job-worker] 回收 {reclaimed} 个租约过期任务", flush=True)
        except Exception:
            pass
        while not self._stop_event.is_set():
            try:
                job = store.claim_next(owner, _LEASE_SECONDS)
            except Exception:
                job = None
            if job is None:
                self._stop_event.wait(_IDLE_POLL_SECONDS)
                continue
            self._execute(job, store, owner)

    def _execute(self, job: dict, store: ModelJobStore, owner: str) -> None:
        job_id = job["id"]
        type_ = job["type"]
        # 领取时经本地快照字段还原创建时 config_revision 对应的不可变快照并持有到结束
        # （§5.1.1 / §7.0.1 第 3 条）：保存新地址/模型不迁移已运行任务，也不让重启恢复
        # 的任务改用新配置。
        provider = get_provider()
        snap = self._snapshot_from_job(job)
        hb = _Heartbeat(store, job_id, owner)
        hb_thread = threading.Thread(target=hb.run, name=f"model-job-hb-{type_}", daemon=True)
        hb_thread.start()
        try:
            if self._is_cancel_requested(store, job_id):
                store.mark_cancelled(job_id, owner)
                return
            if type_ == "pull":
                result, _ = self._do_pull(snap, job, store, owner, provider)
                if result.get("status") == "success":
                    # 拉取结束：可能中途收到取消请求（should_abort 拦截后返回 cancelled，
                    # 若竞态内请求已完整则 here）；统一走取消终态判定（§7 取消规则 4）。
                    self._terminal(job, store, owner)
                elif result.get("status") == "cancelled":
                    store.mark_cancelled(job_id, owner)
                else:
                    err_msg = result.get("error") or result.get("status") or "pull_error"
                    if self._is_cancel_requested(store, job_id):
                        store.mark_cancelled(job_id, owner)
                    else:
                        self._retry_or_fail(job, store, owner, "pull_failed", str(err_msg)[:500])
            elif type_ == "load":
                ollama_client.load(snap, job["target_model"], store=provider.store, timeout=snap.timeout_seconds)
                self._terminal(job, store, owner)
            elif type_ == "unload":
                ollama_client.unload(snap, job["target_model"], store=provider.store, timeout=snap.timeout_seconds)
                self._terminal(job, store, owner)
            else:
                self._retry_or_fail(job, store, owner, "invalid_type", f"未知任务类型 {type_}")
        except Exception as exc:  # noqa: BLE001
            self._retry_or_fail(job, store, owner, _classify_error(exc), str(exc)[:500])
        finally:
            hb.stop()

    def _is_cancel_requested(self, store: ModelJobStore, job_id: str) -> bool:
        try:
            row = store.get(job_id)
        except Exception:
            return False
        return bool(row and row.get("state") == STATE_CANCEL_REQUESTED)

    def _terminal(self, job: dict, store: ModelJobStore, owner: str) -> None:
        """终态归一：操作完成后若已被协作式取消请求，则写 cancelled，否则 succeed。"""
        if self._is_cancel_requested(store, job["id"]):
            store.mark_cancelled(job["id"], owner)
        else:
            store.succeed(job["id"], owner)

    def _do_pull(self, snap, job: dict, store: ModelJobStore, owner: str, provider) -> tuple[dict, list[dict]]:
        """执行流式拉取；on_progress 同时回写持久化进度与续租（双保险：心跳独立，这里再喂一次）。"""
        last_progress = _PROGRESS_UNSET

        def on_progress(current: int, total: int | None) -> None:
            nonlocal last_progress
            if current != last_progress:
                last_progress = current
                try:
                    store.update_progress(job["id"], owner, current, total)
                except Exception:
                    pass
            try:
                store.renew(job["id"], owner, _LEASE_SECONDS)
            except Exception:
                pass

        def should_abort() -> bool:
            # 协作式取消（§7 规则 4）：在流读取边界检查是否收到取消请求，truthy 时
            # pull 停止并返回 status="cancelled"，避免取消后任务永久滞留 cancel_requested。
            return self._is_cancel_requested(store, job["id"])

        return ollama_client.pull(
            snap,
            job["target_model"],
            store=provider.store,
            timeout=_PULL_TIMEOUT,
            on_progress=on_progress,
            should_abort=should_abort,
        )

    def _snapshot_from_job(self, job: dict) -> LocalOllamaSnapshot:
        """按任务持久化的快照字段还原创建时配置，缺失字段（旧记录）回退当前快照。"""
        current = get_provider().get_local_snapshot()
        return LocalOllamaSnapshot(
            base_url=(job.get("local_base_url") or "").strip() or current.base_url,
            model=job.get("target_model") or current.model,
            timeout_seconds=job.get("local_timeout_seconds") or current.timeout_seconds,
            keep_alive=(
                job.get("local_keep_alive")
                if job.get("local_keep_alive") is not None
                else current.keep_alive
            ),
            context_window=(
                job.get("local_context_window")
                if job.get("local_context_window") is not None
                else current.context_window
            ),
        )

    def _resolve_recovered_pulls(self, store: ModelJobStore) -> None:
        """§7 规则 2：被回收重投（attempts>=1）的 queued pull 若模型已完整存在，标记成功。"""
        provider = get_provider()
        try:
            jobs = store.list_jobs(state=STATE_QUEUED, type_="pull", limit=100)
        except Exception:
            return
        for job in jobs:
            if (job.get("attempts") or 0) < 1:
                continue
            try:
                snap = self._snapshot_from_job(job)
                if ollama_client.model_installed(snap, job["target_model"], store=provider.store):
                    store.mark_pull_installed(job["id"])
            except Exception:
                continue

    def _retry_or_fail(self, job: dict, store: ModelJobStore, owner: str, code: str, msg: str) -> None:
        """失败后按类型重试上限（§7 规则 4）：低于上限回 queued 减配重投，否则终态 failed。"""
        store.fail(job["id"], owner, code, msg)
        if store.requeue_for_retry(job["id"], code, msg):
            return
        # 达到最大尝试次数：保持 failed。


# ---- 进程级单例入口（server 生命周期调用） ----


def start_worker() -> None:
    ModelJobWorker.instance().start()


def stop_worker() -> None:
    ModelJobWorker.reset()
