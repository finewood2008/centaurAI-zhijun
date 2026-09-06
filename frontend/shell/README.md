# 知君独立桌面宿主

当前已实现 M0-L：独立 Vue 页面、安全 IPC、连接状态机、资料分页/筛选、取消与退出。页面由 `zhijun://desktop/desktop.html` 加载，不启动 Python，也不依赖 PC 本机 8618 服务。正式 Consumer 密码登录/签名/共享刷新、SDK 主进程装配及 D03 业务桥客户端已实现；Agent 签发和 data-engine 验签代码已推送各自的 `dev/zhijun-business-bridge-0906` 分支，尚未部署到真实盒子。默认未配置时拒绝连接。

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

2026-09-06 本地宿主 73 项、OS 全套/race/Linux ARM64 编译及 data-engine 101 项通过；真实 Consumer 登录和两台在线授权设备列表已由应用 UI 核验。记录时现有窗口仍运行旧 main，SSH `user@192.168.1.18` 认证被拒；新版桥未部署，真实 Direct 资料读取与跨主体矩阵未执行，M0-R 未完成。更新 main 需要重新启动宿主，页面重载不能替换已有主进程；重启本身也不代表盒端已升级。后续证据见[真机验收记录](../../docs/development/REAL-ACCEPTANCE-0906.md)。

盒端部署输入由 [prepare-bridge-release.cjs](scripts/prepare-bridge-release.cjs) 生成；当前已有 Linux AMD64/ARM64 本地产物及 12 项哈希清单，生成输入不执行部署，也不记录真机验收通过。
