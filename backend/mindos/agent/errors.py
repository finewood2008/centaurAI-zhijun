"""Agent Gateway 错误契约（AG-01）。

统一错误响应：
{
  "traceId": "atr_...",
  "error": {"code": "...", "message": "...", "retryable": false}
}
"""
from __future__ import annotations

from typing import Mapping, Optional


class AgentError(Exception):
    """Gateway 统一业务错误。status_code + 稳定错误码 + 人类可读消息。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        headers: Optional[Mapping[str, str]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = dict(headers or {})


def error_payload(trace_id: str, error: AgentError) -> dict:
    return {
        "traceId": trace_id,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        },
    }


def ok_payload(trace_id: str, data: dict) -> dict:
    return {"traceId": trace_id, "data": data}
