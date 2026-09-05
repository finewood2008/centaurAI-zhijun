"""按 clientId 的轻量内存限流（AG-01 基础实现；AG-07 完善并发与时长维度）。

限制项配置化（config.RATE_LIMITS_PER_MINUTE），未配置的 action 不限制。
达到限制返回 429 / RATE_LIMITED / Retry-After。
"""
from __future__ import annotations

import threading
import time

from . import config as agent_config
from .errors import AgentError

WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(self, limits: dict | None = None, window_seconds: float = WINDOW_SECONDS):
        self._limits = dict(limits if limits is not None else agent_config.rate_limits())
        self._window = float(window_seconds)
        self._buckets: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def check(self, client_id: str, action: str, count: int = 1) -> tuple[bool, int]:
        """返回 (是否放行, Retry-After 秒数)。放行时记录本次调用。"""
        limit = self._limits.get(action)
        if limit is None:
            return True, 0
        now = time.monotonic()
        key = (client_id, action)
        with self._lock:
            bucket = [t for t in self._buckets.get(key, []) if now - t < self._window]
            if len(bucket) + count > limit:
                retry_after = max(1, int(self._window - (now - min(bucket))) if bucket else int(self._window))
                self._buckets[key] = bucket
                return False, retry_after
            bucket.extend([now] * count)
            self._buckets[key] = bucket
            return True, 0

    def reset(self, limits: dict | None = None) -> None:
        with self._lock:
            if limits is not None:
                self._limits = dict(limits)
            self._buckets.clear()


# 全局单例；测试可通过 reset() 覆盖限制。
_limiter = RateLimiter()


def get_limiter() -> RateLimiter:
    return _limiter


def check_or_deny(client_id: str, action: str, count: int = 1) -> None:
    """限流检查，超限直接抛 429。"""
    allowed, retry_after = _limiter.check(client_id, action, count=count)
    if not allowed:
        raise AgentError(
            429,
            "RATE_LIMITED",
            "请求频率超过限制，请稍后重试",
            retryable=True,
            headers={"Retry-After": str(retry_after)},
        )
