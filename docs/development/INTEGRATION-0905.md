# 知君 Electron SDK 与 data-engine 集成方案

> 状态：M0-L及正式Consumer/SDK客户端装配已实现；盒内签名桥已编码，盒端部署与真机资料集成未完成，见[D03合同](BUSINESS-BRIDGE-0906.md)。更新日期：2026-09-06。分支：`dev/first-integrate-check-0905`。架构见 [技术架构](ARCHITECTURE-0905.md)，本轮实现与验证见 [M0 实施记录](M0-IMPLEMENTATION-0906.md)，原调研审核见 [审核记录](REVIEW-0905.md)，产品源 `22dc9a3` 的同步事实见 [上游同步记录](UPSTREAM-SYNC-0905.md)。

实施规格入口：[桌面接口合同与 M0](DESKTOP-CONTRACT-0905.md)、[盒端领域迁移](DOMAIN-INTEGRATION-0905.md)、[工作包与交付依赖](INTEGRATION-WORKPACKAGES-0905.md)。本文维护源码事实与总体方案，配套规格维护完整目标接口和验收，已实现部分以 M0 实施记录及当前源码为准；正式应用/跨仓合同未冻结的字段按 D01–D05 跟踪。

实施任务已进一步拆分为 [详细开发任务清单](DEVELOPMENT-TASKS-0905.md)，可按任务 ID 分配、提交和验收；估算为人日范围，不代表已承诺排期。

## 调研计划与交付标准

- [x] 切换分支，确认三个仓库的源码与工作区状态。
- [x] 梳理知君入口、页面、网络层、领域后端与数据归属。
- [x] 核实 Electron SDK 交付物、身份、连接、请求及流式能力。
- [x] 核实 data-engine API、鉴权、远程白名单及与知君的差异。
- [x] 形成当前/目标架构图、连接时序、接口矩阵及分阶段集成方案。
- [x] 复核所有关键结论的源码依据、文档链接及待确认项。

初次调研只修改文档，随后已同步上游业务代码。2026-09-06 按开发任务落实独立桌面 M0-L：main/preload/runtime、独立 Vue 入口、资料策略、显式模拟适配器与测试；后续已在OS/data-engine隔离worktree实现D03签名桥，原工作区不改；真实盒端部署仍待完成。验收区分本地模拟、真实连接和领域迁移；未接入正式身份时保持能力关闭，不将模拟通过计为盒端业务完成。

验证记录分开维护：上游同步的后端、前端及事项/成果回归见同步记录；本轮新增宿主、资料控制器、策略与 Electron E2E 见 M0 实施记录。不沿用此前文档链接或产品测试数量作为本轮桌面验证结果；本轮已明确锁定宿主 Electron 依赖。

## 调研基线

| 仓库 | 分支 / HEAD | 本次观察范围 |
| --- | --- | --- |
| 知君 | `dev/first-integrate-check-0905`；开发起点 `ee8cd96`，产品同步提交 `94239a1` / 产品源 `22dc9a3` | 上游 2 个提交、76 个文件增量及文稿保护修复已保留；本轮在详细任务基线上实现 M0-L，当前提交以 Git 为准 |
| Connectivity SDK | `dev/integrate-sdk-20260902` / `819831c` | 二次审核时工作区干净；Electron 1.2.0、auth、配网包与 release 已进入当前提交 |
| data-engine | `dev/integrate-sdk-20260902` / `ec2854e` | **HEAD + 未提交修改**；上传、模型运行时等文件有改动，`pocket_uploads.py` 未跟踪 |
| OS / Remote Agent | `dev/integrate-sdk-20260902` / `9f7354e` | 初次调研只读；后续从此基线创建独立D03实现分支 |

首次调研时 SDK / OS 分别为 `f1dcb87` / `a13d7e5` 加未提交修改，二次审核时已更新为上表提交。SDK 本地 `release/electron-sdk-1.2.0/` 的 5 个 tgz 曾核对 package.json、exports 对应的 dist、README/examples 共 36 个文件；该检查不等于从源码重建或 npm 已发布。

尤其需要区分仓库状态与二进制来源：现有 sidecar manifest 仍记录构建于 `2026-09-05T01:07:11.563Z`，源提交 `a13d7e5`、`dirty:true`，输入摘要 `97e3d55adf5de789d0111c79d70fe4c7188eef9f94251ef27f2af9c78daf731b`。不能因 SDK / OS 工作区变干净，就将这些二进制认定为当前 OS HEAD 的可重建产物。实施前须重新构建或逐项核对 source-inputs，固定 SDK、sidecar、Agent、服务端的提交/归档哈希与合同版本。[S10]

2026-09-06 D03实施前曾只读核验相邻三个仓库：当时以上SDK/OS/data-engine HEAD、身份桥缺口和上传协议漂移均未变化。后续D03实现与当前交付组合以[签名桥记录](BUSINESS-BRIDGE-0906.md)和[版本基线](integration-release-baseline.json)为准。六平台 sidecar 的二进制与压缩包共 12 项哈希均匹配 manifest；Electron/contracts tgz 对应的 14 个 dist 文件与本地 dist 一致，最小主进程 SDK 装配片段通过内存类型检查。这些检查未运行 SDK 或连接设备，也未解决 dirty 构建来源。上游事项/成果与聊天发送恢复仍不代表 SDK 已支持这些能力。

## 1. 先决定什么，再开始集成

建议采用「知君桌面客户端 + 盒端 data-engine 与知君领域模块」方案。PC 不启动 Python、Chroma 或 Ollama；开发者可以另用 Web 本地入口。知君现有领域后端不能删除，因为当前 data-engine 没有兼容知君的对话、本体、判断、提醒或首次建档 API。它另有开关控制的 Claim/Profile 域，见第 5.2 节，不能把两套个人知识语义视为等价。[A1] [D1] [D12]

首轮可交付的是：**登录 → 列设备 → 选择已绑定盒子 → 建立 SDK 会话 → 通过正式业务鉴权读取资料列表 → 断开并清理。** 这里必须实际通过 `/api/mindos/materials`，不能只用 `/health` 成功代表业务可用。完成该链路后再接知君领域和聊天流。

该交付现命名为 M0：业务操作仅 `materials.list`，暴露字段、query、连接phase/generation、取消边界与错误体已在桌面规格中明确。现有 onboarding guard 不属于 M0；事项/聊天/上传待后续交付开放。M0 的模拟合同验收和正式盒端验收分别记录，不将前者代替后者。

其中 M0-L 已实现：Electron 37.10.3 独立宿主经 `zhijun://desktop/desktop.html` 加载独立 Vue 构建；窄 preload 调用主进程 runtime，主进程负责代次、读调度和资料投影。默认 `unconfigured` 无认证适配器，不发起真实连接；开发者显式启用 `simulation` 才使用合成账号/设备/资料，打包应用禁用模拟。该入口不启动 Python，不访问或回退到 PC `8618`。D02登录/设备列表已在真实客户端验证，D03三端代码已落地；盒端部署、D05固定签名发布组合及完整真机验收仍未完成。[A28] [A29] [A30] [A31]

资料合同差异已在 M0-L 落实：data-engine 列表/摄取服务已产生 `queued`，参考 PC 策略仍只有另外四种状态；知君 query、响应投影及页面筛选已包含 `queued`。本地模拟覆盖排队资料，真实盒端响应兼容性仍须联调验证。

