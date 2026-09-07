# 连接失败、导航与启动图标修复计划

> 后续状态（2026-09-06）：Admin 已上线，Agent Owner 绑定修复已部署，真实 AMD 盒子票据、Direct、v2 context 与资料页面通过。下文“正式连接尚待完成”保留为发布前历史记录；当前结果、任务回收修复和未完成的全功能/安装包验收以[上线与合并记录](ADMIN-VERIFY-MASTER-MERGE-0906.md)为准。

用户实际反馈：登录后出现 `TRANSPORT_UNAVAILABLE`，主导航未显示；启动图标没有使用原来的半人马图片。

## 验收条件

1. 明确失败发生在账号应用授权、票据获取还是 SDK/设备连接；按证据展示准确错误，不把授权配置问题统一归为网络不可用。
2. 已登录但尚未连接、正在连接或连接失败时保留五个主导航与偏好入口；业务未就绪时只展示连接提示，不加载业务页、不访问资料、不保留上一个工作区数据。
3. 恢复既有半人马图标，覆盖开发启动与正式打包配置，并通过实际桌面外观检查。
4. 必要的单元/交互回归、构建通过；实际账号端无法完成的步骤明确记录，不把隔离模拟写成正式验收。
5. 更新相关说明，提交并推送当前开发分支。

## 文件与工作分工

- 主代理：读取当前运行证据、盒端服务健康与 Admin 应用登记合同；合并、实际桌面核验、提交推送。
- 前端代理：`DesktopApp`、`DesktopConnection`、`DesktopTopbar`、`App` / `MainLayout`、导航回归。连接可见性与业务就绪条件分离，保持 Web 入口行为。
- 连接代理：SDK / Consumer 错误传播与回归；先提出具体方案，避免输出凭据和原始错误正文。
- 图标代理：查找原半人马资源，核对 Electron Dock / 打包图标；文件归属另行明确，避免与连接实现冲突。

## 依赖与步骤

- [x] 检查当前源码和截图；确认导航受整个 App 的 ready 条件控制。
- [x] 核查盒端在线状态和当前 SDK 授权/连接错误链。
- [x] 修复准确错误分类与导航显示，恢复原半人马图标资源与启动设置。
- [x] 完成针对性回归、Web/Desktop 构建与实际登录、选盒、导航核验。
- [x] 更新结果与外部依赖。
- [x] 提交推送当前 `dev/first-integrate-check-0905` 分支。
- [x] 发布正式 Admin 修复后完成真实 SDK/P2P/工作区连接与资料页验收；其余业务矩阵另见后续记录。
- [ ] 正式安装包的独立外观、签名和公证验收；开发态 Dock 已切换到 2026-09-07 的透明圆角资产。

生产 Admin `zhijun-desktop / zhijun.workspace` 登记在上一轮尚未发布；本轮先复核事实，不假定截图必然由这一条件造成。不能使用旧只读应用或绕过授权作为修复。

## 已实施的修复

1. `Consumer → SDK ticket provider → production adapter` 不再把安全的账号错误丢弃为通用网络失败。保留原 SDK 严格票据解析；错误保存按单次请求隔离。未知账号错误不推断为应用未登记，关闭 native 失败也不覆盖原拒绝。
2. `DesktopApp` 按登录身份显示框架，按工作区就绪挂载业务页面。失败、选盒、连接、授权、断开期间均保留五个主导航及偏好；业务页卸载、旧工作区清理与主进程拒绝操作不变。
3. 原半人马源为 `frontend/mindos-web/logo.jpg`，SHA256 `9d3fa3428bec533b3656a2b9d373e039faae08a5836dcf518535fe62a775227d`。保持比例与完整画面，经 Swift/CoreGraphics 去除近白背景，置于暖白圆角底板并保留透明外缘，生成 `shell/assets/centaur.png` / `centaur.icns`。macOS ready 后设置 Dock 图标，窗口和打包配置使用同源资产；来源、渲染参数、脚本和产物哈希记录于 `centaur-source.json`。

## 验证结果（2026-09-06）

