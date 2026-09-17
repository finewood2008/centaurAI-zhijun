# 2026-09-17 盒子列表加载失败

## 计划与验收

- [x] 使用当前桌面登录身份，只读复现签名 bootstrap 请求；不输出令牌或私钥。
- [x] 核对 Admin ORM、迁移文件及部署顺序。
- [x] 修复客户端对服务端异常的分类，补充回归测试。
- [ ] 取得可用部署连接，检查生产 schema 是否存在部分迁移，备份后补齐缺失项。
- [ ] 同一桌面账号重试签名 bootstrap，确认成功返回盒子列表。

影响文件：`frontend/shell/production/consumer-client.cjs`、
`frontend/shell/tests/consumer.test.cjs` 及本文。
线上依赖为 Admin 的 `admin-backend/sql/20260916_consumer_reset_v1.sql`。
验收要求：恢复列表查询、保持账号及设备授权校验、保留登录状态、错误提示不暴露 SQL。

## 实际根因

`GET /app-api/v1/sync/bootstrap` 返回 HTTP 200，但正文为旧式异常 envelope：
`code:500`、`success:false`，没有 V2 的请求关联字段，时间也不是 UTC Z 格式。
SQL 错误为 `Unknown column ...recommission_reset_id in field list`，来自
`nexus_consumer_device_pairing_v2_sessions`。

Admin ORM 已映射 `recommission_reset_id`，生产数据库尚缺该列；查询配对记录时失败，
没有返回 `data.devices`。客户端随后在 V2 请求 ID 校验处将错误误报为 `CONTRACT_MISMATCH`。

Admin `docs/device-reset-recommission-v1.md` 明确要求先执行 reset migration 再部署代码，
即使 reset 功能关闭也必须迁移。这里不是盒子离线、解绑或授权丢失的证据。

## 客户端改动与验证

V2 路由收到明确服务端失败（`success:false` 且 HTTP/业务 code 为 5xx）时，
在校验 V2 元数据前映射为 `ACCOUNT_SERVICE_UNAVAILABLE`。
已有的类型化 pairing 错误仍走原路径；成功响应、授权状态和请求 ID 继续严格校验。
不反射远端 SQL 消息，不清除登录，不回退旧设备列表接口。

新增用例先复现原错误，再验证 HTTP 200/500 两种异常包装、登录保留、诊断信息脱敏，
以及服务恢复后的列表重试。consumer、production-adapter、runtime 共 101 项测试通过，
`git diff --check` 通过。尚未重新打包或替换已安装客户端。

## 线上恢复步骤（待执行）

服务器：`8.138.1.109`；应用目录：`/data/apps/nexusaos_admin_backend-0.1`。
本次直接 SSH 使用现有认证未成功，尚未修改生产数据库。

1. 核对线上 migration 与当前代码一致，确认 Consumer V2、Console Claim 的前置迁移已完成。
2. 检查 `information_schema`，确认新列、索引及 reset 表是否部分存在；保存相关 schema 和备份。
3. 若全未存在，应用已有 `20260916_consumer_reset_v1.sql`：新增 nullable
   `recommission_reset_id VARCHAR(36)`、`idx_consumer_pairing_reset`，创建
   `nexus_consumer_device_resets`（33 列及既定索引/约束）。若部分存在，仅补缺失项，不能盲目重跑。
4. MySQL DDL 自动提交；本迁移只增加结构，不删除历史、不回填授权，不开启 reset 功能。
5. 检查新列的类型与 NULL 属性、新表约束，再用原账号请求 bootstrap，确认现代成功 envelope
   及设备列表；刷新已安装客户端。数据库修复本身不依赖安装新版客户端。

查询新列的无数据验收：

```sql
SELECT recommission_reset_id
FROM nexus_aos.nexus_consumer_device_pairing_v2_sessions
WHERE 1 = 0;
```

另外发现本地 bootstrap 对数组数量的限制未体现在 Admin DTO 中；实际现场没有返回数组，
因此这不是本次根因，本次不调整这些限制。