| 优先级 | 已确认问题 | 对实施的影响 |
| --- | --- | --- |
| 已解决（M0-L） | 原薄壳与根桌面启动器指向不同产品入口 | 根脚本与 frontend desktop 命令已统一到 `frontend/shell`，新入口不加载旧 renderer / Python 后端 |
| P0 | data-engine 缺失知君核心领域路由 | 先定盒端模块承载与接口归属；仅替换 BASE 必然不够 |
| P0 | 盒内签名桥尚未部署到真实设备 | 已实现Agent逐请求Ed25519证明和DE验签；部署后实测，不能开启local-debug |
| P0 | 现有 folders 未按设备隔离，知君隐私保护还改动了基础 QA / 上传路径 | 首轮收窄资料返回；领域迁移需包含保护策略扩展点，不能只搬 routers |
| P0 | SDK 无逐帧流 / 单请求取消 | 知君聊天需新协议或持久任务轮询适配，不能原样复用 `streamPost` |
| P0 | sidecar 仍记录旧 dirty 源码，服务端上传协议仍未提交 | 固定源码与实际交付物的对应关系，不能只记 npm 版本 |
| P1 | 客户端分片上传与服务端新增路由不一致 | 先统一协议与白名单，再实现普通上传、附件及媒体 |
| P1 | 保留 Web 产品的 renderer 持票、直连 fetch 与 SDK 边界不同 | M0-L 独立入口已不导入这些路径；完整产品接入时仍须改造三处网络入口 |
| 运行时已升级；BLE待办 | 新 shell 已锁定 Electron 37.10.3；旧 frontend 包仍有未使用的 Electron 33 开发依赖 | 新启动链解析 shell 自身版本；未引入配网包，BLE、固件协议和目标平台仍须另验收 |
| P1 | 现有敏感草稿无账号/设备命名空间；领域隔离尚需逐表审计 | 在引入多账号/多设备前补齐，不把局部请求 gate 当成端到端隔离 |
| P1 | 存在会话总流量、次数、并发与总时限 | 分片和轮询必须纳入预算，不能仅检查单请求 2 MiB |

## 2. Electron SDK 怎么接

### 2.1 依赖与交付物

| 包 / 产物 | 当前工作区版本 | 接入建议 |
| --- | --- | --- |
| `@nexusaos/connectivity-electron` | 1.2.0 | 主进程使用 `.`, `/admin`, `/process`, `/auth`；不放进 Vue bundle |
| `@nexusaos/connectivity-contracts` | 1.0.1 | 固定与 SDK 验证过的版本，复用绑定与请求校验 |
| `@nexusaos/device-management-uni` | 1.0.1 | 可选账号/设备 API 编排，名字含 uni 但宿主适配可供 Electron 使用；不替代安全存储 |
| `@nexusaos/device-provisioning-electron` | 1.0.0 | 仅首次发现/配网需要；optional peer Electron `>=37.2.6 <38` |
| `@nexusaos/device-provisioning-uni` | 1.0.1 | 配网编排依赖；额外需要 `@noble/hashes ^1.7.1`，离线 tgz 不含所有依赖 |
| Go sidecar | 本地 `electron-sidecars-1.2.0` | 六平台产物单独交付；manifest 记录 Go 源提交 `a13d7e5` 且 dirty；不能当作生产签名版本 |

后续开发已安装固定归档 Connectivity Electron 1.2.0 和 contracts 1.0.1，采用SDK共享auth coordinator；Electron仍锁定37.10.3。详见[正式接入记录](M0-PRODUCTION-0906.md)。当前宿主使用 CommonJS，SDK 是 ESM，接入时需使用动态 `import()`，或明确将宿主迁到 ESM；不要在 CommonJS 中直接 `require()` 新 SDK。[S1] [D2]

发布安装包时 sidecar 放在 `process.resourcesPath/sidecar/`、ASAR 外，并按安装包目标映射 `x64 → amd64`、`win32 → windows`。Gateway/ICE 信任参数和二进制路径由主进程的受信配置决定。签名会改变文件哈希，应分别保存签名前与签名后的 manifest。[S2]

### 2.2 宿主仍需实现的接口

SDK 不是现成账号系统。`createElectronConsumerAuth` 负责共享刷新、一次 401 重试与退出竞态；登录 UI、Consumer 请求签名、安全密钥存储和实际网络端口仍由宿主提供。

建议按下面顺序装配，所有对象都留在 main：

1. 独立的知君桌面客户端身份和 OS 安全存储，配合单实例锁。不要直接复用示例中 CLI 的凭据 namespace。
2. 一个 `createElectronConsumerAuth(...)`，设备管理与签发 ticket 的客户端共享它。
3. Consumer client：认证、设备列表、创建连接请求，使用 `/app-api/devices` 和 `/app-api/devices/{deviceId}/connectivity/sessions`，带 Bearer 与 `NEXUSAOS-CONSUMER-V1` 签名。现已确认可作为已登记 `mindos-person-data-pc` 目标应用的 Electron 客户端；独立 clientId 不要求另建目标 applicationId，参数由主进程配置固定。
4. `createElectronAdminTicketProvider({ client, durationMinutes })`：校验返回的应用、平台、scope、purpose 与 transport policy；默认请求 30 分钟，允许 1–60，最终以服务端授权为准。
5. `createElectronProcessNativeHost({ executable, args, tickets, operationTimeoutMs })`，再用 `createElectronMainFacade(native.host)`。
6. 用固定产品策略构造 binding：`device_id / application_id / client_platform:'electron' / requested_scopes / purpose / profile / transport_policy`，建立 session。
7. 通过主进程请求策略调用 `session.request({ method, relative_path, headers, body })`；只把公开结果和非秘密连接状态交给 renderer。
8. 退出、换账号、换设备和应用关闭时关闭 session 与 native host，等待回收完成；设置有界退出超时并拒绝迟到结果。

示例里的 `application_id=centaurai-decision`、`purpose=decision.read` 不是知君配置。`remote.p2p` 连通 scope 也不是所有知君业务路径的访问许可，必须与 Admin/Agent 策略一起确认。[S3] [S4]

### 2.3 preload 与 renderer 边界

当前应用已实现 `window.zhijunDesktop`：快照/订阅、登录动作、列设备、连接/断开/退出、`materials.list` 和本地读结果取消；这是**应用自有接口**，不是 SDK 已提供这些方法。主进程支持显式模拟或配置后的正式Consumer adapter；已新增signInWithPassword、签名/刷新与SDK装配，stream与上传仍待开发；签名桥客户端已注入main，Agent/DE隔离分支等待部署。[A29] [A30]

- 验证 IPC sender、主 frame 和准确应用 URL，拒绝外部页面或子 frame。
- 保留 `contextIsolation:true`、`sandbox:true`、`nodeIntegration:false`、`webSecurity:true`，不要复制旧 `frontend/main.js` 的 `webSecurity:false`。
- renderer 不指定 host、sidecar 路径、应用 ID、任意 headers 或任意远程 API；主进程按方法/路径/字段/响应校验。
- 旧 `__MINDOS_ACCESS__.getTicket()`、renderer `sessionToken` 仅属于现有 Web/旧宿主路线，新的 desktop bundle 不依赖它。
- M0-L 不加载现有 `getOnboardingProgress()` 路由守卫；后续引入完整产品路由时必须等身份与设备连接完成再执行。
- Desktop 与 Web 已采用独立构建入口；完整产品统一 transport 仍待后续实现。SDK 不支持普通浏览器/H5，把包 import 进 Vue 不能产生浏览器直连能力。

## 3. 鉴权是当前最早需要打通的服务端断点

### 3.1 调研基线的凭据与断点

