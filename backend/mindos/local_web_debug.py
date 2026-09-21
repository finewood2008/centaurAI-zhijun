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
ACCESS_MODE_STANDALONE = "standalone"
ACCESS_MODE_TICKET_REQUIRED = "connectivity_ticket_required"

# 独立发行版：装在用户自己电脑上的桌面应用，没有云控制面，也就没有票据可换。
# 开启条件是发行版自己声明（ZHIJUN_STANDALONE）**且**服务只绑在回环地址上。
# 不按 MINDOS_RUNTIME_ENV 推断：那会让某个盒子因为环境变量写错而静默失去票据校验。
#
# 关于 PRD 提到的「首次本机配对码」：这里**没有做**，是想清楚之后的决定。
# 本机 Web 应用真正的边界是三条——只绑回环（外部网络进不来）、写操作要 CSRF 头
# （浏览器跨站发不动）、浏览器同源策略。配对码要防的是「同一台机器上的另一个进程」，
# 可那个进程以同一个用户身份运行，配对码存在磁盘上它照样读得到，而且应用自己也得
# 把码交给浏览器才能用。挡不住它要挡的人，却给每个真实用户加一道门槛。
# 真正该加的是操作系统级别的隔离，不是一串自己发给自己的码。


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
    standalone = _enabled(env.get("ZHIJUN_STANDALONE"))
    loopback_bind = is_loopback_host(bind_host)

    if standalone and loopback_bind:
        return {
            "mode": ACCESS_MODE_STANDALONE,
            "localDebug": False,
            "scope": "zhijun:standalone",
        }

    if runtime_env == "development" and debug_requested and loopback_bind:
        return {
            "mode": ACCESS_MODE_LOCAL_DEBUG,
            "localDebug": True,
            "scope": "mindos:local-debug",
        }

    if standalone:
        reason = "server_not_loopback"        # 声明了独立发行版，却没绑在回环上
    elif runtime_env != "development":
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
