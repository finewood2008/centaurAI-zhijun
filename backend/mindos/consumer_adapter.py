"""MindOS ↔ Consumer API（Mock）适配边界。

Consumer API 是云端权威：本模块只做两件事——
1. 拉取 Consumer API 的公开 JWKS，供 MindOS 真实验签器配置（不保存签发私钥）；
2. 轮询撤销事件并翻译到本机 connectivity_store.mark_revoked（含设备上下文
   失效），使「撤销后 5 秒内断开」在真实 Consumer Webhook 接入前可端到端验证。

撤销同步在生产启动期由 server 配置 base_url，并在连接状态 GC 线程中每 5 秒
轮询一次；以 Consumer 事件 seq 做持久化幂等（重复拉取同一事件不会再次
递增 epoch）。生产接入后此边界对接真实 Consumer Webhook/轮询，语义不变；
不能改为信任 App Token 或本地网络身份。
"""

from __future__ import annotations

import json
import urllib.request

from . import device_context
from .stores import connectivity_store

_configured_base_url: str | None = None

REVOCATION_CURSOR_KEY = "revocations"


def configure_revocation_sync(*, base_url: str | None) -> None:
    """启动期配置撤销事件源；None 关闭轮询（默认保持关闭）。"""
    global _configured_base_url
    _configured_base_url = (base_url or "").strip() or None


def is_revocation_sync_configured() -> bool:
    return _configured_base_url is not None


def fetch_jwks(*, base_url: str | None = None, jwks_getter=None) -> dict:
    """获取 Consumer API 公开 JWKS。离线测试可传入 jwks_getter。"""
    if jwks_getter is not None:
        return jwks_getter()
    url = f"{base_url.rstrip('/')}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync_revocations(
    *,
    base_url: str | None = None,
    since: int | None = None,
    revocations_getter=None,
    cursor_key: str = REVOCATION_CURSOR_KEY,
) -> dict:
    """拉取撤销事件并幂等应用到本机，返回 {applied, newSince, cursor}。

    每个撤销条目按 Consumer 事件 seq 去重（consumer_applied_events），首次
    应用才调用 mark_revoked（避免重复递增 epoch），并把游标持久化到
    connectivity_store.consumer_sync_cursors，重启后可续传。
    """
    if since is None:
        since = connectivity_store.get_sync_cursor(cursor_key)
    if revocations_getter is not None:
        payload = revocations_getter(since)
    else:
        source = (base_url or _configured_base_url).rstrip("/")
        url = f"{source}/api/consumer/v1/__mock/revocations?since={since}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    # 「去重标记 + 撤销 + epoch + 关闭会话 + 游标推进」由 store 在单个 SQLite 事务中
    # 原子提交，杜绝部分落盘：重启后从原游标续传，撤销不会因进程中断而被永久跳过。
    result = connectivity_store.sync_apply_revocations(
        cursor_key=cursor_key,
        since=since,
        entries=payload.get("revocations", []),
    )
    # 设备运行时寄存器（内存态）失效与持久化事务解耦；持久化撤销已在事务内提交。
    for device_id in result["affectedDeviceIds"]:
        device_context.get_device_registry().invalidate(device_id)
    return {
        "applied": result["applied"],
        "newSince": result["newSince"],
        "cursor": result["cursor"],
    }
