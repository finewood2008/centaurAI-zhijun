"""MindOS 统一 Ollama 材料派生调度器（阶段 B §6.2）。

摘要、标签、实体、关系、图片 VLM 描述必须经过本调度器：
- 默认并发=1（避免多线程同时占用 qwen3-vl:2b 导致 OOM）；
- 优先级：用户手动重生成 > 当前材料摘要/实体 > 标签 > 关系 > 批量后台 > 图片 VLM；
- 任务开始时读取运行时配置快照（URL/model/timeout）；已经发出的请求不受后续设置变更影响；
- 超时/连接失败/空响应仅写入派生记录 failed/unavailable，绝不影响正文快照和材料主任务；
- 本调度器取代 derived.py 原有的 _SUMMARY_POOL/_ANALYSIS_POOL 双线程池以及
  _DERIVED_IN_FLIGHT/_RELATION_IN_FLIGHT 去重标记，不留双并发路径。

任务边界：每个任务就是一次「模型调用 + 结果落库」。输入 hash 仍由派生模块
判定；本层对同一材料/产物合并待执行请求，确保最新请求覆盖过时请求。
"""
from __future__ import annotations

import queue
import itertools
import logging
import threading
from typing import Callable, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# 优先级数值越小，优先级越高
PRIORITY_MANUAL_REGENERATE = 0    # 用户手动触发的「重新生成」
PRIORITY_SUMMARY_ENTITIES = 10    # 材料刚完成解析，第一次生成摘要与实体
PRIORITY_TAGS = 20                # 标签生成
PRIORITY_RELATIONS = 30           # 关系生成
PRIORITY_BATCH_BACKGROUND = 40   # 批量刷新/后台补算
PRIORITY_VLM_IMAGE = 50           # 纯视觉图片 VLM 描述


@dataclass(order=True)
class _PrioritizedTask:
    priority: int
    sequence: int
    token: int = field(compare=False)
    task_fn: Callable[[], None] = field(compare=False)
    material_id: str | None = field(compare=False, default=None)
    kind: str | None = field(compare=False, default=None)
    key: tuple[str, str] | None = field(compare=False, default=None)


