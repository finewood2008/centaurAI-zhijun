# 会话打开性能发布 0.1.32

## 计划及验收

目标：正式 macOS arm64 安装包；公司 AMD-A2A-248（192.168.0.7）的知君 Worker。

- [x] 执行正式 package:mac-arm64 流程，单元测试、UI 构建、签名、内容核验和独立启动通过；记录版本及哈希。
- [x] 只读核对盒子身份、运行源码、挂载与活动任务；保留现有配置、数据、授权和 Data Engine 程序。
- [x] 备份原程序，定向更新本轮两个 Worker Python 文件；采用现有服务的受控重启，避免活跃写入被中断。
- [x] 验证运行文件哈希、服务健康和未授权请求拒绝；保留可回滚原程序。

影响：客户端优先调度会话详情、按需读取统计；Worker 详情 scope 查询。无数据库迁移，无业务数据清理，不更新 Admin/Remote Agent/Data Engine。

依赖：Node 22、本机签名配置、现有公司盒 SSH 与服务管理能力。主代理负责构建部署；并行子代理只读核查和后端回归。

验收边界：安装包验证及服务健康不等于真实网络端到端性能验收；不重发用户聊天，不调用真实模型。若需要重启承载 Worker 的容器，须保留镜像、配置和挂载。

## 结果

2026-09-16 12:09:07（北京时间）完成盒端部署，客户端版本为 0.1.32。此次为 SSH 定向部署，不是 OTA/manager 发布。未覆盖本机已安装应用。

### 客户端

- `frontend/shell/release/Zhijun-0.1.32-mac-arm64.dmg`
  - SHA256 `272bb20e53c884721750ba14a5fb2097c607519a71ee6ccff2e1dae8a3b6619d`
- `frontend/shell/release/Zhijun-0.1.32-mac-arm64.zip`
  - SHA256 `03b9b9d71ed2bcc5bb700ec71e3d950812033937cf0a150a2bfb714039393d4a`
- 正式 production 配置，Developer ID 签名；当前发布配置 `notarize:false`，未做 Apple 公证。
- 完整正式构建链通过：shell 测试、desktop 测试、Electron E2E、类型检查/UI 构建、签名与验包、迁移目录独立启动 smoke。
- 额外比对 ASAR 内 production/runtime 共18个源码文件，逐字一致；mindos-web-dist 与本次构建目录逐文件一致。
- 后端独立回归43 passed、5 subtests passed。

附加 Windows 内容核验工具在 Mac 包上对 `@noble/hashes` 根锁版本产生误报：根开发依赖为2.4.0，生产 provisioning 依赖锁定的嵌套1.8.0被 electron-builder提升到包内根路径。只读核验83个生产依赖文件匹配1.8.0，Mac正式验包和启动通过。未为此修改或重打包；Windows工具的依赖解析改进不属于本次部署。

### 盒端

- 设备 ID `centauros-5f46a5cc86c2dbfe1de1349c`，目标 `192.168.0.7`。
- 保留同一容器 `09545225fc277716221d1b19ac8ed3ced5a715760693a61247c2d23112fdf5ee`、镜像 `sha256:d7f3d6cd506514644e9a38ff30ecad73c84133a535edffebecc35781a5cb4d29`。
- 运行源码根 `/srv/centauros-releases/company248-business-20260914-1/zhijun/backend`。
- `mindos/conversations.py` SHA256 `5fa03533502ca63f5bdcd56e827ff9327fd7828fd7cab8f9e6ae02b992794ffa`。
- `mindos/stores/conversation_store.py` SHA256 `65aa1accd1b4a737259e753b133f506c35c96e59a14b4b1f6374653ba625b454`。
- 现场源码比对确认仅本轮两个函数优化，未带入其它盒端代码差异；无schema迁移。
- 旧文件备份 `/srv/centauros-releases/company248-perf-032-backup-20260916-r2`（root 0700）；暂存脚本 `/home/user/zhijun-perf-032.KoL1BI`。
- 阻止新入口请求后检查网关和本体无 queued/running，再停止、替换、启动同一业务容器。第一次代理刚启动时立即健康检查过早，触发自动代码回滚；增加就绪等待后第二次成功。未恢复/覆盖业务数据。
- 最终 running/healthy、RestartCount=0、ReadonlyRootfs=true；8618 health=200、8620 unsigned context=401，proxy/Remote Agent 均 active。
- 最终只读聚合网关 succeeded=1869/failed=10/interrupted=5，本体 done=654/failed=34，无 queued/running；没有手工重发对话或批量重试任务。

### 使用与回滚

安装0.1.32后重新连接 AMD-A2A-248，再验证首次打开、连续切换的实际耗时。本轮未把模拟排队基准作为现场端到端结论。

回滚应先排空活动任务、停止同一业务容器，将上述root私有备份中的两个旧文件分别恢复至运行路径，再启动及验健康；继续使用当前数据。不得用旧数据覆盖升级后写入。