| 凭据 / 主体 | 用途 | 当前边界 |
| --- | --- | --- |
| Consumer access/refresh token 与客户端签名密钥 | 云端登录、设备管理、签发连接 | Electron main / 安全存储，renderer 不持有 |
| SDK 连接 ticket | 绑定设备、应用、平台、scope、purpose 的 P2P 会话建立 | SDK main → sidecar；不是逐请求业务 Bearer |
| `X-MindOS-Session` | data-engine 受保护业务路由的会话 | 要求先用专用 Connectivity JWT 单次交换，再传业务 session；不能直接使用登录 access token 或 SDK ticket |

以下段落描述改造前基线；新桥增量见3.2。Agent → data-engine 受保护调用链使用盒内 loopback；其[服务端 gate][D4]在非开发模式验证 `X-MindOS-Session` 并建立设备上下文，写请求另要 `X-Requested-By: centaur-vdb`。服务端另有非 loopback App Token 的有限资料只读例外，不适用于这条 Agent 链路，也不提供所需设备上下文。[交换入口][D5]是 `POST /api/mindos/connectivity/sessions/exchange`，使用 `Authorization: Bearer <专用 Connectivity JWT>`；返回 sessionToken/sessionId/deviceId/accountId/clientId/epochGeneration/expiresAt。该 JWT 经 Consumer JWKS 验签，校验 issuer/audience，并要求 `account_id / client_id / device_id / scope / nonce / epoch_generation / connect_before / exp / iat / nbf` 等字段。不是任意 Consumer access token。[D11]

guard 是按路由注册的，不能说整个 `/api/mindos/*` 前缀都统一受保护。例如 `/api/mindos/validate` 未挂上述 gate，不可用其成功证明鉴权；它与受保护的分片文件预检 `/api/mindos/uploads/validate` 也不是同一合同。[D4] [D8]

静态补查发现：

- data-engine 现有 PC `production-adapter` 只调 SDK；`trusted-request-headers.js` 只给写操作加 CSRF，未找到 MindOS session 交换。[D2] [D6]
- SDK 公共/sidecar 路径未提供 `X-MindOS-Session` 或上述 exchange 的桥接；native-host只签发连接ticket并传输相对请求，auth模块的exchange是注入的账号refresh回调。[S3] [S9]
- Remote Agent HTTP channel 构造盒内请求后只复制批准 headers、设置 User-Agent，没有注入 MindOS session；其 header 白名单也不接受 `X-MindOS-Session`。当前 PC 应用 manifest 未放行 exchange。[O1] [O2]

Agent 还将 Bearer token 长度限制为 512 bytes，若选择通过通道传专用 JWT，必须用真实签发 token 验证长度、字符与所有层 header 合同；不能只加 exchange 路由便认为握手可用。[O9]

以上是本次登录报错对应的实现缺口。3.2所述改造已完成本地闭环验证，目标盒端仍未部署与实测，不能将源码或合成测试视作真机通过。

### 3.2 已实现的签名桥与待完成验收

D03已选择**盒端逐请求Ed25519签名桥**：Agent从同一新鲜grant快照取得Owner，给仅两条GET请求签发最多5秒证明；DE校验独立公钥、主体/应用/原始请求目标、空body、时间、持久nonce与本地撤销。main经同一SDK session读取context并匹配四项主体后才进入ready；详见[版本化合同](BUSINESS-BRIDGE-0906.md)。该实现不使用P2P ticket作为专用JWT，也不生成客户端可复用的业务session token。

当前MindOS ticket/session验证忽略method/path，principal不含applicationId，只验证配置的required scope。应用和路径级授权是新身份桥必须补齐或由Agent持续保证的合同，不是当前MindOS session已经提供的能力。[D11]

桥不复用现有JWT；Agent/服务端改动范围和密钥文件合同见D03记录。renderer不能传入自称可信的身份头，也不能用开启 `MINDOS_LOCAL_WEB_DEBUG_ACCESS` 或取消loopback/session guard作为正式方案。

最小验收：同一个合法连接能读取 `/api/mindos/materials`；错误账号、设备、应用、过期或撤销身份被拒绝；两台设备不会读写彼此数据；注销/重连不会复用旧业务 session。

## 4. 业务 API 对照与集成范围

状态解释：「有」只代表源码存在对应业务族，不表示 DTO、鉴权、主进程策略和 Agent 白名单已经兼容。「缺失」指当前 data-engine 服务端未找到同族路由，不以原有文档名称推断存在。

| 知君功能 / 当前请求 | data-engine 当前状态 | 集成动作与合同重点 |
| --- | --- | --- |
| `GET/POST /api/mindos/zhijun/onboarding`，`GET .../zhijun/home`、`.../status` | 缺失 | 将首次建档、首页和状态随知君领域模块接入；不可把 `/home` 或 `/today` 简单改名当等价接口 |
| `GET/POST /api/mindos/conversations`，`GET/PATCH/DELETE /{id}` | 缺失 | 迁入会话生命周期、分页/搜索、metadataRevision、device scope |
| `POST /api/mindos/conversations/{id}/messages` | 缺失 | 保留 POST SSE、requestId、重试与中止状态；data-engine `/api/mindos/qa` 是单轮 JSON，不能替代 |
| `.../{id}/routing/**`、`.../reply-assistance`、`.../learning/**` | 缺失 | 迁入来源授权、preview revision、章程例外、学习与表达辅助；不放宽授权弹窗语义 |
| `/api/mindos/ontology/{stats,claims,inbox,entities,projection,proposals,...}` | 缺少知君兼容路由；另有可选 Claim/Profile 域 | 迁入实体/理解/证据/确认/撤回；先厘清两套 Claim 的归属，知识卡片也不等同于本体 Claim |
| 本体 `context-pack`、合并候选处理、`consolidate`、`purge` | 知君源实现本身仅支持 `global` | 正式 `device:*` 身份会被拒绝；先隐藏或实现设备范围版本，不能直接解除 guard |
| `/api/mindos/growth/{charter,decisions,reviews,today}`、会话 `decision-draft/**` / `outcome` | 缺失 | 迁入判断与复盘；保留草稿确认和乐观锁 |
| `/api/mindos/nudges/**`、`/api/mindos/memory-policy` | 缺失 | 迁入提醒、记忆模式及策略；需要补查全部读写的设备归属 |
| `.../conversations/{id}/charter/**` | 缺失 | 迁入章程草稿、工作区、发布与审核；不能只搬 growth 的表 |
| `GET/POST /api/mindos/matters`、`GET/PATCH /matters/{id}`、`GET .../history` | 缺失（本次知君新增） | 持续事项独立于 Claim；scope、requestId、expectedRevision、状态 active/paused/completed 及历史需完整适配 |
| `GET/PUT .../conversations/{id}/matter` | 缺失（本次知君新增） | 会话绑定有独立 bindingRevision，解绑不删事项，改绑后重新校验外发来源 |
| `GET/POST .../matters/{id}/artifacts`、`GET/PATCH .../artifacts/{id}`、`GET .../history` | 缺失（本次知君新增） | 完整回复由服务端读取并保存成果；编辑保留原来源链；新增 7 个GET、5个写端点的整体合同见API附录 |
| `.../conversations/{id}/imports/**`、`references`、`file-consent`、`files/{id}/preview` | 缺失 | 建立对话附件适配；保留批次、版本、对话关联及摄取前的隐私保护 |
| `GET /api/mindos/materials`、`GET .../{id}`、`GET /api/mindos/uploads/{id}` | 有 | 对齐列表分页与状态字段、UploadResult；现有 PC 策略要求 limit/offset，知君列表调用未统一携带 |
| `/api/mindos/folders` 与资料列表的 `folders` | 有，但现有目录元数据/计数未按 deviceScope 隔离 | 首轮不开放目录写；响应不得暴露未经隔离的目录，完整目录能力需先修后端归属与查询 |
| `/api/mindos/knowledge`、`/search`、`/graph`、`/related`、`/governance`、`/corrections` | 有 | 对齐 DTO、允许的 query、条目上限、写操作幂等键和服务端真实路径；逐条放行，不照搬 `/api/**` |
| `POST /api/mindos/uploads`、资料 `versions` multipart | 有普通上传与版本相关能力 | 完整编码请求体≤2 MiB才可做受限小文件方案，multipart须扣除边界和字段开销；大文件用统一分片协议，版本上传不得退化为无关联的新资料 |
| `/api/mindos/uploads/init` 与分片操作 | 工作区新增实现，但与 PC 策略不一致 | 见下节，先固定一套协议；不能自动 fallback 到未知路径 |
| `GET .../materials/{id}/file` / `parts/{id}/file`、DTO 的 previewUrl | 有相关读取能力 | 受控下载适配，不能让 renderer 请求盒子 loopback；响应上限、类型、Range 另验 |
| `/api/system/models/**`、`/api/system/mindos-pipeline/status` | 有部分同族接口，且模型文件有未提交变更 | 本机管理接口不是正式 PC 已批准能力；定义可远程操作的最小产品面，或首期隐藏该入口 |
| `/api/mindos/ontology/context-pack` 与导出开关；第三方 Agent 网关 | 知君 profile 语义缺失，data-engine 有自己的 Agent 网关 | 第三方 `agk_` 网关不是桌面账号通道；不可混用 token 或把全量本体直接送入知识网关 |

