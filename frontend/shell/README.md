# 知君独立桌面宿主

当前已实现 M0-L：独立 Vue 页面、安全 IPC、连接状态机、资料分页/筛选、取消与退出。页面由 `zhijun://desktop/desktop.html` 加载，不启动 Python，也不依赖 PC 本机 8618 服务。正式 Consumer 密码登录/签名/共享刷新、SDK 主进程装配及 D03 业务桥客户端已实现；Agent 签发与 data-engine 验签已部署到家中盒子；当前 DE 合并版本 `c16dc17` 已推送 `dev/zhijun-business-bridge-0906-live`，对应盒端 `6b549ad` 新发布基线。默认未配置时拒绝连接。

在仓库根安装依赖并启动：

```sh
rtk proxy npm --prefix frontend/mindos-web ci
rtk proxy npm --prefix frontend/shell ci
rtk proxy bash start-desktop.sh --simulation
```

`--simulation` 显式启用合成账号、两个盒子和47条资料，页面持续标注模拟环境。运行 `rtk proxy bash start-desktop.sh` 则进入默认未配置状态，登录返回配置未就绪。启动器每次先构建独立页面；已构建后可在本目录执行 `rtk proxy npm start` 或 `rtk proxy npm run start:simulation`。

开发宿主固定 Electron 37.10.3，已验证 macOS ARM64；尚未交付签名安装包或其他平台运行验收。打包环境禁止通过环境变量开启 simulation。旧 `ZHIJUN_BASE_URL` 不再使用，不能用它连接本机业务服务。

验证（前端依赖已安装，且先构建桌面页面）：

```sh
rtk proxy npm --prefix frontend/mindos-web run build:desktop
rtk proxy npm --prefix frontend/shell test
rtk proxy npm --prefix frontend/mindos-web run test:desktop
rtk proxy npm --prefix frontend/shell run test:e2e
```

E2E 启动真实 Electron，使用新建临时 userData 与合成数据，关闭后清理。无需后端、SDK连接、账号凭据或用户资料。`ZHIJUN_DESKTOP_USER_DATA` 是主进程测试隔离路径；日常开发默认使用独立的 `zhijun-desktop` 应用配置目录。模拟身份/资料只在内存中，renderer 使用非持久 session 分区。

实施、测试证据及正式接入待办见 [M0实施记录](../../docs/development/M0-IMPLEMENTATION-0906.md)。旧 `frontend/main.js` 与 `renderer/` 保留为历史代码，不在当前启动链。

正式账号入口：在仓库根执行 `rtk proxy bash start-desktop.sh --real` 自动准备开发配置与固定哈希 sidecar；已有自定义配置时按[正式接入记录](../../docs/development/M0-PRODUCTION-0906.md)指定 `ZHIJUN_DESKTOP_CONFIG` 绝对路径。示例文件在 `config/zhijun-product.example.json`，只含参考 Consumer 地址。配置后在应用内输入密码，应用不会自动登录；不要将密码/token 写入配置或提交 Git。

[D03 v1 业务桥](../../docs/development/BUSINESS-BRIDGE-0906.md)通过同一 SDK session 请求 `GET /api/mindos/connectivity/context`，主进程核对账号、client、设备、应用及期限后才进入 ready。每次资料 GET 的证明由 Agent 在盒内独立签发，data-engine 逐请求验签；renderer 不提交身份头，客户端不保存可复用业务 token。新 scope 按账号、设备和 ownershipEpoch 隔离，不读取或迁移旧 global 资料。

2026-09-06 已有宿主 73 项、Electron E2E 4 项及 OS 全套/race 检查通过；本次当前 DE 合并版本新增回归结果为 111 项及 6 个 subtests。盒端实际依赖下 55 项桥测试和 5 项完整应用合成检查通过；正式服务未签名/错误签名请求均拒绝。此前真实 Consumer 登录和两台在线授权设备列表已由 UI 核验，新版桌面已真实登录并通过同一 SDK 链路读取空资料页，刷新、断开后清理和一次重连通过；跨主体矩阵和完整登录退出循环仍未完成，M0-R 未完成。部署证据见[盒端部署记录](../../docs/development/BOX-DEPLOYMENT-0906.md)，逐场景状态见[真机验收记录](../../docs/development/REAL-ACCEPTANCE-0906.md)。

盒端部署输入由 [prepare-bridge-release.cjs](scripts/prepare-bridge-release.cjs) 生成；当前已有 Linux AMD64/ARM64 本地产物及 12 项哈希清单，生成输入不执行部署，也不记录真机验收通过。

当前 `main-desktop.ts` 仅挂载独立 `DesktopApp.vue`，没有加载原产品 `MainLayout/AppSidebar` 和路由。因此没有“今日来信、对话、我的本体、判断、资料与边界、偏好”导航；代码仍在，完整页面及其业务传输尚需接入。恢复这些入口必须同时处理对应 API/SSE 与主体隔离，不能直接切回旧 renderer 的本机 HTTP。
