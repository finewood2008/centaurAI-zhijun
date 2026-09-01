"""Consumer API 统一错误契约：{error: {code, message, details?}}。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ConsumerApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


ERROR_AUTH_REQUIRED = ("AUTH_REQUIRED", "缺少或无效的访问凭证")
ERROR_AUTH_INVALID = ("AUTH_INVALID", "访问凭证无效")
ERROR_TOKEN_EXPIRED = ("TOKEN_EXPIRED", "访问凭证已过期，请用 Refresh 凭证续期")
ERROR_CLIENT_REVOKED = ("CLIENT_REVOKED", "客户端已被撤销")
ERROR_REFRESH_INVALID = ("REFRESH_INVALID", "Refresh 凭证无效或已过期")
ERROR_CLIENT_NOT_FOUND = ("CLIENT_NOT_FOUND", "客户端不存在")
ERROR_ACCOUNT_NOT_FOUND = ("ACCOUNT_NOT_FOUND", "账号不存在")
ERROR_DEVICE_NOT_FOUND = ("DEVICE_NOT_FOUND", "设备不存在")
ERROR_DEVICE_NOT_OWNED = ("DEVICE_NOT_OWNED", "设备不属于当前账号")
ERROR_DEVICE_ALREADY_OWNED = ("DEVICE_ALREADY_OWNED", "设备已被其他账号认领")
ERROR_TICKET_ACTIVE = ("TICKET_ACTIVE", "连接会话仍处于活动窗口，请复用原票据或等待其过期")
ERROR_SMS_CODE = ("SMS_CODE_INVALID", "验证码错误或已过期")
ERROR_STEP_UP_REQUIRED = ("STEP_UP_REQUIRED", "敏感操作需要二次验证（Step-up）")
ERROR_STEP_UP_INVALID = ("STEP_UP_INVALID", "Step-up 凭证无效、已用或已过期")
ERROR_PROTOCOL_UPGRADE = ("PROTOCOL_UPGRADE_REQUIRED", "客户端协议版本过旧，需重新确认最新协议")
ERROR_PROTOCOL_UNSUPPORTED = ("PROTOCOL_UNSUPPORTED", "客户端协议版本不受支持")
ERROR_VALIDATION = ("VALIDATION_ERROR", "请求参数不合法")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ConsumerApiError)
    async def _consumer_api_error_handler(_request: Request, exc: ConsumerApiError) -> JSONResponse:
        body: dict = {"error": {"code": exc.code, "message": exc.message}}
        if exc.details is not None:
            body["error"]["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)
