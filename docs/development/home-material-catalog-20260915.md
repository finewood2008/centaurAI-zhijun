# 2026-09-15 家里盒子原材料列表兼容修复

## 范围和验收计划

用户已授权修复 `192.168.1.18` 原材料列表。目标身份
`centauros-c975febb427df29b0fe2334b`，不是公司盒子。

- [x] 核对实际运行进程、设备身份、操作清单及分页业务实现。
- [x] 无损复现：旧清单不带参数通过，带 `limit=50&offset=0` 抛
  `WORKSPACE_ARGUMENTS_INVALID`；仅在诊断进程内补齐两个参数后通过。
- [x] 备份实际清单，仅添加 `get_api_mindos_materials.query` 中的 `limit`、`offset`。
- [x] 重启加载清单的用户服务 `centaurAI-database.service`，验收新进程、健康和参数校验。
- [x] 通过用户已登录的知君客户端重新连接原盒子，确认真实会话材料列表恢复。

不更换 Data Engine/worker 代码、模型配置，不改变文件、Owner 或授权策略。
Gateway 与 Data Engine 运行在同一服务中，重启会短暂中断业务连接。
如果重启失败，恢复备份清单并重启原服务，不删除数据或数据库。

## 变更前证据

- 当前发布根：`/home/user/apps/centuarai-data-engine/releases/home-business-full-20260914-3`。
- 实际清单：该目录下 `zhijun/frontend/shared/product-operations.json`。
- 原清单 SHA256：`dcfda9ddeda0b721ba0857134d70337327b3850d7f8d23a02001d27831f48550`。
- 原服务 MainPID `501577`，监听 `127.0.0.1:8618`。
- Data Engine dispatch 和 materials 能力代码已经接受分页，无须为此替换程序。
- 新客户端增加分页参数，而盒端旧清单尚未同步。文件夹是独立操作，不受此问题影响。
- 桌面端将 Gateway HTTP 400 统一映射成“设备未能完成请求”，掩盖了具体参数不兼容原因。

本修复不把最新完整清单覆盖到盒子，避免连带切换本次未部署的模型/worker 路由。

## 执行与实际验收

- 原清单和变更记录保存在发布根的 `.catalog-pagination-backup-agj4luho/`，目录 0700。
- 新清单 SHA256：`d0de8e3022efed3daf62bb3fe139f49a7137f5b64b08d8d9d6fa9d808dea29a6`。
- JSON 语义比较确认仅上述操作增加两个 query 参数，其他操作与权限均未改变。
  文件保持 root:root、0644、普通文件。未修改原发行 manifest 来伪装哈希不变。
- 使用实际部署版本 Catalog 验证：不带分页通过；带分页通过；未知参数仍拒绝。
- 用户服务按原 systemd 配置重启，新 MainPID `976793`，active/running、NRestarts 0。
- 直连本地 `/openapi.json` 返回 200；诊断 curl 显式禁用环境代理，避免代理影响 loopback 检查。
- 重启后客户端原工作区连接失效，使用正常“重新选择盒子”流程重新连接相同家里设备，
  没有重登账号、认领、修改授权或重放写操作。
- 实际 Electron 原材料页显示“共 3 项资料 · 本页 3 项”；两个既有文件夹及三个材料均显示，
  材料状态“已完成”、知识卡片“草稿待确认”，列表原错误消失。未打开原文或修改资料。
- 本轮无需重新打包客户端。完整发行时仍需让客户端与盒端清单同步，不能只更新一端。
