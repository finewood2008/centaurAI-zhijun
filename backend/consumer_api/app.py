"""Consumer API v1 Mock 的 FastAPI 应用与路由。

前缀 /api/consumer/v1；JWKS 位于 /.well-known/jwks.json；
__mock__ 前缀仅用于 Mock 管理（建设备、状态快照、撤销事件流）。

发布隔离：consumer_api 整体属于 Mock/联调包，runtime 打包必须排除，
且构建守卫禁止私钥/devOnlyCode/__mock 进入生产制品。__mock 路由与
devOnlyCode 仅在 MINDOS_CONSUMER_MOCK_DISABLED=1 之外的默认状态下可用，
防御性兜底（见 scripts/check_release_guard.py）。
"""

from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from . import signing
from .errors import (
    ERROR_AUTH_REQUIRED,
    ERROR_SMS_CODE,
    ERROR_STEP_UP_REQUIRED,
    ConsumerApiError,
    install_error_handlers,
)
from .schemas import (
    ClaimDeviceRequest,
    ConnectivitySessionRequest,
    CreateDeviceRequest,
    PhoneLoginRequest,
    RefreshRequest,
    RenameDeviceRequest,
    StepUpSendRequest,
    StepUpVerifyRequest,
)
from .store import MOCK_SMS_CODE, ConsumerState, stepup_request_digest, CURRENT_PROTOCOL_VERSION

API_PREFIX = "/api/consumer/v1"