知君接口证据集中在 [api.ts][A2]、[conversations.py][A3]、[ontology.py][A4]、[chat_import_routes.py][A5]。data-engine 真实注册见 [server.py][D4] 和各路由文件；`docs/development/consumer-api-contract/openapi.json` 主要是 Consumer 控制面合同，不能拿它当所有 `/api/mindos/*` 的兼容证明。

### 4.1 已核实的上传协议漂移

| 操作 | data-engine 现有 Electron request-policy | data-engine 当前未提交服务端 |
| --- | --- | --- |
| 创建 | `POST /api/mindos/uploads/init` | 同路径，但请求体/响应体需要重新对齐 |
| 传块 | `PUT /api/mindos/uploads/{uploadId}/chunks/{chunkIndex}` | `PUT /api/mindos/uploads/{upload_id}/parts/{part_number}` |
| 完成 | `POST /api/mindos/uploads/{uploadId}/commit` | `POST /api/mindos/uploads/{upload_id}/complete` |
| 取消 | `POST /api/mindos/uploads/{uploadId}/abort` | `DELETE /api/mindos/uploads/{upload_id}` |

服务端新增 Pocket 合同用 `filename / sizeBytes / kind / metadata` 初始化，返回 `uploadId / partCount / partSizeBytes / uploadedPartNumbers`；每片 1 MiB，partNumber 从 1 开始，要求 `application/octet-stream` 和幂等处理；complete 返回 uploadId/status，查询状态再取得 material。当前文件是未提交的 `backend/mindos/pocket_uploads.py`。[D7] [D8] [D9]

版本差异也影响部署：`ec2854e` 的已提交后端没有这套 init/parts/complete 路由；不能只检出 HEAD 就按工作区协议调用。另外 PC 策略使用 `X-Idempotency-Key`，新增后端要求 `Idempotency-Key`；Agent允许两种头不代表会自动转换。`GET /uploads/{id}` 工作区还变成 material 与 upload-session 的联合响应，adapter必须按明确类型处理。

必须核对四层路径：Vue adapter → PC main policy → Agent parser 与应用 manifest → 后端。当前 Agent parser 接受这些规范化 MindOS 路径，PC manifest 的 PUT/POST uploads 前缀已覆盖 parts/complete，但没有放行 `DELETE /api/mindos/uploads/{id}`；移动端 manifest 有该项，也不授权给 PC。新增 `/uploads/validate` 也不能拿现有 `/validate` 代用。方法、query、header 均须进入合同测试。[O2] [O9]

实施前选择并冻结一个版本，把 Vue upload adapter、main request/response policy、Agent manifest/header 白名单、后端实现和合同测试一起对齐。单独把 URL 的 `chunks` 换成 `parts` 不能解决编号、body、幂等及响应字段差异。

### 4.2 DTO、错误体与幂等头

首轮资料竖切至少固定以下响应，不把 status=200 或存在同名路由作为合同验收：

| API | 当前 data-engine 公开响应重点 |
| --- | --- |
| `GET /api/mindos/materials` | `{items,total,folders}`，条目为材料公开字段；显式分页 |
| `GET /api/mindos/search` | `{query,knowledge,materials,unavailableMaterials,visualMaterials,capabilities,total,unavailableTotal}` |
| `POST /api/mindos/qa` | `{status,question,answer,citations,correctionNotices,meta}`；status 为 ANSWERED/PARTIAL_ANSWER/INSUFFICIENT_EVIDENCE |
| `POST .../materials/{id}/draft-card/confirm` | 要求 `Idempotency-Key`；核对materialId、knowledgeId及幂等结果 |

data-engine 的 `/api/mindos/*` HTTP 错误由注册的 handler 包装为 `{error:{code,message,traceId}}`，422 可另有details；部分内部 dict detail 的细分code不一定原样透出。其他API或模型运行时仍可能有不同错误格式。知君现有 `throwApiError` 处理 `detail` 和顶层 `message`，未解析该嵌套error，因此网络adapter需统一映射HTTP status、业务code、message、traceId与授权preview，不丢掉409/版本冲突等信息。[D10] [A8]

有些错误已经在服务端丢失：gate / exchange 将 `ConnectivityTicketError` 转成只含 message 的 HTTPException，统一 handler 又把 401/403 归为 `HTTP_ERROR`。客户端不能恢复不存在的细分 code。若要区分票据过期、撤销、错误设备并驱动正确重连，须同步修服务端结构化错误合同；在此之前按状态码提供受限提示，不能解析中文 message 猜测重试策略。[D4] [D5] [D10]

Consumer 控制面使用 `{code,data,...}`，外部Agent成功返回 `{traceId,data}`；这两层的解包应位于各自adapter，不能对全部API统一取`.data`。路径、body、headers、响应投影和错误体都应进入同一份合同测试。

事项/成果 DTO 另见 [当前 API 附录](zhijun-api-contract.md)。新冲突码 `WORK_REVISION_CONFLICT` 需要保留；会话 bindingRevision 与事项/成果 revision 不是同一个版本。旧 PC response policy 尚不认识这些字段，不能让成功响应在投影时变成空对象。当前列表/历史不分页、历史含完整正文，接 SDK 前需设计分页和响应预算；不能仅按一篇文稿最多 50,000 字符估算所有响应。[A20] [A21]

### 4.3 SSE 与取消的目标合同

当前限制：[SDK facade][S5]只有整包 response；[协议][S6]只有 connect/request/close，请求体 2 MiB、响应体 16 MiB、JSON line 24 MiB；默认 watchdog 为 connect 20 秒、request 70 秒、close 5 秒。任一 watchdog 超时会结束整个 sidecar，其他在途请求也会失败。增加 timeout 不能带来增量流或单请求取消。[S7]

底层不是完全没有分帧：Agent 已边读 HTTP body 边发 `http.response.start/chunk/end`；Go Core 却把 chunk 写进内存，等 end 后才向调用方返回。应复用并扩展已存在的帧协议，同时改 Core 的读取模型、sidecar JSON Lines、SDK/IPC 的事件投递与取消，不只改 Vue 或 TypeScript wrapper。[O3] [O4]

