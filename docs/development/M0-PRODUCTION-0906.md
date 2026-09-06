# 正式认证与 SDK 接入实施记录

日期：2026-09-06；起点 `3d9679b`，当前开发分支不变。

**当前验收边界（2026-09-06）：** 家中盒子 Agent 与 DE 已部署 D03，Agent active / Gateway connected / 授权快照新鲜，DE 保持 `MINDOS_LOCAL_WEB_DEBUG_ACCESS=0`；正式无签名及伪造桥请求均拒绝。新版桌面已真实登录并连接家中 AMD 盒；同一 SDK 会话的 context 握手、0条资料响应及刷新已通过UI链路，一次断开后资料区清空并重连通过。尚未验证非空资料、退出后重新登录的第二轮、跨账号/设备及撤销矩阵，`M0-R=false`、`realDeviceValidated=false`；空页不代表历史资料迁移。部署、版本、负向检查及回退证据统一见[盒端部署记录](BOX-DEPLOYMENT-0906.md)。

**当前服务基线：** 家中盒子保留并发新发布 `6b549ad3371b5250a4b6e6f2afd7cd06c61ef6b0`，D03 在独立分支 `dev/zhijun-business-bridge-0906-live` 的 `c16dc17be81240285820b9e86076877911d153c4` 上合流；已部署的 6 个运行文件与该交付一致。原 `ec2854e` + dirty 调研事实及其上传协议描述保留为历史，不能当成现运行基线。新发布的权限、分页、错误边界和 Pocket 改动已保留；这不表示知君领域或上传四层合同已完成集成。

## 实施计划

- [x] 核查 Admin 已有密码登录、签名、刷新、设备与连接票据合同。
- [x] 实现主进程受信配置、独立系统加密存储、Consumer 登录/签名/共享刷新/退出。
- [x] 安装固定归档 SDK，实现 main facade / ticket provider / sidecar 装配和业务桥拒绝边界。
- [x] 扩展窄 IPC 与独立桌面密码登录页面，保留模拟模式和旧 Web 行为。
- [x] 隔离验证真实 SDK 模块、合成 Consumer/密钥/竞态和 Electron 页面；审核修复。
- [x] 更新架构/接口/任务状态；交付提交后的远程状态另由Git校验。

## 范围与验收

影响 `frontend/shell/production/`、宿主/runtime/preload/安全白名单、`frontend/shared`、桌面 Vue/controller、shell 依赖锁、vendor 归档和相关文档。本阶段相邻仓库只读；后续D03已在各自隔离worktree实现，原data-engine既有未提交变更未合入。

1. 未配置时不发请求；配置只由 main 从显式文件读取，renderer 无权设置服务地址、应用身份或sidecar路径。
2. 按现有 Admin 密码登录与 ECDSA-SHA256 签名合同执行；密码只作一次登录输入，access/refresh/private key 不通过 IPC，凭据只使用独立 safeStorage 加密目录；不可用时拒绝明文降级。
3. 一份 SDK auth coordinator 服务设备/票据调用；换账号/退出/超时后旧登录和刷新不恢复身份，401只允许一次受控刷新，业务写不重放。
4. SDK包固定哈希并仅 main import；native host 验证 sidecar 哈希和固定参数，业务桥验证失败时不进入ready，不用P2P ticket替代data-engine业务票据。
5. 登录页/模拟路径、权限与错误边界、Web/Desktop构建、隔离测试通过；真实网络/账号/设备结果单独记录，不以合成fixture代替。

## 外部输入

后续已根据用户要求自动核定 Consumer 和目标应用参数：`mindos-person-data-pc` / `person-data.read` / `remote.p2p`，知君使用自身 clientId、密钥和存储。`applicationId` 是盒端目标应用，不要求因知君 UI 品牌新建一项登记。账号密码在应用内输入；D03可信业务桥已编码且部署家中盒子，真实SDK context及空资料页已通过UI链路，完整M0-R仍待落实。最新证据见[自动配置与真机验收](REAL-ACCEPTANCE-0906.md)。

本记录首阶段的部分并行研究未返回，由主代理接续；后续D03的OS和服务端实现已返回并经过主代理交叉检查。

## 已实现的正式接口