def _mock_enabled() -> bool:
    """__mock 管理面与固定验证码的开关（默认开启；发布包整体排除 consumer_api）。"""
    value = (os.environ.get("MINDOS_CONSUMER_MOCK_DISABLED") or "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


class PhoneCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(..., min_length=5, max_length=32, pattern=r"^\+?[0-9]{5,32}$")


class OtaStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    otaStatus: str = Field(..., min_length=1, max_length=64)


def create_app(state: ConsumerState | None = None) -> FastAPI:
    app = FastAPI(title="Consumer API v1 (Stateful Strict Mock)", version="1.0.0-mock")
    # 仅 Mock 联调：允许浏览器端完成登录/认领/换票闭环；发布包整体排除 consumer_api。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.consumer = state if state is not None else ConsumerState()
    install_error_handlers(app)

    def require_client(request: Request, authorization: str | None = Header(default=None)):
        if not authorization or authorization.lower().partition(" ")[0] != "bearer":
            raise ConsumerApiError(401, *ERROR_AUTH_REQUIRED)
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise ConsumerApiError(401, *ERROR_AUTH_REQUIRED)
        client = app.state.consumer.authenticate_access(token)
        request.state.consumer_client = client
        return client

    @app.post(f"{API_PREFIX}/auth/phone-code")
    def send_phone_code(req: PhoneCodeRequest):
        del req
        # devOnlyCode 仅 Mock 联调返回；发布包排除 consumer_api（构建守卫兜底）。
        return {
            "success": True,
            "expiresIn": 300,
            "devOnlyCode": MOCK_SMS_CODE if _mock_enabled() else None,
        }

    @app.get(f"{API_PREFIX}/auth/protocol")
    def get_protocol():
        """客户端发现当前协议版本；登录后返回的 protocol 字段用于首次/升级确认。"""
        return app.state.consumer.protocol_info()

    @app.post(f"{API_PREFIX}/auth/login")
    def login(req: PhoneLoginRequest):
        if req.code != MOCK_SMS_CODE:
            raise ConsumerApiError(400, *ERROR_SMS_CODE)
        return app.state.consumer.register_or_login(req.phone, req.clientName, req.protocolVersion)

    @app.post(f"{API_PREFIX}/auth/refresh")
    def refresh(req: RefreshRequest):
        return app.state.consumer.refresh_tokens(req.refreshToken)

    @app.post(f"{API_PREFIX}/auth/logout", status_code=204)
    def logout(request: Request, _client: dict = Depends(require_client)):
        authorization = request.headers.get("authorization") or ""
        token = authorization.split(" ", 1)[1].strip()
        app.state.consumer.logout(token)

    @app.get(f"{API_PREFIX}/auth/clients")
    def list_clients(client: dict = Depends(require_client)):
        return {"clients": app.state.consumer.list_clients(client["account_id"])}

    @app.post(f"{API_PREFIX}/auth/clients/{{client_id}}/revoke")
    def revoke_client(client_id: str, client: dict = Depends(require_client)):
        result = app.state.consumer.revoke_client(client["account_id"], client_id)
        return {"success": True, "newEpoch": result["newEpoch"], "closedSessions": result["closedSessions"]}

    @app.post(f"{API_PREFIX}/auth/step-up/sms/send")
    def stepup_send(req: StepUpSendRequest, client: dict = Depends(require_client)):
        return app.state.consumer.begin_stepup(
            account_id=client["account_id"],
            client_id=client["client_id"],
            action=req.action,
            target={"clientId": req.target.clientId},
            request_digest=req.requestDigest,
        )

    @app.post(f"{API_PREFIX}/auth/step-up/sms/verify")
    def stepup_verify(req: StepUpVerifyRequest, client: dict = Depends(require_client)):
        return app.state.consumer.verify_stepup(
            account_id=client["account_id"],
            client_id=client["client_id"],
            challenge_id=req.challengeId,
            code=req.code,
        )

    @app.delete(f"{API_PREFIX}/auth/clients/{{client_id}}")
    def remove_client(
        client_id: str,
        _client: dict = Depends(require_client),
        x_nexus_step_up: str | None = Header(default=None),
    ):
        """移除其他终端：必须携带 Step-up 凭证，且仅重放原始 client.revoke 动作。"""
        if not (x_nexus_step_up or "").strip():
            raise ConsumerApiError(403, *ERROR_STEP_UP_REQUIRED)
        digest = stepup_request_digest(
            "client.revoke",
            client_id,
            "DELETE",
            f"{API_PREFIX}/auth/clients/{quote(client_id, safe='')}",
        )
        result = app.state.consumer.redeem_client_revoke(
            account_id=_client["account_id"],
            current_client_id=_client["client_id"],
            step_up_token=x_nexus_step_up.strip(),
            target_client_id=client_id,
            request_digest=digest,
        )
        return {"revokedClientId": result["revokedClientId"], "revokedAt": result["revokedAt"]}

    @app.get(f"{API_PREFIX}/devices")
    def list_devices(client: dict = Depends(require_client)):
        return {"devices": app.state.consumer.list_devices(client["account_id"])}

    @app.get(f"{API_PREFIX}/devices/{{device_id}}")
    def get_device(device_id: str, client: dict = Depends(require_client)):
        return app.state.consumer.get_device(client["account_id"], device_id)

    @app.patch(f"{API_PREFIX}/devices/{{device_id}}")
    def rename_device(device_id: str, req: RenameDeviceRequest, client: dict = Depends(require_client)):
        return app.state.consumer.rename_device(client["account_id"], device_id, req.name)

    @app.post(f"{API_PREFIX}/devices/{{device_id}}/claim")
    def claim_device(device_id: str, req: ClaimDeviceRequest, client: dict = Depends(require_client)):
        return app.state.consumer.claim_device(client["account_id"], device_id, req.idempotencyKey)

    @app.post(f"{API_PREFIX}/devices/{{device_id}}/ota")
    def update_ota(device_id: str, req: OtaStatusRequest, client: dict = Depends(require_client)):
        return app.state.consumer.update_device_ota(client["account_id"], device_id, req.otaStatus)

    @app.get(f"{API_PREFIX}/sync/bootstrap")
    def sync_bootstrap(client: dict = Depends(require_client)):
        return app.state.consumer.bootstrap(client["account_id"])

    @app.get(f"{API_PREFIX}/sync/changes")
    def sync_changes(cursor: int = 0, client: dict = Depends(require_client)):
        return app.state.consumer.changes(client["account_id"], cursor)

    @app.post(f"{API_PREFIX}/connectivity/sessions")
    def create_connectivity_session(req: ConnectivitySessionRequest, client: dict = Depends(require_client)):
        result = app.state.consumer.create_connectivity_session(
            account_id=client["account_id"],
            client_id=client["client_id"],
            device_id=req.deviceId,
            idempotency_key=req.idempotencyKey,
        )
        return {"deviceId": req.deviceId, **result}

    @app.get("/.well-known/jwks.json")
    def jwks():
        return {"keys": [signing.public_jwks()]}

    if _mock_enabled():
        _register_mock_routes(app)

    return app


def _register_mock_routes(app: FastAPI) -> None:
    """仅 Mock 联调可用的管理面（建设备/状态/撤销事件流）；发布包不包含。"""

    @app.get(f"{API_PREFIX}/__mock/state")
    def mock_state():
        return app.state.consumer.snapshot()

    @app.get(f"{API_PREFIX}/__mock/revocations")
    def mock_revocations(since: int = 0):
        return {"revocations": app.state.consumer.revocations_since(since)}

    @app.post(f"{API_PREFIX}/__mock/devices")
    def mock_create_device(req: CreateDeviceRequest):
        device = app.state.consumer.create_device(req.deviceId, req.name, req.otaStatus)
        return {"success": True, "device": app.state.consumer._public_device(device)}


app = create_app()