class OllamaMaterialScheduler:
    """单例优先调度器：所有 Ollama 派生任务走这一个工作线程，默认并发=1。"""

    _instance: Optional[OllamaMaterialScheduler] = None
    _instance_lock = threading.Lock()

    def __init__(self, max_workers: int = 1):
        self._max_workers = max_workers
        self._queue: queue.PriorityQueue[_PrioritizedTask] = queue.PriorityQueue()
        self._workers: list[threading.Thread] = []
        self._running: bool = False
        self._lock = threading.Lock()
        self._shutdown_flag = threading.Event()
        self._sequence = itertools.count()
        self._pending_tokens: dict[tuple[str, str], int] = {}

    @classmethod
    def instance(cls) -> OllamaMaterialScheduler:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _discard_finished_workers_locked(self) -> bool:
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        return bool(self._workers)

    def start(self) -> bool:
        """启动 worker；旧 worker 未退出时不允许重启，避免突破并发上限。"""
        with self._lock:
            if self._running:
                return True
            # stop 不会强制中断已经发出的 HTTP 调用。必须保留旧线程及其 shutdown
            # 标记，直到它自然退出，不能先 clear event 再启动另一批 worker。
            if self._discard_finished_workers_locked():
                logger.warning("OllamaMaterialScheduler is still stopping; start deferred")
                return False
            self._running = True
            self._shutdown_flag.clear()
            for i in range(self._max_workers):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"ollama-material-worker-{i}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)
            logger.info("OllamaMaterialScheduler started with %d worker(s)", self._max_workers)
            return True

    def stop(self) -> None:
        """停止调度器；取消未开始的任务，不阻塞已运行的调用。"""
        with self._lock:
            if not self._running and not self._workers:
                return
            self._running = False
            self._shutdown_flag.set()
            self._pending_tokens.clear()
            # 清空队列，丢弃未开始的任务（进程即将退出，无需等待）
            cleared = 0
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    cleared += 1
                except queue.Empty:
                    break
            if cleared:
                logger.info("Cleared %d pending tasks during shutdown", cleared)
            # 保留仍活跃的 worker 引用；start 必须等它们退出才能创建新 worker。
            self._discard_finished_workers_locked()
            logger.info("OllamaMaterialScheduler stopped")

    def status(self) -> dict:
        """脱敏运行快照，供阶段 D 监控读取。"""
        with self._lock:
            self._discard_finished_workers_locked()
            return {
                "running": self._running and not self._shutdown_flag.is_set(),
                "workers": len(self._workers),
                "maxWorkers": self._max_workers,
                "queued": self._queue.qsize(),
                "deduplicatedPending": len(self._pending_tokens),
            }

    def submit(
        self,
        priority: int,
        task_fn: Callable[[], None],
        *,
        material_id: str | None = None,
        kind: str | None = None,
    ) -> bool:
        """提交一个 Ollama 派生任务到优先级队列。

        Args:
            priority: 优先级常量 PRIORITY_*；数值越小优先级越高。
            task_fn: 可调用对象（无参数），执行模型调用与派生落库。
            material_id: 材料 ID（用于日志）。
            kind: 派生种类（summary/tags/entities/relations/vlm，用于日志）。
        Returns:
            True=已接受；False=调度器已停止，任务被拒绝。
        """
        if not self.start():
            logger.warning("OllamaMaterialScheduler unavailable, task rejected: %s/%s", material_id, kind)
            return False
        key = (material_id, kind) if material_id and kind else None
        token = next(self._sequence)
        task = _PrioritizedTask(
            priority=priority,
            sequence=token,
            token=token,
            task_fn=task_fn,
            material_id=material_id,
            kind=kind,
            key=key,
        )
        with self._lock:
            if not self._running or self._shutdown_flag.is_set():
                logger.warning("OllamaMaterialScheduler stopped, task rejected: %s/%s", material_id, kind)
                return False
            if key is not None:
                # 旧项保留在 PriorityQueue，消费时按 token 跳过；避免重建队列的竞争。
                self._pending_tokens[key] = token
            self._queue.put(task)
        return True

    def _worker_loop(self) -> None:
        while not self._shutdown_flag.is_set():
            try:
                task = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._shutdown_flag.is_set():
                    continue
                if task.key is not None:
                    with self._lock:
                        if self._pending_tokens.get(task.key) != task.token:
                            continue
                        # 已领取任务不再是 pending；运行期间的新请求只保留一个后继任务。
                        del self._pending_tokens[task.key]
                if task.material_id and task.kind:
                    logger.debug("Running scheduled Ollama task: material=%s kind=%s", task.material_id, task.kind)
                task.task_fn()
            except Exception:
                # 任务本身不应该抛出未捕获异常；但顶层兜底打日志不崩线程。
                if task.material_id:
                    logger.exception(
                        "Uncaught exception in scheduled Ollama task: material=%s kind=%s",
                        task.material_id, task.kind,
                    )
                else:
                    logger.exception("Uncaught exception in scheduled Ollama task")
            finally:
                self._queue.task_done()


# 便捷模块级入口，避免到处 import .instance()
_scheduler = OllamaMaterialScheduler.instance()


def start_scheduler() -> bool:
    return _scheduler.start()


def stop_scheduler() -> None:
    _scheduler.stop()


def scheduler_status() -> dict:
    return _scheduler.status()


def submit_manual_regenerate(fn: Callable[[], None], *, material_id: str, kind: str) -> bool:
    return _scheduler.submit(PRIORITY_MANUAL_REGENERATE, fn, material_id=material_id, kind=kind)


def submit_summary_entities(fn: Callable[[], None], *, material_id: str) -> bool:
    return _scheduler.submit(PRIORITY_SUMMARY_ENTITIES, fn, material_id=material_id, kind="summary+entities")


def submit_tags(fn: Callable[[], None], *, material_id: str) -> bool:
    return _scheduler.submit(PRIORITY_TAGS, fn, material_id=material_id, kind="tags")


def submit_relations(fn: Callable[[], None], *, material_id: str) -> bool:
    return _scheduler.submit(PRIORITY_RELATIONS, fn, material_id=material_id, kind="relations")


def submit_background(fn: Callable[[], None], *, material_id: str, kind: str) -> bool:
    return _scheduler.submit(PRIORITY_BATCH_BACKGROUND, fn, material_id=material_id, kind=kind)


def submit_vlm_image(fn: Callable[[], None], *, material_id: str) -> bool:
    return _scheduler.submit(PRIORITY_VLM_IMAGE, fn, material_id=material_id, kind="vlm-description")