| 验证 | 结果与范围 |
| --- | --- |
| Shell `npm test` | 126/126；含真实已安装 SDK 的授权错误传播、并发隔离、关闭失败、票据校验、native 分类和敏感字段拒绝 |
| 前端测试 | 63/63；TypeScript 检查、Web 与 Desktop 构建通过 |
| 浏览器导航回归 | `node tests/product-navigation.e2e.mjs` 通过；连接各阶段保留导航，未就绪零业务请求，重连、断开和退出正确 |
| 隔离 Electron 导航 | shell 下 `node --test tests/product-navigation.e2e.cjs` 通过；实际 main/preload/自定义协议与构建，合成已登录失败状态首次加载和 Reload 均有导航，业务调用 0、main 订阅 1；未见页面异常、console error、短暂 Toast 或外部请求 |
| 实际启动 | `bash start-desktop.sh --real` 重启后正常显示登录页；无启动异常。旧会话热重载的短暂空白未在隔离 Reload 或新主进程中复现，未将其归因于某项未证实的故障 |
| 实际账号与 AMD 盒子 | 用户授权账号登录成功；设备列表显示家庭 `AMD AI盒子` 在线，选择目标 `centauros-c975febb427df29b0fe2334b` 后出现 `APPLICATION_AUTHORIZATION_DENIED` |
| 实际导航 | 真实已登录失败状态显示五个主导航和偏好；对话、资料与边界、偏好切换正常，内容区保持连接提示。实际 Cmd+R 重载后登录身份、导航与准确错误继续显示，无空白。此项不代表业务页面已通过验收 |
| 图标 | 2026-09-07 本地渲染后的 PNG 为 1024×1024 RGBA，四角透明，约 24.47% 像素全透明；ICNS 含 alpha，重复构建哈希一致。新版真实主进程正常启动并重新连接公司 AMD 盒，开发态 Dock 通过 `app.dock.setIcon` 使用该 PNG。CUA 无法截取系统 Dock，正式品牌安装包尚未构建 |

没有记录账号密码或票据；没有探测、录制麦克风。

## 正式连接尚待完成的事项（Admin 上线前历史）

这次真实界面错误来源已明确：票据申请 POST 返回 HTTP 200、业务码 601 和固定消息“Connectivity应用或权限未获准”。拒绝发生在 Admin 应用策略检查，不能据此进一步区分运行旧版登记表与现场策略不一致；也不能把它作为家庭盒子 P2P 不通的证据。盒端 DE health 200、Agent active/connected、服务无重启，均只证明盒端运行状态。

已准备且推送的 Admin 分支为 `dev/zhijun-full-product-0906`、提交 `44a0950`。变更是 `remoteops_stream_service.py` 的独立应用登记及对应测试，无数据库迁移。当前源码没有环境变量覆盖或运行时 API 修改登记表的入口，需要核对生产实际基线、合入增量并按现场方式重启后端。

匹配字段为 `applicationId=zhijun-desktop`、`clientPlatform=electron`、`requestedScopes=["remote.p2p"]`、`purpose=zhijun.workspace`、`profile=SOVEREIGN_DIRECT_ONLY`、`dataPathPolicy=transportPolicy=DIRECT_ONLY`、`protocolVersion=nexusaos.connectivity.v1`、`durationMinutes=30`。已核对安装 SDK 默认时长 30，Admin DTO 默认 transportPolicy 为 DIRECT_ONLY。

当前仍缺 `boss.nexusaos.qitus.cn` 的生产 Admin 发布入口（CI 或 SSH 主机与运行目录）。现有云效会话重载后要求登录，无法读取发布流水线；用户提供的 `192.168.1.18` 是家庭盒子，不能作为 Admin 发布主机。仓库 run.sh 默认 dev/9099，不能在未知生产环境直接执行默认 restart。

拿到发布入口后：核对现网版本并保留现有修改 → 合入登记增量及对应测试 → 按现网进程管理方式发布 → 在当前实际桌面重新选盒 → 验证票据、Direct、v2 context 和各业务页。正式 SDK/P2P、真实业务功能与安装包验收仍未完成。
