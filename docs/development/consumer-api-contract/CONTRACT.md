# Consumer API v1 合同（Stateful Strict Mock，冻结版）

- 合同版本：1.0.0
- API 版本：1.0.0-mock
- 协议版本：`1`（旧客户端隐式同意值：`0`）
- 生成方式：`.venv\\Scripts\\python.exe scripts/export_consumer_contract.py`
- 一致性：`openapi.json` 必须与 `consumer_api.create_app().openapi()` 一致（由
  `backend/tests/test_consumer_contract.py` 守护）；任何破坏性变更必须先提升合同版本。

## 1. 概述

Consumer Account / Client / Session / Device Ownership / Pairing / OTA / 同步状态
只由 Consumer API 管理（云端权威）。客户端与 MindOS 不得伪造 Owner 或权威
`device_id`。本实现为 Stateful Strict Mock，供 PC（Electron）/外部 App/SDK 联调；
发布包整体排除 `consumer_api`。

- Base URL（Mock）：`http://127.0.0.1:8801/api/consumer/v1`
- JWKS：`/.well-known/jwks.json`（唯一公开验签材料，不含签发私钥）

## 2. 认证与凭证

- 手机号验证码登录即注册：`POST /auth/phone-code`（Mock 返回 `devOnlyCode`，仅联调）→
  `POST /auth/login`（携带 `protocolVersion`）→ 返回 `accessToken` / `refreshToken` /
  `client` / `account` / `protocol`。
- 业务请求携带 `Authorization: Bearer <accessToken>`。
- `POST /auth/refresh` 用 `refreshToken` 换新对；`POST /auth/logout` 撤销当前 Client 会话。
- 敏感操作（移除其他终端）要求 Step-up：`/auth/step-up/sms/send` + `/auth/step-up/sms/verify`
  后携带 `X-Nexus-Step-Up` 凭证，仅允许重放原始 `client.revoke` 动作（requestDigest 绑定）。

## 3. 错误契约

所有错误统一为 `{"error": {"code", "message", "details?"}}`：

| code | HTTP | message | 场景 |
| --- | --- | --- | --- |
| `AUTH_REQUIRED` | 401 | 缺少或无效的访问凭证 | 缺少或无效的 Bearer 凭证 |
| `AUTH_INVALID` | 401 | 访问凭证无效 | 访问凭证无效 |
| `TOKEN_EXPIRED` | 401 | 访问凭证已过期，请用 Refresh 凭证续期 | Access 过期，用 Refresh 续期 |
| `CLIENT_REVOKED` | 401 | 客户端已被撤销 | 客户端已撤销 |
| `REFRESH_INVALID` | 401 | Refresh 凭证无效或已过期 | Refresh 凭证无效或已过期 |
| `CLIENT_NOT_FOUND` | 404 | 客户端不存在 | 客户端不存在 |
| `ACCOUNT_NOT_FOUND` | 404 | 账号不存在 | 账号不存在 |
| `DEVICE_NOT_FOUND` | 404 | 设备不存在 | 设备不存在 |
| `DEVICE_NOT_OWNED` | 403 | 设备不属于当前账号 | 设备不属于当前账号 |
| `DEVICE_ALREADY_OWNED` | 409 | 设备已被其他账号认领 | 设备已被其他账号认领 |
| `TICKET_ACTIVE` | 409 | 连接会话仍处于活动窗口，请复用原票据或等待其过期 | 连接会话仍在活动窗口，复用原票据或等待过期 |
| `SMS_CODE_INVALID` | 400 | 验证码错误或已过期 | 验证码错误或已过期 |
| `STEP_UP_REQUIRED` | 403 | 敏感操作需要二次验证（Step-up） | 敏感操作需 Step-up |
| `STEP_UP_INVALID` | 403 | Step-up 凭证无效、已用或已过期 | Step-up 凭证无效/已用/过期 |
| `PROTOCOL_UPGRADE_REQUIRED` | 426 | 客户端协议版本过旧，需重新确认最新协议 | 协议过旧需重新确认 |
| `PROTOCOL_UNSUPPORTED` | 422 | 客户端协议版本不受支持 | 协议版本不受支持 |
| `VALIDATION_ERROR` | 422 | 请求参数不合法 | 请求参数不合法 |

## 4. 协议确认

- 客户端在登录时声明 `protocolVersion`：`1` 为当前版本，`0` 表示旧客户端
  未携带（隐式同意）。
- 版本裁决：等于 `0` 或 `1` → 通过；`0 < v < 1` →
  `426 PROTOCOL_UPGRADE_REQUIRED`（details 含 `latestVersion`）；`v > 1` →
  `422 PROTOCOL_UNSUPPORTED`。
- 每次登录按 client 记录协议确认审计（首次确认与升级重确认）。
- 协议发现：`GET /auth/protocol`。

## 5. 幂等约定

- `POST /devices/{device_id}/claim`：重复认领同一设备返回已有所有权（幂等）；
  已被其他账号认领 → `409 DEVICE_ALREADY_OWNED`。
- `POST /connectivity/sessions`：携带 `idempotencyKey`；活动窗口内重复创建 →
  `409 TICKET_ACTIVE`（复用原票据或等待过期），不重复签发。

## 6. 连接票据与 JWKS

- `POST /connectivity/sessions` 签发短期一次性票据（nonce 单次使用，取走即失效，重放被拒）。
- 验签方仅用 JWKS 公开密钥校验 `iss` / `aud` / `nbf` / `iat` / `exp` / account / client /
  device / scope / nonce。票据不是持久 App Token，不可作为业务请求凭证。

## 7. 同步模型

- `GET /sync/bootstrap`：全量事件（`cursor` + `events[]`）。
- `GET /sync/changes?cursor=N`：增量事件，`cursor` 前进。
- 事件类型：`device_added` / `device_renamed` / `device_ota_status` 等（`consumer_sync_events`）。
- 客户端前台 3/5 秒协调，事件 reducer 幂等应用。

## 8. 设备模型

- `GET /devices`、`GET /devices/{device_id}`、`PATCH /devices/{device_id}`（重命名）、
  `POST /devices/{device_id}/ota`。
- 一台设备同一时间只能有一个 Consumer Owner；跨账号访问 → `403 DEVICE_NOT_OWNED`。

## 9. Mock 管理面（仅联调，生产包排除）

- `GET /__mock/state`：状态快照；`POST /__mock/devices`：创建可认领设备；
  `GET /__mock/revocations?since=N`：撤销事件流（供 Consumer Adapter 轮询）。
- `__mock` 前缀、`devOnlyCode` 与固定验证码禁止进入生产制品（发布守卫守护）。

## 10. 安全约束

- 不允许旧 `/nexus/*`、Admin、企业 Token、明文 Wi-Fi、LAN/direct-local 回退。
- 客户端不得持久化权威 `device_id` 伪造凭据；Mock 私钥/测试向量仅限测试夹具。
