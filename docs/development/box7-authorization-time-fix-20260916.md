# 公司 .7 授权时间与错误映射修复

## 范围与验收

- 问题：.7 授权快照 generatedAt 将北京时间标为 UTC，Remote Agent 按未来时间拒绝；TARGET_NOT_ALLOWED 又被 SDK 拒绝为未知代码，界面显示 CONTRACT_MISMATCH。
- 保留账号、绑定、授权校验、业务数据；不重置、不扩大权限。
- Admin：修复旧登录/撤销时间进入 V2 授权发布的 UTC 边界，保留其它业务时间约定。测试覆盖非 UTC 时区。
- 通信层：只兼容明确的目标授权拒绝错误，保持未知错误的严格校验，知君显示权限错误。
- 部署前核对生产代码/运行方式，仅安装本次差异，保存可回退备份；不混入工作区尚未部署的 reset 功能。
- 现存错误快照通过受控授权发布恢复，不直接编辑盒端授权文件或放宽未来时间检查。
- 验收：.7 snapshotValid/livenessFresh 为 true、当前授权 ACK 正常；真实连接验证与自动化测试分开报告。

## 分工与进度

- 主线程：生产核实、最小部署、现存快照恢复及现场验收。
- Admin 子代理：时间边界修复与测试。
- 通信子代理：SDK/知君错误映射与测试。
- [x] 现场只读诊断完成：票据成功、Remote Agent TARGET_NOT_ALLOWED；时间超前约 8 小时。
- [x] 修复和回归。
- [x] Admin 最小部署及 .7 授权恢复。
- [x] 客户端构建/验收与交付说明。

## 已完成的修复

Admin 两个旧业务调用点将本地 naive 时间通过 `astimezone(timezone.utc)` 明确转换；V2 发布入口使用 `_db_time(now)` 转为 UTC-naive 数据库时间。没有全局改变历史时间语义，也没有将所有 naive 时间硬减 8 小时。新回归覆盖 Asia/Shanghai、UTC、UTC-aware、+08-aware 和登录/撤销两条真实发布路径；相关测试共 78 passed。

Electron SDK 1.3.1 仅在 sidecar 私有协议兼容明确的 `TARGET_NOT_ALLOWED`，经过请求关联验证后映射为公开合同已有的 `REQUEST_TARGET_NOT_ALLOWED`；知君显示 `ACCESS_DENIED`。未知错误及非法字段继续拒绝，不自动降级 TURN 或重试。SDK 全量及独立打包消费者测试通过；shell 358 tests，357 passed，1 Windows-only skipped。

## Admin 生产部署

- 服务器：8.138.1.109。
- 程序：`/data/apps/nexusaos_admin_backend-0.1`，9099 API。
- 仅从生产原文件叠加本次 4 个小改动，未拷贝包含前轮 reset 改动的本地整文件。
- 原 auth SHA256：`e304ac253d7d6b3df9f70f35d36bc36baa22e4bae51ca4cdf38e25b8bde06da3`。
- 原 V2 SHA256：`99d0f97197c67a6c0de2de488c374776183ec26f9f25f9a8568df833a52ae2aa`。
- 新 auth SHA256：`935b50df077be57f7a5882253cc4e605a7718caf2169b33ba18d5b21252e03ef`。
- 新 V2 SHA256：`7d9d67068c6a4f4432927743e70dcebf8e7235d7b7e5c780dff29eb8f9b945e3`。
- 备份和安装回执：`/data/apps/nexusaos-admin-auth-time-backup-20260916`。
- 仅对既有 Admin API 进程执行正常 SIGTERM 并按原参数/环境重启；新 PID 2794039，`/openapi.json` HTTP 200。没有重启整台服务器或其它服务。

## .7 授权恢复与现场验收

恢复前：authVersion 46 / applied，57 条 grants，`generatedAt=2026-09-16T10:59:19Z`，实际 UTC 约 03:30。digest 正确但未来时间使 Agent 拒绝。

通过 Admin 原 `publish_account_projection_updates` 发布新版本，严格限定 `.7` 的单一 binding，并验证原 owner、device、ownershipEpoch、client/scopes 集合完全一致；若旧版本或摘要已变化则停止。没有修改盒端快照、修改历史授权时间或扩大权限。审计事件为 `device.authorization.timestamp_repaired`。

- 新 authVersion：47。
- 新 generatedAt：`2026-09-16T03:34:14.256224Z`。
- 新授权更新 ID：`bbadb1cf-0419-4aa7-85e7-f4163a640fac`。
- 正常云同步 ACK：applied，Owner accessStatus=ready。
- 盒端诊断：snapshotValid=true、livenessFresh=true、state=local_snapshot_fresh。
- 云同步按原流程自动重启 Remote Agent 后已连接 gateway；没有更新盒端二进制、Data Engine 或知君 Worker。

实际操作原已安装知君窗口，重新选择 AMD-A2A-248（设备 ID `centauros-5f46a5cc86c2dbfe1de1349c`）连接：

- connection attempt：`b8c52dee1980ad99a47cdeb2`。
- runtime_prepare 26 ms / ok。
- direct_ticket 432 ms / ok。
- direct_connect 4214 ms / ok（包含 ticket，不重复相加）。
- context_authorize 466 ms / ok。
- total_connect 4706 ms / ok，selectedPath=DIRECT。
- `.7` 同期 11:39:52–53 日志出现 p2p.answer/p2p.path，无本次 TARGET_NOT_ALLOWED。

验收仅涵盖连接和工作区授权，未代替对话、RAG、本体写入等业务全链路测试。

## 安装包

正式 macOS arm64 0.1.31 包生成完成，沿用 production 打包流水线；包含新 SDK 1.3.1 和错误映射。原客户端无需重装即可恢复连接，安装新版用于准确错误展示。

- `frontend/shell/release/Zhijun-0.1.31-mac-arm64.dmg`
- DMG SHA256：`bd410e6dda54d3ee5e40c6e95a37359a24d3d1c274095fd0a4ff05aca9ae28bb`。
- ZIP SHA256：`703ef284e2426f270256cd721e664dd291eff2f993eab9588cf5458d805aedba`。
- shell、web desktop、Electron E2E、前端构建、包结构/签名验证、迁移位置启动冒烟均通过。
- Developer ID 签名完成；现有流水线明确禁用 notarization，本包未做 Apple 公证，不宣称已通过公证。
- Windows 安装包未生成；后续 Windows 打包将采用相同 vendor SDK 1.3.1。
