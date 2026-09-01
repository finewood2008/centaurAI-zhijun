"""模型服务 HTTP 传输适配器。

QA、材料派生和标签生成统一经 `allowed_urlopen` 发起，以保持请求参数与错误
处理一致。目标地址仅在运行时配置保存阶段做 URL 格式校验，不施加主机范围限制。
"""
from __future__ import annotations

import urllib.request


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

    `channel` 和 `store` 保留为调用兼容参数，不参与目标地址限制。
    """
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    return urllib.request.urlopen(req, timeout=timeout)