优先方案：扩展全链路 stream 操作，至少定义请求 ID、response head、按顺序 data 帧、end、业务 error、cancel 与 backpressure/队列上限，并映射为 Vue 可消费的异步流。保留原 `parseSseChunk` 解析能力，避免自行按网络包边界拆事件。协议版本协商失败时应关闭聊天入口并提示组件版本不兼容。

知君关键事件包括 `meta / provenance / token / context_phase / extraction / decision_draft / message_done / error`；具体集合及 payload 以 `turn.py` 与 ConversationPage handlers 为准。流前 HTTP 失败与流中业务 error 分开；断线后的重试使用原 requestId/已存用户消息，不默认重发并生成重复回复。现有后端 generator 关闭有持久化 aborted 的处理，远端 cancel 需要真正到达该链路；同步模型调用阻塞时的取消延迟仍须实测，不能只以客户端 Promise 结束作为成功。[A3] [A6] [A7]

生命周期须区分三种操作：当前 ConversationPage 明确规定普通切页不终止生成，让后端继续落库；新宿主应解除页面订阅、在同一身份/设备的会话管理器中管理剩余执行。用户明确点击停止才发送单请求 cancel。换账号、换设备或退出则先使旧代次失效，再取消/关闭旧任务与连接；若取消未确认，记录结果未知，回到原设备后查持久状态，不能宣称模型已停止。禁止把组件 unmount 一律等同于取消，或把全 session.close 当成正常单轮停止。[A11]

上游新增 `chatStream.ts`：只在首个已处理事件前遇到指定 409（ROUTE_CHANGED/PREVIEW_EXPIRED）时重新预览一次，保留正文、requestId 与 replyAssistance 来源；准备路由内部另有一次预览刷新上限，授权循环最多 6 轮。SOURCE_*、REPLY_*、500、断网、取消及已开始的流均不自动重放。AbortController 也提前覆盖读取 routing、preview 和授权弹窗。SDK adapter 必须维持这些层次，不能额外增加无条件写重试，或把流内 error 转成可重试 HTTP 失败。[A22] [A23]

备选方案：盒端新增「创建生成任务 → 按游标读取事件 → 取消任务」短请求协议，持久化有序事件后由 renderer 轮询。该方案可绕开当前传输流缺口，但会新增存储、清理和恢复语义；不能把它描述为 SDK 原生 SSE。产品与 SDK 负责人应在完整聊天实施前选定方案。

### 4.4 请求预算与会话寿命

下表是**当前源码/参考 PC 应用配置**，不是已经批准给知君的额度；最终合同取主进程、SDK/Core、Agent、后端各层约束的交集。SDK 交付二进制也须核对与这些源码限制一致。

| 层 | 已核实限制 | 集成影响 |
| --- | --- | --- |
| SDK/Core | 单请求 2 MiB，单响应 16 MiB；每个 Core session 最多 1,024 次请求 | 流量小的轮询也会耗尽请求次数；重试计入预算 |
| Core 默认值 | 请求总时限 60 秒；在途响应累计缓冲 32 MiB | 不能因 IPC watchdog 为 70 秒而承诺 70 秒业务时限；并行预览也可能触发缓冲上限 |
| Agent HTTP client | 总超时 60 秒 | 长聊天需全链路协调总时限、空闲时限和心跳；只调 main timeout 不够 |
| Agent 参考 PC 身份 `mindos-person-data-pc` | 8 并发、120 请求/分钟、整场会话 64 MiB 请求与响应 body 总量 | 主进程需统一排队、轮询去重/退避，给前台交互留额度 |
| Agent 移动身份 `mindos-pocket-mobile` | 300 请求/分钟、256 MiB 会话额度 | 属于另一应用，不能作为 PC 的授权依据 |

来源：[Core 限额][O5]、[默认参数][O6]、[请求计数与超时][O7]、[Agent 配额累计][O8]、[应用 manifest][O2]。例如 200 MiB 音频即使拆成 1 MiB 分片，也不能在当前 64 MiB PC 会话内上传完。需要业务批准的额度，或具备同主体重新授权后的跨会话续传合同；不得通过自动反复建连绕过配额。超限、会话过期与网络失败应分别映射，保留上传 ID、已确认分片与幂等键，仅在同一账号/设备重新授权后恢复。

实施验收需覆盖限额附近及超限、慢模型超过 60 秒、并行下载、后台轮询、会话到期和续传。连接票据期限、SDK session 寿命与 MindOS session 期限须一起编排；当前建议的票据 30 分钟不代表所有层都能无条件存活 30 分钟。

上游 AppTopbar 现有 health 探测超时 5 秒、连通时每 30 秒/断线时每 5 秒轮询。它针对本机 Web 后端，不是 SDK session 状态；桌面必须接主进程统一调度和正式应用批准的 health 路径。自动恢复仅用于显式选择的只读 reload，不可重发事项编辑、成果保存或消息发送。[A24]

## 5. 服务端整合方式

推荐把知君特有模块以清晰命名空间接入盒端 data-engine，不覆盖它的 `server.py`、uploads、model-runtime 等已有文件。先提取所需 routers、stores、worker 注册与服务适配，补齐数据 schema 升级和 scope。知君的 `chat_imports`、`routing` 等直接调用本地 ingestion/store，并非可以立即独立部署的纯 HTTP 客户端。

| 数据 / 能力 | 建议唯一事实源 | 需要的适配 |
| --- | --- | --- |
| 原材料、文件版本、知识卡片、索引和处理任务 | data-engine | 知君使用稳定 materialId/version；处理状态与字段转换集中到服务适配层 |
| 会话、消息、理解、证据、确认/撤回、判断和回访 | 盒端知君领域模块 | 保留原领域语义和关联表，不映射成普通卡片后丢失 trust/evidence |
| 模型配置、密钥及模型运行设施 | 盒端受控模型服务 | 保留知君外发预览、授权和回执，不因复用模型 client 自动扩大数据外发 |
| 账号与设备所有权 | Admin Consumer 控制面 | 不用知君本体数据作为账号认证依据 |
| PC 本地数据（目标） | 安全凭据、必要 UI 偏好与有明确保留规则的临时草稿 | 不运行第二套领域事实库；敏感草稿及缓存需按身份/设备隔离 |

如果选择「知君盒端独立进程」，需额外定义：独立监听端口与服务注册、Agent 目标映射、服务间认证、资料读取/摄取 API、跨库引用与删除语义、worker 责任、备份/迁移和版本兼容。两个服务不能共享相同 `data/` 目录并同时执行迁移或后台任务。该选项目前未实现，本方案也未替用户批准此部署决策。

真实设备数据scope由会话身份导出`device:<deviceId>`，本地debug的数据为`global`。客户端不能提交任意deviceScope选择数据；已有本地知君数据是否迁到某个真实账号/盒子，需要单独的导入映射、归属校验和备份方案，本次接SDK不会自动完成这项迁移。

### 5.1 客户端草稿与设备切换

当前 PC 并非只存 UI 偏好：Composer 将未发送文本/表达来源写入 `sessionStorage` 的 `zhijun.reply-input.<conversationId>`，新对话统一使用 `__new_conversation__`；章程编辑器保存正文、旧来源和基线 revision，键只含 workspaceId，且兼容旧 `zhijun-charter-buffer:*`。这些内容敏感，现有键没有账号/设备维度；它们是新增多设备场景必须处理的隔离缺口，不代表当前已经发生跨用户泄露。[A9] [A10]

建议草稿键绑定账号、客户端身份、deviceId、业务对象；异步回包另绑定递增 session generation，重连不应因此丢弃同主体的合法草稿。先定义未发送内容在主动换设备时的保留/放弃交互，以及退出后的清除规则；旧无归属键不得自动带到新账号或新设备。失效处理应覆盖内存 Map、组件状态、sessionStorage、上传句柄、定时轮询与对象 URL，不只清主进程 session。验收包括 A 设备未发送新对话切到 B、章程未保存文本、切换后迟到回包和退出再登录。