| 层 | 实际文件 | 已实现行为 |
| --- | --- | --- |
| 受信配置 | [config.cjs](../../frontend/shell/production/config.cjs) | 仅 main 读取显式文件；HTTPS、字段白名单、16 KiB、文件权限及拒绝符号链接；无配置保持 unconfigured |
| 系统存储 | [credential-store.cjs](../../frontend/shell/production/credential-store.cjs) | P-256 客户端身份、ECDSA-SHA256 DER 签名；safeStorage 加密、0600原子写入、按 Consumer 基址和登录账号分别保存客户端身份；不降级 basic_text |
| 账号请求 | [consumer-client.cjs](../../frontend/shell/production/consumer-client.cjs) | 密码登录、已授权设备、签名连接票据、共享刷新及退出；HTTPS请求10秒超时、禁止重定向、流式读取256 KiB上限 |
| SDK 装配 | [sdk-runtime.cjs](../../frontend/shell/production/sdk-runtime.cjs) | 实际SDK动态import；二进制哈希和权限校验、固定gateway/ICE参数、Direct-only、私有管道与有界关闭 |
| 业务桥 | [business-bridge.cjs](../../frontend/shell/production/business-bridge.cjs) | 同一SDK context握手、8KiB/精确字段/四项主体校验，只允许受控资料GET；不持有业务token |
| 应用适配 | [adapter.cjs](../../frontend/shell/production/adapter.cjs) | 身份绑定、业务桥端口、请求字段转换、退出/迟到授权资源回收；主程序没有注入fake bridge |
| 桌面 UI/IPC | [共享类型](../../frontend/shared/desktop-contract.ts)、[DesktopApp](../../frontend/mindos-web/src/desktop/DesktopApp.vue) | 新增 signInWithPassword(context,{phone,password})；密码提交时清空输入框，不存入controller状态、浏览器存储或公开快照 |

Admin 源码基线 `8ff6e888fb17ce268527755d6795c9d68b5b5305`：

- [控制器](../../../nexusaos-admin/admin-backend/module_nexus/controller/nexus_consumer_auth_controller.py)：`POST /app-api/auth/password/login`、`POST /app-api/auth/refresh`、`POST /app-api/auth/logout`、`GET /app-api/devices`、`POST /app-api/devices/{device_id}/connectivity/sessions`。
- [输入输出模型](../../../nexusaos-admin/admin-backend/module_nexus/entity/vo/nexus_consumer_auth_vo.py)：登录需手机号、8–72 UTF-8字节密码及宿主生成的clientId/P-256公钥；不沿用旧占位Consumer路径或RSA签名。
- [签名验签](../../../nexusaos-admin/admin-backend/module_nexus/service/consumer_request_signature_service.py)：`NEXUSAOS-CONSUMER-V1`、account/client、method、应用路由、时间戳、nonce、body SHA-256；签名不含反向代理的 `/prod-api` 前缀。

所有账号受保护请求共用实际SDK `createElectronConsumerAuth`。只在明确401时共享刷新并重试一次；403不猜测为撤销，不自动重试超时/丢包，更不重放业务写入。登录提交凭据前检查client epoch和runtime代次，超时或退出后的迟到结果不会形成隐藏登录。刷新凭据拒绝后清空公开主体，允许重新登录。

