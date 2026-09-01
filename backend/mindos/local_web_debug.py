"""MindOS 本机 Web 调试门。

该模块只决定是否能以本机开发调试身份进入 MindOS Web 业务接口。
真实 Consumer 连接票据将在阶段 2 接入同一鉴权边界；此处绝不伪造
account、Owner 或设备归属。
"""

from __future__ import annotations

import ipaddress
import os
from typing import Mapping


ACCESS_MODE_LOCAL_DEBUG = "local_debug"
ACCESS_MODE_TICKET_REQUIRED = "connectivity_ticket_required"


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_loopback_host(host: str | None) -> bool:
    """仅接受字面 loopback 地址，避免把 hostname 或代理配置当作可信边界。"""
    try:
        return ipaddress.ip_address((host or "").strip()).is_loopback
    except ValueError:
        return False


def access_context(
    *,
    bind_host: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | bool]:
    """返回不含秘密的 Web 访问上下文。

    以传入环境字典而不是模块级常量判断，便于测试且支持关闭 Gate 后立即
    使后续请求失效。未通过任一条件时，调用方必须改走阶段 2 的票据校验。
    """
    env = environ if environ is not None else os.environ
    runtime_env = (env.get("MINDOS_RUNTIME_ENV") or "production").strip().lower()
    debug_requested = _enabled(env.get("MINDOS_LOCAL_WEB_DEBUG_ACCESS"))
    loopback_bind = is_loopback_host(bind_host)

    if runtime_env == "development" and debug_requested and loopback_bind:
        return {
            "mode": ACCESS_MODE_LOCAL_DEBUG,
            "localDebug": True,
            "scope": "mindos:local-debug",
        }

    if runtime_env != "development":
        reason = "runtime_not_development"
    elif not debug_requested:
        reason = "local_debug_disabled"
    else:
        reason = "server_not_loopback"
    return {
        "mode": ACCESS_MODE_TICKET_REQUIRED,
        "localDebug": False,
        "reason": reason,
    }