本次又新增 `zhijun.reply-failed.*`、草稿 undo 信息、模块级 `replyRecoveries` / `pendingConversations` 和 MatterWorkspace 的 drafts/operationKeys；这些同样未绑定账号/设备。事项工作区还允许复制正文与下载 Markdown，因此目标“盒端为事实源”不等于 PC 不持有业务内容：用户显式导出的剪贴板/文件不由退出清缓存自动删除，应在产品数据保留说明中分开。[A9] [A25] [A26] [A27]

### 5.2 领域依赖闭包与基础服务扩展点

迁移清单必须从调用链和事务边界反查，不能按文件名批量拷贝 `zhijun/` 后便认为完成。以下是已发现的必要依赖，实施时仍需逐表/逐入口补全：

| 边界 | 源码事实 | 需要的设计与验收 |
| --- | --- | --- |
| 资料隐私与普通 QA | 知君 `qa.py` 排除受保护附件及其衍生知识卡；data-engine 对应路径未接这套保护 | 提取保护策略服务，在基础检索/QA 出口调用；受保护原件和衍生卡不能进入普通 RAG，聊天授权不自动解除该排除 |
| 新版本摄取 | 知君 `uploads.py` 在 start_ingestion 前继承保护，不继承旧版本授权；data-engine 新版本路径尚无该 hook | 上传、版本、衍生处理统一调用策略；保护先于摄取，新版本须重新授权；删除会话后保护仍存 |
| 共享事务 | ChatImportStore 使用 conversation 数据库；RoutingStore 使用 ontology 连接与锁 | 列出表、外键/逻辑引用、唯一约束与事务，保留同事务边界，不能机械拆库 |
| 事项/成果与来源链 | 四张 work_* 表复用 ontology 连接；新增 matter/artifact 来源，编辑后仍保留原消息快照和祖先来源 | 随 P4 迁入并检查 scope；dispatch 前重新验证 bindingRevision、service/purpose 与来源；暂停/换题不得静默重新纳入上下文 |
| worker 与生命周期 | 抽取/投影任务有租约恢复，附件有独立恢复；data-engine 自身要求单进程、独占数据目录 | 注册唯一 worker 负责人；迁移/停机/重启不双跑、不重复抽取、不恢复已撤销授权 |
| 重叠 Claim 域 | data-engine 的 `MINDOS_CLAIM_STORE_ENABLED` 可启用 `/profile`、问卷、`/claims`，使用 `claim_store.db` | 与知君 ontology 的 ID、状态、owner、证据语义不同；先选唯一事实源或显式关联，禁止自动双写/覆盖 |
| 投影 | 知君 `projection.py` 仅将 global 写入 `USER.md` / `ZHIJUN_PROFILE.md`，device 视图动态渲染 | 分开“设备范围 API 视图”与“旧 global 文件/MCP 画像”；远程确认不代表旧 MCP 自动获得该用户画像 |

依据：[知君 QA 保护][A12]、[版本摄取][A13]、[附件存储][A14]、[routing 事务][A15]、[任务恢复][A16]、[附件恢复][A17]、[data-engine Claim][D12]、[单进程服务生命周期][D4]、[投影实现][A18]。

P4 的前置交付应包含：完整依赖与表清单、当前/目标 schema 版本、幂等升级脚本设计、升级前备份、失败后的匹配代码/数据库恢复、旧数据导入的归属映射、worker 启停顺序。升级演练须使用隔离副本；恢复不能只回退程序而保留不兼容数据库。它们属于后续实施交付物，本轮没有迁移用户运行数据。

新增失败后台任务的 `pending` 返回 `state: paused/failed/mixed` 与 failedCount；显式恢复沿原 job ID 重新排队并重做权限检查，不直接授予权限。对原消息引用标记的清理只影响展示/提示词，授权仍按原始消息版本快照核验，不能把清理后的文本当作新的无来源内容。[A23]

事项/成果的保留需要额外合同：当前没有 DELETE 路由，旧 ontology purge 不清 work_* 表，删除原对话后仍保留成果副本及历史，失效来源只限制后续模型复用。P4 需定义独立清除、历史/幂等记录处置和用户说明。另有已审出的上游幂等边界：创建成果省略 title 时，fingerprint 使用当前事项标题及回复内容；首次成功后这些依赖变化会让相同请求重试返回 409。当前同步保留该行为并记录，SDK 重试上线前须修为稳定请求身份与可回放结果，不能靠换 requestId 重复保存。[A20] [A21]

### 5.3 设备范围能力与持久数据归属

知君当前 `_global_only` 限制 context-pack 状态、实体合并处理、立即整合、全部记忆清除；真实设备请求会失败。实体合并候选也仅 global 返回。首期能力清单必须明确隐藏哪些入口、哪些另做按 device/owner 范围的实现；不得把真实设备降级成 global 来解锁功能。[A19]

data-engine 目录也有具体缺口：`folder_nodes.scope` 指 RAW/KNOWLEDGE 分类，不是设备；表中无 device_scope。目录 API、数量统计、删除目录后清理资料归属都未按设备过滤，`GET materials` 的 `folders` 也来自共享目录。首轮资料只读闭环应投影经过核验的字段，暂不外露共享目录/计数、不开放目录写；这只是首轮范围收窄，不代表后端隔离已经修复。完整目录上线前必须补归属、查询、计数、删除与历史迁移合同，并做跨设备负向测试。[D13] [D14]

此外，`device:<deviceId>` 只表示设备隔离，不表示同盒不同账号隔离。账号变化时清运行时缓存不会迁移或隔离持久表；data-engine 可选 Claim 域却使用 ownerId + deviceScope，并要求近期认证。必须明确盒级资料是否共享、个人本体是否 owner 私有、设备解绑/转让时数据清除或继承的规则，再确定服务端主键与授权条件。测试同时覆盖不同设备、同设备不同账号、设备所有权变更，而非只换 deviceId。[D12] [D15]

## 6. 分阶段工作包、依赖与验收

下表保留总体 P0–P7 分层；具体任务编号、跨仓角色、文件归属、并行边界与完成证据见 [工作包](INTEGRATION-WORKPACKAGES-0905.md)。[早期文档样例](contracts/desktop-contract-v1.ts) 保留设计背景，当前实现类型在 [frontend/shared/desktop-contract.ts](../../frontend/shared/desktop-contract.ts)。M0-L 已落实 preload、runtime 和资料页面；SDK/正式认证客户端已实现，本地通过；业务身份桥已在独立分支编码，必须以盒端部署和真实资料读取关闭M0-R。

