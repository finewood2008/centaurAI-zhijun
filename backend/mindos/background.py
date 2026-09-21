"""后台任务的登记接口。独立版里这些都是空操作。

这套接口原本在 `zhijun_worker.background`：盒子要求每个后台任务先向 Data Engine
登记执行来源，拿不到授权就不许排队。独立发行版没有那一层——任务就在本机的
SQLite 队列里，来源就是本机，没有第二方需要说服。

所以这里不是「桩」，而是独立版**本来的**语义：盒端那几个函数在
`ZHIJUN_WORKSPACE_ID` 未设时本来就直接返回。把它们搬过来，`backend/mindos`
就不再无条件依赖盒端包了。
"""

from __future__ import annotations

from contextlib import contextmanager


class CapabilityError(RuntimeError):
    """能力不可用。独立版里只作为类型出现，不会被抛出。"""

    def __init__(self, code: str = "CAPABILITY_UNAVAILABLE", status: int = 503) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class BackgroundEnqueueError(CapabilityError):
    """任务登记失败。保留这个类型是因为调用方会把它记进失败记录供人工恢复。"""

    def __init__(self, job_id: str | None = None, cause: Exception | None = None) -> None:
        super().__init__(getattr(cause, "code", "BACKGROUND_ENQUEUE_FAILED"),
                         getattr(cause, "status", 503))
        self.job_id, self.cause = job_id, cause


def register(ident: str, purpose: str) -> None:
    """登记一个后台任务的执行来源。独立版无需登记。"""


@contextmanager
def activated(ident: str):
    """在任务执行期间恢复它的执行来源。独立版直接执行。"""
    yield


def finish(ident: str) -> None:
    """任务结束。独立版无需注销。"""
