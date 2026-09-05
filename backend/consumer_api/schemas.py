"""Consumer API v1 Mock 的请求/响应 DTO。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PhoneLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(..., min_length=5, max_length=32, pattern=r"^\+?[0-9]{5,32}$")
    code: str = Field(..., min_length=4, max_length=8)
    clientName: str = Field("", max_length=64)
    # 客户端声明并同意签署的业务/隐私协议版本；0 表示旧版客户端未携带（隐式同意）。
    protocolVersion: int = Field(0, ge=0, le=64)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refreshToken: str = Field(..., min_length=8, max_length=256)


class RenameDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=64)


class ClaimDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotencyKey: str = Field("", max_length=128)


class ConnectivitySessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deviceId: str = Field(..., min_length=1, max_length=128)
    idempotencyKey: str = Field(..., min_length=1, max_length=128)


class StepUpTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clientId: str = Field(..., min_length=1, max_length=128)


class StepUpSendRequest(BaseModel):
    """敏感操作 Step-up 发送：绑定 action / target / requestDigest。"""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., min_length=1, max_length=64)
    target: StepUpTarget
    requestDigest: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class StepUpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challengeId: str = Field(..., min_length=1, max_length=128)
    code: str = Field(..., min_length=4, max_length=8)


class CreateDeviceRequest(BaseModel):
    """Mock 专属：创建一个可被认领的未绑定设备。"""

    model_config = ConfigDict(extra="forbid")

    deviceId: str = Field(..., min_length=1, max_length=128)
    name: str = Field("", max_length=64)
    otaStatus: str = Field("up_to_date", max_length=64)