| 阶段 | 工作与受影响文件（拟） | 前置依赖 | 完成标准 |
| --- | --- | --- | --- |
| P0 合同冻结 | 本文；SDK/sidecar版本清单；应用注册；身份桥；接口、归属与限额矩阵 | SDK、Admin、Agent、后端负责人确认可实施版本 | 记录方法/路径/query/body/header/响应/错误/身份/限额；目录与同盒多账号边界明确；未提交特性有固定交付物 |
| P1 知君桌面骨架（M0-L已落实） | `frontend/shell/{package.json,main.js,preload.cjs,security.cjs,launch.cjs}`、`runtime/`；`src/main-desktop.ts`、`src/desktop/`、独立 Vite 配置；根 `start-desktop.sh` | 已选 shell 和 Electron 37.10.3；完整产品路由仍属后续工作 | 独立资料 UI、默认未配置及显式模拟流程已实现；无 Python / 旧 renderer / 本机 HTTP 回退，验证见 M0 实施记录 |
| P2 身份与已有资料 API 竖切 | `shell/electron/consumer/*`、`connectivity/*`、`security/*`；Agent/服务端身份桥（跨仓） | P0应用授权、凭据适配与返回字段隔离检查；P1宿主、真实测试盒子 | 登录、选盒子、正式 session 下资料分页读取、断开/重连；不暴露未隔离 folders；无本机HTTP回退 |
| P3 统一网络层与状态隔离 | `services/transports/*`、`api.ts`、`taskRouting.ts`、`chatStream.ts`、`matters.ts`、`main.ts`；请求策略/health调度/草稿 | P2证明鉴权和传输 | 三处网络入口统一；stream未就绪关闭入口；保留有限重预览语义；路由等待连接；草稿/恢复缓存/迟到结果按身份隔离 |
| P4 盒端领域基础 | 知君领域及依赖模块提取；data-engine router/worker/schema/保护策略适配；work_*表、分页/清除及Agent策略 | 领域承载、Claim事实源、owner/device合同确定；第5.2节清单完成 | schema升级/恢复、worker唯一；本体/事项/成果读写及保护测试；历史预算/幂等/清除合同通过；global-only能力明确；完整闭环待P5 |
| P5 聊天传输与领域合流 | SDK contracts/process/sidecar、Core/Agent、Vue `sse.ts`、ConversationPage；或持久任务协议 | 协议研发依赖P0冻结合同，可与P4并行；端到端验收依赖P3/P4 | 首token、顺序、取消/切页差异、断线恢复、全链路时限通过；完整建档、对话、判断与回访在真实盒端验收 |
| P6 附件与资料完整闭环 | upload adapter、媒体blob适配、chat-import路由；四层分片合同与白名单 | 上传版本与预算统一；P4保护策略先通过；附件自动回复另依赖P5 | >2MiB与超过单session预算文件、同主体续传、幂等、取消、版本引用、保护先于摄取及媒体预览通过 |
| P7 安装包与可选配网 | 宿主打包配置、sidecar manifest与签名；BLE独立模块 | 目标OS/CPU、安全凭据；BLE另需兼容固件 | 包内无后端/模型，签名校验和生命周期通过；BLE真机另外验收 |

依赖主线为 `P0 → P1/P2 → P3 → P4/P5 合流 → P6 → P7`；P0 后可启动 P4 服务端提取与 P5 协议研发，P7 打包骨架也可提前验证。阶段编号不代表研发必须串行，但完整建档/判断依赖消息通道，不能在 P5 前宣称端到端完成。实际编码按不重叠模块分工，由主负责人完成跨仓版本和集成验证。

### 6.1 测试与发布检查

| 类别 | 必须验证的场景 |
| --- | --- |
| 宿主边界 | IPC错误sender/frame、任意URL/路径穿越、伪造身份头、超限body、renderer无法读取ticket/refresh token/key |
| 登录与连接 | 并发401单次刷新、旋转token、账号退出竞态、设备撤销、应用无目标权限、Direct失败后关闭；重新连接重新绑定 |
| 业务鉴权 | `/api/mindos/materials`成功；错误绑定/过期/重放/错误设备拒绝；local-debug关闭；不能仅测health |
| 数据归属 | 资料 items、folders/计数/删除均按合同隔离；同盒多账号、换绑/转让、global-only接口；不以deviceScope推导owner隔离 |
| 请求和DTO | 与选定服务端的真响应比对；分页/状态/空值/错误体；同名路由不默认兼容；response policy不丢字段 |
| 事项与成果 | deviceScope、两类revision、重复写/丢包后重试、历史分页/超限、删除与保留、原来源失效/编辑后保护；未保存A时保存B不能丢A |
| 设备切换 | 新设备连接前旧代次失效；旧请求/流/上传回包不可写入；对象URL回收；新对话/章程草稿与旧sessionStorage键不串账号/设备 |
| 聊天 | 授权预览、首帧/中间帧/完成/错误；UTF-8跨块；预览取消/切页继续；指定409有限重预览保留requestId；来源变化/500/流开始后不重放；重启恢复、60秒以上与并发 |
| 上传与引用 | 分片大小/编号、幂等、部分续传、错误文件hash/长度、跨设备上传ID、完成/取消竞态、隐私保护与附件版本 |
| 隐私保护 | 普通QA不能使用受保护原件或衍生卡；新版本继承保护但不继承grant；删除会话、重启、任务恢复后保护仍有效 |
| 存储与worker | 隔离副本schema升级/匹配版本恢复；共享事务一致；唯一worker、租约/半完成任务恢复、重启不重复执行 |
| 限额与续期 | 2/16MiB边界、32MiB响应缓冲、1024请求、PC64MiB会话预算、并发/速率、过期重授权；限额用已批准部署值复验 |
| 打包 | 本地资源/路由可加载，Web与Desktop产物分离，sidecar架构与签名匹配，退出无残留，PC包不带Python/数据/密钥 |
| 真机 | 指定账号、应用和盒子，至少两轮连接/请求/关闭；记录客户端/SDK/Agent/后端版本及关联ID，不记录凭据正文 |

现有知君回归使用 `npm run typecheck`、`npm run build`、全部 `tests/*.test.mjs`；新后端入口为 `scripts/run_tests.py --isolated-modules <selectors> -- -q`，旧 `run_isolated_qa.py` 仍可保留独立报告。conftest 只强制数据根，测试外层还应隔离 secret store 并清除独立 metadata/gbrain/MCP 路径覆盖，详见 [本机运行](local-runtime.md)。原迁移的 32 个前端文件、150 项后端测试及原 E2E 自动确认断言失败属于历史结果；本次同步有独立的 37 个前端文件、36 个后端模块等验证记录，不能混用，也不是 SDK 真机集成通过。[迁移记录](MIGRATION.md) [本次验证](UPSTREAM-SYNC-0905.md)

上游同步执行了隔离产品回归；本轮新增宿主/资料测试、独立桌面构建和 Electron 模拟 E2E 的结果另见 [M0 实施记录](M0-IMPLEMENTATION-0906.md)。两轮均没有登录真实账号、连接盒子、发布 SDK 或部署服务。SDK 文档中的实测记录包括 Direct 超时和 TARGET_NOT_ALLOWED，跨平台构建也不等于真机连通通过。[S8]

## 7. 首期建议与待确认项

当前SDK合同强制Direct-only，不提供可任选的relay策略。推荐首期先支持当前开发机对应的 macOS ARM64与已绑定在线盒子；配网、跨平台签名和完整模型管理不与第一条资料读取链路绑定。此为减少外部依赖的实施建议，尚未成为用户确认的产品范围。

正式联调、发布组合冻结或领域迁移前需要落实下列输入（对应桌面规格 D01–D05）。M0-L 已在这些输入尚未齐备时完成本地宿主、注入式 adapter 与模拟合同实施；仍可继续隔离研发，真实连接和运行库迁移保持不放行：

1. **领域部署**：知君领域模块并入盒端 data-engine，还是盒端独立服务？推荐前者，并通过服务适配层保留边界。
2. **应用身份与归属**：M0 已核定 `mindos-person-data-pc` / `person-data.read` / `remote.p2p`、Consumer/Gateway 及独立安全存储，自动配置与本机检查见[真机记录](REAL-ACCEPTANCE-0906.md)。真实登录及授权设备列表已验；仍需部署业务桥、资料归属验收与个人Claim事实源；已有传输 scope 不等于业务权限。
3. **可复现版本**：SDK `819831c` 的 1.2.0、OS `9f7354e` 与实际 sidecar 来源如何对应；未提交服务端上传改动如何冻结和交付。
4. **聊天协议**：扩展SDK原生流（推荐保持现有体验），还是任务+游标轮询；两者都需要服务端支持。
5. **首期平台和配网**：只连已绑定盒子，还是包含BLE首次发现/配网？配网包当前要求Electron37，安全配网还依赖固件AEAD协议和宿主secureCommands实现，不能把开发明文兼容开关当作默认方案。

