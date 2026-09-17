# 公司 248 Worker 与 MCP 管理接口更新

## 范围与计划

用户要求先更新 `192.168.0.7`。目标为公司 AMD-A2A-248 的
`zhijun-integration-0907`，部署当前分支知君源码和匹配 product catalog。

- [x] 核对现有镜像、挂载、依赖、代码路径；生成仅源码的 197 文件候选包。
- [x] 在盒子现有镜像的无网络容器中，用临时工作区验证签名 dispatch、MCP 未开通状态和现有 CRUD。
- [x] 并行完成本地 Worker/MCP/V3 回归，审阅源码差异与依赖。
- [x] 检查活动任务；一致性备份现有代码、状态和部署配置；成对切换 Worker/catalog。
- [x] 验证运行哈希、健康、未签名拒绝和已有数据保留；用桌面账号经正式链路读取设置。

依赖：现有 SSH/Docker 管理权限，目标源代码挂载与持久卷。候选源码来自
`scripts/build-full-product-release.py` 的 `collect` 与静态依赖审计；不包含业务数据、配置或凭据。
影响目录为 `/srv/centauros-releases/company248-business-20260914-1/zhijun`。
保留当前 Data Engine 镜像、已有补丁、挂载、环境与账号/设备授权；不部署 Admin 或公网 relay，
不配置虚构 MCP resource，不自动启用外部访问或创建授权。

验收：新管理操作能够正常读取（未配置公网入口时返回 available=false），原会话/消息/本体数据保留，
服务 healthy，未认证请求仍失败。合成环境与正式读取验收分别记录，不发送真实聊天/模型请求。

回滚：保留完整旧源码和 catalog；服务切换失败时成对恢复旧代码，继续使用当前持久数据。
数据备份只作保护，不能用旧备份覆盖升级后的写入。

## 部署与验收结果

2026-09-17 14:37（Asia/Shanghai）完成验收。

- 源码基线：`ed817cbc447c6b6d2a506d5d8260fbc3455f4807`；打包路径的 tracked diff 为空。
- 源码包 SHA-256：`d3d091c09a62afde3e1b917b30da7294577e80f32494d01e63c116d732a0db58`。
- 容器运行挂载内 197 个源文件哈希逐项通过；catalog 为 191 项，包含 9 项外部 Agent 操作。
- 保留原容器 ID、镜像、Config 与 HostConfig；Data Engine store、manager、materials 三处原有代码哈希不变。
- 隔离环境使用目标现有镜像，通过签名调用、未认证拒绝、会话 CRUD 与版本冲突验证。
  并行本地回归为 167 tests、6 subtests 全部通过。
- 切换前 Gateway 与 ontology queued/running 均为零，alignment queued 为零。
- 容器 healthy，`/api/health` 返回 200；未签名 operation 请求仍返回 401。
- 对升级前两份快照逐行核对，原有 54 个会话、213 条消息、15 条本体声明，以及证据、
  工作事项、成长记录和运行配置等受保护表的原记录全部保留。
- 使用现有桌面登录经正式设备连接提交 `get_api_mindos_settings_external_agents`，
  operation 返回 200、最终 succeeded，业务响应为 200：
  `{"available":false,"enabled":false,"grants":[],"endpoint":null}`。
  原先的 `WORKSPACE_OPERATION_DENIED` 已消除。

尚未配置 MCP 公网 resource、OAuth 与 Gateway/relay 接入；本次更新不代表已开通公网 MCP。
外部 Agent 面板可正常读取未开通状态，已有数据不会因此对外开放。

## 备份与过程记录

以下路径均在盒子 release 父目录
`/srv/centauros-releases/company248-business-20260914-1` 下：

- 旧代码：`zhijun-before-mcp-v3-20260917`。
- 最终切换前私有备份：`.mcp-v3-upgrade-backup-20260917-attempt2/`，
  其中 `workspace-state-secrets-final.tar.gz` 为停服后一致性备份，另含容器配置及校验清单。
- 首次切换备份 `.mcp-v3-upgrade-backup-20260917/` 也保留。
  备份目录权限 0700、文件 0600；包含敏感状态，不导出到仓库。

切换前检查脚本曾误用 ontology_jobs.status，改为实际字段 state 后继续。
首次启动健康后，附加探针误用 `/health`，自动回滚成功；已改用实际 `/api/health`。
再次切换前，SQLite 在只读挂载中无法建立共享内存文件，备份检查因此停止，旧版重启正常。
最终采用停服后的数据库及 WAL 临时副本完成检查，避免修改源数据库；之后切换与全部验收通过。
升级后逐行核对在原容器内执行，保留 SQLite 正常并发读取语义。
