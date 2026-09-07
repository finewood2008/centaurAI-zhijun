# 知君完整工作区盒端部署记录

2026-09-06 13:13（Asia/Shanghai）已完成盒端匹配部署。正式 Consumer / SDK / UI 验收仍待生产 Admin 发布新应用登记；服务健康不能替代业务验收。

## 固定版本与产物

| 项目 | 版本 / 证据 |
| --- | --- |
| 桌面及知君领域 | 代码 `58dac31`；发布源 HEAD `735e3411bba7cccc8079392fa44ee8cdb9b4b5f0` |
| DE Gateway / 能力 | `015c65967d4d2cd370067e16d65111bfdcd820a4` |
| Agent | `5f5f4c914ee15736ae4d189329018b0cd2bdd38b` |
| Admin 应用登记 | `44a0950` 已推送，生产未发布 |
| 归档 | `full-product-release.tar.gz`，10,456,709 字节，363 个源文件 |
| 归档 SHA-256 | `03d8152cc0f236c94f8d089d419c50144ac1e0dd7701367de18ca78a4f646de6` |
| Agent 二进制 SHA-256 | `1b44da2a688b5acd64121ad55c14b178968de85bef89a223858d009725e6c574` |
| 实际 Agent manifest SHA-256 | `41a54e50b26df4e3baac153ab2f47aa65e3f7fce0d3b8e5c86921d6c7834d552` |

构建时三个源工作区均 clean；逐文件校验与已通过隔离硬件测试的候选源码完全一致。后续仅补充部署文档，不改变已部署代码。Agent manifest 保留设备已有两个应用的全部配置，只加入 `zhijun-desktop`，因此实际 manifest 的哈希与仓内完整示例不同。未复制示例中的其他应用或调整既有手机配额。

发布目录：`/home/user/apps/centuarai-data-engine/releases/20260906T051053Z-zhijun-015c659`。DE 使用其 `de/backend`，领域 worker 使用 `zhijun/backend`，共享操作表使用 `zhijun/frontend/shared/product-operations.json`。Python 3.14 依赖继续复用原发布的既有 `.venv`，离线模型缓存通过明确链接复用；本次工件是源码发布包，不是完全自包含的操作系统镜像。预编译 Agent 记录了内容哈希与构建源 HEAD，源回执不声称具备额外的可重现构建证明。

原数据根 `/home/user/centaurAI-database` 和其 `secrets` 保持原值。新的 Gateway 任务、密钥和领域目录分别为 `/home/user/.local/share/zhijun/{gateway-jobs,secrets,workspaces}`，运行套接字在 `/run/user/1000/zhj`。配置通过新增 `96-zhijun-workspace-v2.conf` 生效；原 90/95 drop-in 保留，`MINDOS_LOCAL_WEB_DEBUG_ACCESS=0`、`MINDOS_REDACTION_MODE=enforce`，解析运行时离线。

## 备份与回退

备份目录：`/home/user/apps/centuarai-data-engine/patch-backups/20260906T051053Z-zhijun-015c659-full-product`。在 DE 停止期间以 SQLite backup API 一致备份 16 个数据库；旧 Agent 二进制、实际 manifest、90/95 配置均另存。发布前已用新的二进制验证合并后的实际配置；临时验证配置已删除，未手工读取或输出 live Agent 私钥内容。

若需回退，先停止桌面业务和 Agent，再停止 DE；移走新增的 `96-zhijun-workspace-v2.conf`、执行用户级 daemon-reload，恢复备份的 Agent 二进制（0755/root）和 manifest（0644/root），再启动原 DE 与 Agent 并验证健康。旧 DE 发布目录仍存在。数据库快照保留为独立恢复点；回退代码时不自动覆盖数据库，以免丢失部署后的新业务记录。

## 部署验证与剩余项

DE 新主进程 `3184701` 的工作目录已核对为上述发布目录；Agent 主进程 `3184868`。正式 `8618/api/health` 返回 200，未签名 `/api/mindos/zhijun/context` 返回 401。连续三次、间隔 15 秒的样本均为 HTTP 200，两服务 active、NRestarts=0；DE FD 为 55/56/55，connectivity DB FD 始终为 0。Agent 状态为 connected，实际 Gateway 目录配置验证通过。隔离测试服务已停止，临时合成证明私钥已删除。

[源码与逐文件回执](evidence/full-product-source-receipt.json) · [实际部署回执](evidence/deployment-receipt.json) · [硬件验证与故障记录](FULL-PRODUCT-HARDWARE-0906.md)

尚缺 `boss.nexusaos.qitus.cn` 的生产发布入口，用于部署已提交的 Admin `44a0950`（独立 `zhijun-desktop / zhijun.workspace` 登记）。入口就绪后，仍需正式账号登录、真实 SDK/P2P、各页面业务、实际麦克风及跨账号/撤销/断线验收。不能用隔离测试证明或旧只读应用替代。
