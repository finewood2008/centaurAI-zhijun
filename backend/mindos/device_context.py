"""MindOS 按真实 device_id 隔离的运行上下文。

阶段 2：连接票据验签后，所有盒内业务请求都归属于票据绑定的 device_id。
本模块登记每个设备的运行时上下文：缓存命名空间、任务命名空间与活动会话，
保证不同设备之间不共享缓存/会话；设备被撤销或切换后旧上下文立即失效。

约束：
- 本机调试模式不创建真实设备上下文（不得声称或写入 Consumer device_id）。
- 上下文只记录非秘密元数据，不保存票据、密钥或用户内容。
- 后续迭代中，盒内缓存/任务/会话层应通过 request.state.mindos_device_context
  取得 cache_namespace / task_namespace 后按设备键控，不能在业务层自行拼接。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .connectivity_ticket import ConnectivityPrincipal


@dataclass
class DeviceContext:
    """单个真实设备的盒内运行时状态（无秘密）。"""

    device_id: str
    account_id: str
    client_id: str
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    cache_generation: int = 0
    session_ids: set[str] = field(default_factory=set)

    def cache_namespace(self, kind: str) -> str:
        """缓存命名空间：设备 + 代际，代际递增使旧缓存整体失效。"""
        return f"device:{self.device_id}:{kind}:{self.cache_generation}"

    def task_namespace(self) -> str:
        return f"device:{self.device_id}:tasks"

    def touch(self) -> None:
        self.last_active_at = time.time()

    def invalidate(self) -> None:
        """使该设备旧缓存与会话整体失效（撤销/切换设备时调用）。"""
        self.cache_generation += 1
        self.session_ids.clear()


class DeviceContextRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, DeviceContext] = {}

    def get_or_create(self, principal: ConnectivityPrincipal) -> DeviceContext:
        with self._lock:
            context = self._contexts.get(principal.device_id)
            if context is None:
                context = DeviceContext(
                    device_id=principal.device_id,
                    account_id=principal.account_id,
                    client_id=principal.client_id,
                )
                self._contexts[principal.device_id] = context
            elif context.account_id != principal.account_id:
                # 同一 device_id 换账号：旧上下文立即失效，防止跨账号复用缓存/会话。
                context.invalidate()
                context.account_id = principal.account_id
                context.client_id = principal.client_id
            else:
                context.touch()
            return context

    def get(self, device_id: str) -> DeviceContext | None:
        with self._lock:
            return self._contexts.get(device_id)

    def invalidate(self, device_id: str) -> DeviceContext | None:
        with self._lock:
            context = self._contexts.get(device_id)
            if context is not None:
                context.invalidate()
            return context

    def release(self, device_id: str) -> bool:
        with self._lock:
            return self._contexts.pop(device_id, None) is not None

    def list(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "deviceId": c.device_id,
                    "accountId": c.account_id,
                    "clientId": c.client_id,
                    "createdAt": c.created_at,
                    "lastActiveAt": c.last_active_at,
                    "cacheGeneration": c.cache_generation,
                    "activeSessions": sorted(c.session_ids),
                }
                for c in self._contexts.values()
            ]

    def reset(self) -> None:
        """测试钩子：清空全部运行时上下文（生产不调用）。"""
        with self._lock:
            self._contexts.clear()


_registry = DeviceContextRegistry()


def get_device_registry() -> DeviceContextRegistry:
    return _registry


def namespace_for(device_id: str, kind: str) -> str:
    """非代际的静态命名空间辅助（供任务队列等按键控）。"""
    return f"device:{device_id}:{kind}"


SCOPE_GLOBAL = "global"


def scope_for_device(device_id: str | None) -> str:
    """业务数据行的 device_scope 取值：真实设备返回 device:<id>，否则 global。

    阶段 2：票据模式下写入业务数据时必须带真实 device_scope，使不同设备/账号
    之间的材料、卡片、任务互不可见；本机调试模式不得写入设备作用域。
    """
    if not device_id:
        return SCOPE_GLOBAL
    return f"device:{device_id}"
