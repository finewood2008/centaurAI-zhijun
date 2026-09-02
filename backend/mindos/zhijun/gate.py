"""并发门：同一会话同时只允许一轮生成；模型通道按本地/外部分别限流。

替代 qa.py 的全局 ``BoundedSemaphore(1)``（那把锁只保护 ``/api/mindos/qa``，保持不动）。
后台抽取与交互轮次共用通道门，交互侧等待上限很短（默认 2 秒）以便快速返回 429。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager


class TurnInFlightError(Exception):
    """同一会话已有生成中的轮次。"""


class ProviderBusyError(Exception):
    """通道并发已满。"""


class ConversationLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def acquire(self, conversation_id: str) -> bool:
        with self._guard:
            lock = self._locks.setdefault(conversation_id, threading.Lock())
        return lock.acquire(blocking=False)

    def release(self, conversation_id: str) -> None:
        with self._guard:
            lock = self._locks.get(conversation_id)
        if lock is not None and lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass

    def in_flight(self, conversation_id: str) -> bool:
        with self._guard:
            lock = self._locks.get(conversation_id)
        return bool(lock is not None and lock.locked())


class ProviderGate:
    def __init__(self, local_limit: int = 1, external_limit: int = 3) -> None:
        self._sems = {
            "local": threading.BoundedSemaphore(max(1, int(local_limit))),
            "external": threading.BoundedSemaphore(max(1, int(external_limit))),
        }

    def acquire(self, channel: str, timeout: float) -> bool:
        sem = self._sems.get(channel) or self._sems["local"]
        return sem.acquire(timeout=max(0.0, float(timeout)))

    def release(self, channel: str) -> None:
        sem = self._sems.get(channel) or self._sems["local"]
        try:
            sem.release()
        except ValueError:
            pass

    @contextmanager
    def slot(self, channel: str, timeout: float):
        if not self.acquire(channel, timeout):
            raise ProviderBusyError(channel)
        try:
            yield
        finally:
            self.release(channel)


conversation_locks = ConversationLocks()
provider_gate = ProviderGate()
