"""并发门：同一会话同时只允许一轮生成；模型通道按本地/外部分别限流。

替代 qa.py 的全局 ``BoundedSemaphore(1)``（那把锁只保护 ``/api/mindos/qa``，保持不动）。
后台抽取与交互轮次共用通道门，交互侧等待上限很短（默认 2 秒）以便快速返回 429。
"""
from __future__ import annotations

import threading
import time
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
        self._limits = {"local": max(1, int(local_limit)), "external": max(1, int(external_limit))}
        self._active = {"local": 0, "external": 0}
        self._interactive = {"local": 0, "external": 0}
        self._condition = threading.Condition()

    def acquire(self, channel: str, timeout: float, *, background: bool = False) -> bool:
        channel = channel if channel in self._limits else "local"
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            if not background:
                self._interactive[channel] += 1
            try:
                while self._active[channel] >= self._limits[channel] or (background and self._interactive[channel]):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self._active[channel] += 1
                return True
            finally:
                if not background:
                    self._interactive[channel] -= 1
                self._condition.notify_all()

    def release(self, channel: str) -> None:
        channel = channel if channel in self._limits else "local"
        with self._condition:
            self._active[channel] = max(0, self._active[channel] - 1)
            self._condition.notify_all()

    @contextmanager
    def slot(self, channel: str, timeout: float, *, background: bool = False):
        if not self.acquire(channel, timeout, background=background):
            raise ProviderBusyError(channel)
        try:
            yield
        finally:
            self.release(channel)


conversation_locks = ConversationLocks()
provider_gate = ProviderGate()