## 8. 可复用实现与源码证据

| 参考 | 可以复用的设计 | 必须调整的部分 |
| --- | --- | --- |
| data-engine `frontend/electron/connectivity/` | session代次、SDK动态导入、sidecar校验、请求与响应策略、公开错误 | 知君业务白名单、DTO、鉴权桥、stream、上传合同 |
| data-engine `frontend/electron/consumer/` | Consumer适配、安全凭据、桌面身份与设备选择 | 知君独立注册、namespace与产品流程；不照搬旧账号状态 |
| data-engine `services/transports/` 与独立desktop构建 | Web/Desktop显式分离、相对请求、文件资源与hash路由 | 知君的routing/SSE/附件语义，不复制原产品页面 |
| SDK `examples/electron-poc/` | 最小SDK装配、connect/request/close | mock/CLI账号依赖、示例applicationId、精确sender校验、等待退出清理 |
| SDK 桌面配网示例 | 用户手势、候选选择、GATT状态关联 | AEAD固件、owner proof、Electron版本与目标OS真机验收 |

路径以相邻工作区布局为准，`#L` 为本次调研的源码定位，不是固定远端永久链接。

[A1]: ../../frontend/shell/README.md#L1
[A28]: ../../frontend/shell/main.js
[A29]: ../../frontend/shell/preload.cjs
[A30]: ../../frontend/shell/runtime/desktop-runtime.cjs
[A31]: ../../frontend/mindos-web/vite.desktop.config.ts
[A2]: ../../frontend/mindos-web/src/services/api.ts#L1182
[A3]: ../../backend/mindos/conversations.py#L344
[A4]: ../../backend/mindos/ontology.py#L397
[A5]: ../../backend/mindos/chat_import_routes.py#L245
[A6]: ../../backend/mindos/zhijun/turn.py#L1
[A7]: ../../frontend/mindos-web/src/pages/ConversationPage.vue#L947
[A8]: ../../frontend/mindos-web/src/services/api.ts#L68
[D1]: ../../../nexusaos-data-engine/README.md#L5
[D2]: ../../../nexusaos-data-engine/frontend/electron/connectivity/sdk-sidecar-runtime.js#L54
[D4]: ../../../nexusaos-data-engine/backend/server.py
[D5]: ../../../nexusaos-data-engine/backend/mindos/connectivity_admin.py
[D6]: ../../../nexusaos-data-engine/frontend/electron/connectivity/trusted-request-headers.js#L6
[D7]: ../../../nexusaos-data-engine/frontend/electron/connectivity/request-policy.js#L55
[D8]: ../../../nexusaos-data-engine/backend/mindos/uploads.py#L137
[D9]: ../../../nexusaos-data-engine/backend/mindos/pocket_uploads.py#L29
[D10]: ../../../nexusaos-data-engine/backend/mindos/agent/router.py#L223
[S1]: ../../../nexusaos-centuarai-conn-sdks/packages/connectivity-electron/package.json#L1
[S2]: ../../../nexusaos-centuarai-conn-sdks/docs/electron-sidecar-delivery.zh-CN.md#L24
[S3]: ../../../nexusaos-centuarai-conn-sdks/packages/connectivity-electron/src/admin-ticket-provider.ts#L36
[S4]: ../../../nexusaos-centuarai-conn-sdks/examples/electron-poc/macos-consumer-client.mjs#L69
[S5]: ../../../nexusaos-centuarai-conn-sdks/packages/connectivity-electron/src/index.ts#L19
[S6]: ../../../nexusaos-centuarai-conn-sdks/packages/connectivity-electron/src/sidecar-protocol.ts#L14
[S7]: ../../../nexusaos-centuarai-conn-sdks/packages/connectivity-electron/src/process-exchange.ts#L27
[S8]: ../../../nexusaos-centuarai-conn-sdks/docs/electron-completion-status.zh-CN.md#L19
[O1]: ../../../nexusaos-centuarai-os/remote-agent/internal/p2p/http_channel.go#L435
[O2]: ../../../nexusaos-centuarai-os/manifests/remote-agent-applications.yaml#L23

[S9]: ../../../nexusaos-centuarai-conn-sdks/packages/connectivity-electron/src/native-host.ts#L61
[D11]: ../../../nexusaos-data-engine/backend/mindos/connectivity_ticket.py#L117
[S10]: ../../../nexusaos-centuarai-conn-sdks/release/electron-sidecars-1.2.0/manifest.json#L1
[O3]: ../../../nexusaos-centuarai-os/remote-agent/internal/p2p/http_channel.go#L450
[O4]: ../../../nexusaos-centuarai-os/p2p-core-go/http.go#L255
[O5]: ../../../nexusaos-centuarai-os/p2p-core-go/types.go#L26
[O6]: ../../../nexusaos-centuarai-os/p2p-core-go/client.go#L75
[O7]: ../../../nexusaos-centuarai-os/p2p-core-go/http.go#L89
[O8]: ../../../nexusaos-centuarai-os/remote-agent/internal/p2p/http_channel.go#L512
[O9]: ../../../nexusaos-centuarai-os/remote-agent/internal/p2p/http_channel.go#L637
[A9]: ../../frontend/mindos-web/src/components/conversation/Composer.vue#L36
[A10]: ../../frontend/mindos-web/src/components/conversation/CharterWorkspaceEditor.vue#L32
[A11]: ../../frontend/mindos-web/src/pages/ConversationPage.vue#L1160
[A12]: ../../backend/mindos/qa.py#L329
[A13]: ../../backend/mindos/uploads.py#L697
[A14]: ../../backend/mindos/stores/chat_import_store.py#L1
[A15]: ../../backend/mindos/stores/routing_store.py#L66
[A16]: ../../backend/mindos/zhijun/jobs.py#L396
[A17]: ../../backend/mindos/chat_imports.py#L305
[A18]: ../../backend/mindos/zhijun/projection.py#L93
[A19]: ../../backend/mindos/ontology.py#L81
[D12]: ../../../nexusaos-data-engine/backend/mindos/claims.py#L58
[D13]: ../../../nexusaos-data-engine/backend/mindos/stores/job_store.py#L55
[D14]: ../../../nexusaos-data-engine/backend/mindos/uploads.py#L438
[D15]: ../../../nexusaos-data-engine/backend/mindos/device_context.py#L130
[A20]: ../../backend/mindos/matters_routes.py#L1
[A21]: ../../backend/mindos/stores/matters_store.py#L1
[A22]: ../../frontend/mindos-web/src/services/chatStream.ts#L1
[A23]: ../../frontend/mindos-web/src/services/taskRouting.ts#L1
[A24]: ../../frontend/mindos-web/src/layouts/AppTopbar.vue#L1
[A25]: ../../frontend/mindos-web/src/composables/useReplyRecovery.ts#L1
[A26]: ../../frontend/mindos-web/src/services/matters.ts#L1
[A27]: ../../frontend/mindos-web/src/components/matters/MatterWorkspace.vue#L1

2026-09-06 正式接入增量详见[实施记录](M0-PRODUCTION-0906.md)：可配置账号登录和设备列表，SDK装配通过合成私有管道测试；后续D03代码也已完成，但盒端部署、历史资料归属和M0-R验收仍未关闭。