显式退出首先清理本地令牌，再尝试远端logout；远端失败会提示失败，不能声称服务器已撤销。关闭应用仅清理本地登录并关闭所持SDK资源，下一次打开仍需登录；同一登录账号的客户端密钥保留以复用注册身份；切换手机号使用独立clientId/P-256密钥。账号索引整体加密，不把手机号作为文件名，最多保留32个账号身份。Admin `_upsert_client` 不允许跨账号复用clientId，因此不能只按安装实例共用一把身份密钥。当前 Electron 37 使用同步 safeStorage，系统钥匙串交互可能阻塞主线程，应用计时器不能保证截断系统提示；后续已用真实macOS safeStorage验证临时合成凭据；签名安装包仍待完成。[Electron 官方说明](https://www.electronjs.org/docs/latest/api/safe-storage)

## 启动正式账号入口

安装 SDK 依赖后，可自动生成开发验收配置并启动：

```sh
rtk proxy npm --prefix frontend/shell ci
rtk proxy bash start-desktop.sh --real
```

[账号样例](../../frontend/shell/config/zhijun-product.example.json)和[目标绑定](../../frontend/shell/config/zhijun-connectivity.json)来自 data-engine 产品配置及 Admin/Agent 策略。准备脚本按固定 SHA-256 从 SDK release 复制当前平台二进制到 `data/desktop/`，生成完整绝对路径配置；不会覆盖已有不同内容。已有 `ZHIJUN_DESKTOP_CONFIG` 时 `--real` 优先验证并使用它。当前自动准备只支持 macOS/Linux，实际通过的是 macOS ARM64；Windows 权限/启动仍待验证。不要在JSON中放账号密码或token。没有配置或启动参数时仍保留原未配置状态，`--simulation` 继续显式运行合成数据。

配置正确时显示“账号服务已配置”，用户可在应用内输入已有账号密码并查询设备。`production` 表示加载了正式账号适配器，不表示部署和真机验收通过。打包后只接受 `resources/zhijun-product.json`，忽略配置环境变量；打包/签名尚未交付。

可选 `connectivity` 对象字段是 applicationId、purpose、requestedScopes、gatewayHost、iceHost、sidecarPath、sidecarSha256、profile；profile只能为 SOVEREIGN_DIRECT_ONLY，传输固定DIRECT_ONLY。这些必须对应真实注册和固定产物，不能使用猜测值。**后续已在main注入真实D03握手适配器；连接现在进入SDK及盒端context验证。家中Agent/DE已部署匹配的[签名桥v1](BUSINESS-BRIDGE-0906.md)，配置JSON本身不能伪造授权。**

## 验证及未验范围

- shell：61项（原45项加Consumer9、存储/配置3、SDK/生产适配4）。实际SDK模块与合成Consumer响应、临时加密存储替身及私有管道进程一起运行，未连接真实网络。
- 前端：原37个文件级项目和11个desktop controller用例，新增密码不进入状态及退出抢占。
- 真实Electron：4项，新增配置后的密码页、UTF-8超预算拒绝及输入清空；该拒绝发生在系统存储/网络之前。原模拟资料全流程保持通过。
- Web、Desktop分别构建并执行模块边界检查，文档类型严格检查、启动检查和差异格式检查。

上述测试分别位于 [shell/tests](../../frontend/shell/tests)、[desktop-ui.test.mjs](../../frontend/mindos-web/tests/desktop-ui.test.mjs)。命令沿用 M0 实施记录的 shell test、前端 tests/*.test.mjs、build/build:desktop 和 test:e2e。没有把测试替身的AES加密当成真实系统钥匙串已验收，没有把合成管道进程当成生产sidecar或盒子连通。

新增测试覆盖并修复了以下问题：已失效Consumer身份仍停留在设备页、主进程超时后迟到登录保存令牌、签名等待期间旧代次继续发请求、退出时业务桥授权迟到遗漏回收，以及Admin拒绝跨账号复用同一clientId的兼容问题。共享刷新、错误主体、字段泄漏和退出失败均有拒绝测试。

## 当前完成状态与下一步

DESK-04/05/06已有正式客户端实现，真实登录及两台授权设备列表已验证，macOS安全存储已测；签名发布仍待验；DESK-07/08 已有真实SDK装配与关闭边界，D03后续已部署家中盒子，真实空资料页及一次断开重连已验；非空资料及完整矩阵仍使M0-R未完成。BASE-02 的配置文件和现有Admin合同已经落实到代码，正式应用注册并未由本次代码创建。M1领域迁移、流式聊天、上传和签名发布没有因此完成。

后续已核定并自动填写目标参数；macOS safeStorage 及当前平台原生 sidecar 已实测，Consumer/Gateway 在线且未认证设备查询正确拒绝。真实账号登录及授权设备列表已通过，Agent→data-engine可信业务桥已编码；家中盒端部署已完成；真实空页及一次断开重连已验；仍需非空资料及跨主体验收、历史资料归属，以及可重建签名产物。现有Admin P2P票据不能直接交给data-engine session exchange，不能借用其他应用登录态或开启local-debug补过验证。具体结果和未完成项以[真机验收记录](REAL-ACCEPTANCE-0906.md)为准。

本记录首阶段本地结果：shell 61项、前端48项、真实Electron 4项全部通过；Web/Desktop构建、严格类型与启动边界通过。289处本地文档链接与4段Mermaid渲染通过，目标图源码未变。已检查真实Electron密码页布局；截图仅保留临时检查目录，不提交用户/运行数据。

D03后续增量：三端签名桥已实现，真实Consumer登录和两台授权设备列表已确认；宿主现为73项测试通过，另4项Electron、101项DE和Agent全套/race通过。目标SVG已按本轮架构更新。早期已生成Linux双架构部署输入；后续家中盒端部署完成，真实Direct空资料页及一次断开/重连已验，非空资料和跨主体隔离仍待验，详见[D03记录](BUSINESS-BRIDGE-0906.md)。
