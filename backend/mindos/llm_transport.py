"""模型服务 HTTP 传输适配器。

QA、材料派生和标签生成统一经 `allowed_urlopen` 发起，以保持请求参数与错误
处理一致。遵守本机的禁用域名约束；在线模型请求不自动重定向。
"""
from __future__ import annotations

import urllib.request
import urllib.error
import json
from urllib.parse import urlsplit


def _check_destination(url):
    try:
        # DNS applies IDNA too: Unicode full stops / full-width letters must
        # not disguise a forbidden host before it reaches the network.
        host = (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError):
        raise ValueError("模型服务地址不合法") from None
    if any(host == h or host.endswith("." + h) for h in ("claude.ai", "anthropic.com")):
        raise ValueError("本机禁止访问此模型服务")


class _NoModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward personal payloads/authorization headers to a new host.
        raise urllib.error.HTTPError(req.full_url, code, "模型请求不允许重定向", headers, fp)


def allowed_urlopen(
    url: str,
    *,
    channel: str,
    store=None,
    timeout: float | None = None,
    headers: dict | None = None,
    data: bytes | None = None,
    method: str | None = None,
):
    """发起模型服务请求。

    在线聊天禁止自动重定向，以免向未经授权的接收服务转发内容。
    """
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    _check_destination(url)
    if channel == "chat":
        from .zhijun.routing import EGRESS_PERMIT
        from fastapi import HTTPException
        permit = EGRESS_PERMIT.get()
        if not callable(permit):
            raise HTTPException(409, {"code": "EGRESS_NOT_AUTHORIZED", "detail": "请在对话中预览并授权在线使用；旧问答入口不能绕过资料授权"})
        permit()
        return urllib.request.build_opener(_NoModelRedirect()).open(req, timeout=timeout)
    if channel == "diagnostic":
        # The settings connection test has no user data and cannot be used as an
        # alternative transport for personal payloads.
        from .model_runtime import _TEST_MESSAGES
        payload = json.loads(data or b"{}")
        if payload.get("messages") != _TEST_MESSAGES or set(payload) != {"model", "messages", "stream"}:
            raise ValueError("连接测试只允许固定的无个人数据探测文本")
        return urllib.request.build_opener(_NoModelRedirect()).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
