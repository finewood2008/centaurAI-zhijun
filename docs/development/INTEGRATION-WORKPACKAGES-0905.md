# 知君桌面集成工作包与验收计划

> 日期：2026-09-05。状态：实施规划，未实施业务集成、未签发正式应用权限、未连接真实盒子。本轮把 [集成方案](INTEGRATION-0905.md) 的 P0–P7 细化为可分工工作包；客户端接口以 [桌面接口合同](DESKTOP-CONTRACT-0905.md) 为准，部署边界以 [架构](ARCHITECTURE-0905.md) 为准。

## 1. 目标、状态与开始条件

M0 的完整交付是：**正式登录 → 列出可访问设备 → 选择已绑定盒子 → 建立受控连接 → 经正式业务鉴权分页读取资料 → 断开并清理**。演示只读资料列表；不调用建档路由、不发送消息、不上传文件、不启动本机 Python、不自动回退旧本机 HTTP 服务。健康检查或 SDK connect 成功均不能代替资料 API 验收。

推荐先用 macOS ARM64 与已有绑定设备完成这一条链路；此为实施建议。领域并入盒端 data-engine、正式 applicationId、权限、配额、平台和发布范围仍需责任方落实。文档规划不等于修改了控制面配置，也不意味着相邻仓库现有未提交文件已成为发布依赖。

决策编号复用桌面接口合同：**D01** 领域承载与数据归属；**D02** 应用身份；**D03** 业务身份桥；**D04** 长请求/上传；**D05** 交付组合。WP-00 跟踪 D02/D03/D05 的 M0 输入；WP-07 依赖 D01，WP-08/WP-09 依赖 D04，WP-10 依赖 D05。本文不另造一套决策状态。

| 项目 | 当前状态 | 后续完成证据 |
| --- | --- | --- |
| 产品代码同步 | 已同步产品源 `22dc9a3`，目标提交 `94239a1` | [上游同步及产品回归记录](UPSTREAM-SYNC-0905.md)；不代表桌面 SDK 集成通过 |
| 架构、接口与工作包 | 文档草案，供实施核对 | 本文及桌面接口合同；待合同责任方确认可部署值 |
| 桌面 host、模拟端口与隔离测试 | 尚未实施，可先开始 | WP-01、WP-04 的本地证据；必须标注使用 fake adapter |
| 正式应用身份、业务身份桥 | 尚未闭环，跨仓外部依赖 | WP-00、WP-02、WP-03 的部署配置和正负向结果 |
| SDK、sidecar、Agent、后端发布组合 | 尚未固定为可复现组合 | 提交/归档哈希、合同版本、实际二进制来源及兼容报告 |
| M0 正式设备验收 | 未执行 | WP-05 的真机记录，不以既有产品测试数量替代 |
| 领域、聊天、上传、安装包 | 尚未实施 | WP-06–WP-10 各自验收，按下述依赖合流 |

### 分工原则

角色表示责任边界，不指定人员：桌面宿主负责 Electron main/preload；前端负责 Vue transport/UI；控制面负责应用注册、Consumer 身份与授权；连接平台负责 SDK/Core/Agent；后端负责 data-engine 身份 gate、资料及知君领域；发布与验证负责跨仓版本和证据。集成负责人维护合同及依赖、审查交叉改动并最终合流。

下文标为“拟”的路径只是落点建议，不声称已存在。编码前核对各仓库工作区，把未提交代码固定到可追溯提交或归档；不要覆盖他人工作。不同人可并行写不重叠模块，同一入口、策略表或 schema 的改动由指定包独占后统一集成。

## 2. M0 工作包

### WP-00：固定合同与正式环境输入（对应 P0）

- **责任与仓库**：集成负责人协调控制面、连接平台和后端；知君、SDK、OS/Agent、data-engine；控制面服务仓库位置由维护方补充。
- **拟影响文件**：本文件、桌面接口合同；拟新增 `docs/development/integration-release-baseline.json`；OS `manifests/remote-agent-applications.yaml` 的独立知君配置；SDK 的版本/产物 manifest。真实密钥和 token 不入文档。
- **前置**：当前源码基线和只读资料范围已完成调查。正式环境配置由维护方提供，不从 demo applicationId 或 CLI 凭据推导授权。
- **交付物**：冻结 Consumer/Gateway 信任地址、独立 applicationId/clientId namespace、purpose/scopes、目标路径/headers/限额、账号与设备归属、身份桥选择，以及 SDK/sidecar/Agent/data-engine 提交和实际产物哈希；记录变更负责人和兼容范围。
- **验收**：每个部署值可指向配置或维护方确认记录；sidecar manifest 的源码摘要与实际构建输入一致；不存在把旧 `dirty:true` 产物写成当前 HEAD 构建的情况；服务端新增特性不得仅用工作区文件名作为版本依据。
- **并行边界**：WP-01 和基于草案的 WP-04 模拟合同可提前做；真实 WP-02/WP-03 及 WP-05 的验收依赖正式输入。未知值保持显式未配置，禁止默认为示例身份。

### WP-01：独立桌面 host 与窄 IPC（对应 P1）

- **责任与仓库**：桌面宿主、前端；知君。
- **拟影响文件**：`frontend/shell/{package.json,main.js,preload.cjs}`，拟新增 `frontend/shell/electron/security/`、`frontend/mindos-web/src/main-desktop.ts`、`src/router/desktop.ts`；现有 Vite 配置、根 `start-desktop.sh`。
- **前置**：采用 `frontend/shell` 作为新桌面宿主，确认所用 Electron 主版本；本地开发可使用显式 fake 端口，不等待业务身份桥。
- **交付物**：独立 desktop 构建和包边界、登录/设备/连接状态页；实现桌面接口合同的 preload 入口、主 frame/sender 校验和受信资源加载；fake adapter 仅在显式测试配置可启用。
- **验收**：冷启动显示新知君 UI；进程树无 Python/旧 BackendRpc；未连接不执行 onboarding；renderer 无 Node 权限和凭据读取入口；外部页面、子 frame、任意 URL、伪造身份头的 IPC 请求被拒绝；Web 构建不包含 Electron SDK。测试路径失效时也不得回退本机后端。
- **并行边界**：宿主独占 shell、安全配置及启动器；前端独占 desktop 入口/路由。以同一版接口类型同步，避免同时修改 `api.ts`；认证实现由 WP-02 提供适配端口。

### WP-02：账号、设备与 SDK 生命周期（对应 P2 的客户端部分）

- **责任与仓库**：桌面宿主、控制面、连接平台；知君与 SDK，必要时控制面服务。
- **拟影响文件**：知君拟新增 `frontend/shell/electron/consumer/`、`connectivity/`；参考 data-engine 同名目录；SDK `packages/connectivity-electron/src/` 仅在确认 SDK 合同缺口时独立改动。
- **前置**：WP-01 提供入口；正式端口依赖 WP-00 的注册、签名、安全存储 namespace、绑定策略和固定 SDK/sidecar。模拟登录可先做，但不得读取示例 CLI 的登录态。
- **交付物**：主进程 Consumer 客户端、OS 安全凭据适配、单实例控制、共享 refresh coordinator、设备列表/选择、ticket provider、native host/session 装配及有界退出；向 renderer 只输出公开账号/设备/连接状态。
- **验收**：并发 401 只触发共享刷新；refresh 旋转和退出竞态不恢复旧登录；设备选择来自服务端可访问集合，无法借 renderer 改写 applicationId/scopes；ticket 不进入 renderer/日志；Direct 失败明确断开；连续连接/关闭两轮无残留子进程。SDK connect 成功只进入 `authorizing`，D03 身份桥通过才进入 `ready`；M0 完成还需实际资料读取。
- **并行边界**：consumer 与 connectivity 目录可独立实现，由一人维护共享会话状态机；OS 密钥存储与真实账号验收分开记录。WP-03 可同期建设服务端身份桥。

### WP-03：业务身份桥与资料最小授权（对应 P2 的可信服务端部分）

- **责任与仓库**：连接平台、控制面、后端；OS/Remote Agent、data-engine，按选定方案涉及 SDK 和控制面。
- **拟影响文件**：OS `remote-agent/internal/p2p/http_channel.go`、应用 registry/manifest；data-engine `backend/mindos/` 中 connectivity session/gate 模块及资料投影；确切控制面签发入口由 WP-00 列定。
- **前置**：WP-00 明确选择盒端可信桥或批准的专用票据交换方案，并确定主体/应用/路径授权责任；不能同时保留两条未约束的降级路线。
- **交付物**：把已验证连接映射为 data-engine 可验证会话的实现和威胁边界；issuer/audience、账号/设备/应用/scope、nonce、epoch、有效期、撤销、续期规则；批准的资料 GET 路径与字段合同；同盒多账号资料归属说明。
- **验收**：关闭 local-debug 后，正确主体读 `/api/mindos/materials` 成功；错误账号、设备、应用、过期/撤销/重放凭据被拒；renderer 自定义身份头不能授权；重新登录/连接不复用旧业务 session；若票据过通道，真实票据长度也通过所有层的 header 校验。不能用 `/api/mindos/validate` 或 health 成功替代此验收。
- **并行边界**：Agent 负责可信连接和目标权限，后端负责业务会话与持久归属；两者先共享合同测试向量，再联调。M0 不开放 folders 写，响应也不带未经隔离的共享目录元数据和计数。

### WP-04：只读资料适配与状态隔离（对应 P3 的 M0 子集）

- **责任与仓库**：前端、桌面宿主、后端；知君，必要的 DTO 修正落 data-engine。
- **拟影响文件**：拟新增 `src/services/transports/`，资料页与 desktop 路由；shell `connectivity/` 内请求/响应策略与调度；现有 `api.ts` 只接入 M0 资料路径，不一次改完全域服务。
- **前置**：WP-01 的窄 IPC；先按桌面接口合同的模拟响应开发，真实联调依赖 WP-02/WP-03。分页字段、条目上限和实际响应预算经 WP-00 固定。
- **交付物**：显式 Web/Desktop transport 选择；renderer 仅调用窄操作 `materials.list`；投影仅含 `materialId/fileName/fileType/status/createdAt` 和分页元数据；统一错误映射；账号/设备/session generation 校验、列表分页和去重调度；断开/切换后清理旧数据、订阅及对象 URL。
- **验收**：空列表、连续分页、含 `queued` 的列表与筛选、非法参数、未授权和超限响应均有确定结果；剔除顶层 `folders`、条目 `folder/folderId` 及共享目录计数；旧设备迟到回包无法写入新状态；取消/代次失效的Promise仅结算一次，真实在途额度不提前释放；主进程拒绝任意路径、超限 body 和伪造 header；刷新只允许明确的只读操作；网络错误不触发本机 HTTP 回退。模拟与真实响应都通过同一投影校验器。桌面接口合同中的 M0 调度数值作为应用策略验证，不误写为 SDK 的固有限额。
- **并行边界**：前端实现列表/状态、宿主实现策略/IPC；源服务响应修正由后端负责。聊天及事项入口保持未开放，WP-06 再处理全域草稿与缓存，不能把 M0 页面隔离宣称为全应用隔离。

### WP-05：M0 真机合流与证据（对应 P2/P3 的完整里程碑）

- **责任与仓库**：集成负责人、发布与验证；上述参与仓库。
- **拟影响文件**：拟新增知君 `docs/development/M0-ACCEPTANCE-0905.md`；已实现测试脚本和脱敏证据目录规范；不要提交凭据、设备业务正文或用户运行数据。
- **前置**：WP-00–WP-04 完成；合法测试账号/已绑定盒子、可部署身份桥、固定交付物可用。缺任一条件则记录外部依赖，状态为“本地合同通过，真机待验”。
- **交付物**：记录操作系统/架构、全部组件版本/哈希、正式应用配置版本、场景、期望/实际、脱敏关联 ID 和失败定位；同一安装环境完成登录→选盒→资料分页→断开两轮。
- **验收**：包括失效登录、拒绝无权设备、连接失败、切设备迟到响应、断网/重连、会话过期；local-debug 关闭且无本机后端；至少两设备及同盒不同账号按选定归属合同做负向验证；若环境无法提供，明确列为未验，不给 M0 完成结论。退出无残留连接/sidecar，renderer 与日志无凭据。
- **并行边界**：真机用例可按独立账号/设备并行执行；共享盒子的迁移、权限变更和故障注入串行，由验证负责人协调。只读 M0 不要求领域迁移或上传先完成。

## 3. M0 后续工作包

| ID / 对应阶段 | 责任、仓库与拟文件 | 前置与可并行边界 | 交付物和可验证验收 |
| --- | --- | --- | --- |
| **WP-06 全域 transport 与草稿隔离 / P3** | 前端、宿主；知君 `api.ts`、`sse.ts`、`taskRouting.ts`、`chatStream.ts`、`matters.ts`、Composer、章程编辑器、MatterWorkspace | M0 状态机与错误合同稳定；可先独立做缓存归属；启用聊天待 WP-08 | 三个网络入口走统一端口；草稿/失败恢复/operationKeys 按账号、客户端、设备、对象归属；旧无归属 sessionStorage 不自动迁入。保留同主体重连草稿；换账号/设备、未保存正文和迟到请求均验证；用户导出文件另列保留边界。 |
| **WP-07 盒端领域与数据合同 / P4** | 后端、连接平台；知君 `backend/mindos/{zhijun,stores}` 及 routes/QA/uploads 依赖；data-engine 领域适配、schema/worker；Agent 逐条策略 | 领域承载、Claim 唯一事实源、owner/device 决策先定；可与 WP-08 并行；共享表与保护扩展点由一包独占 | 依赖/表/事务清单，隔离副本升级及代码+数据库配对恢复，唯一 worker；QA 排除受保护附件和衍生卡，版本摄取先继承保护不继承 grant。事项/成果定义稳定原请求幂等回放、分页/正文预算、保留/清除及历史处置；global-only 能力隐藏或按设备实现；没有消息通道时只验独立 API，不宣称完整建档/判断闭环。 |
| **WP-08 聊天传输与恢复 / P5** | 连接平台、后端、前端；SDK contracts/process、OS Core/Agent，知君 `sse.ts`、`chatStream.ts`、ConversationPage；任务方案另涉及盒端 task/cursor | 先选原生流扩展或持久任务协议；协议研发可与 WP-07 并行，领域端到端依赖 WP-06/WP-07 | 版本化帧/错误/终止/取消/背压合同及有界预算；复用底层已有 chunk 能力时仍需处理 Core 缓冲。验证首帧、UTF-8 跨块、顺序、超60秒、断线、并发、取消与最终落库；切页继续与显式停止分开；指定409有限重预览保留 requestId，开始后不重放消息。完成后才验建档→对话→判断→回访。 |
| **WP-09 上传、版本与媒体 / P6** | 后端、宿主、前端、连接平台；data-engine uploads/pocket uploads，知君 upload/媒体 transport 和 chat imports；SDK/Agent 路径与 headers 策略 | 统一一套已固定上传协议及会话额度；WP-07 隐私保护先通过；附件自动回复另依赖 WP-08 | 方法/分片编号/幂等 header/完成/取消一致；验证 >2MiB 文件及超过单会话预算的同主体续传、hash/长度错误、跨设备 uploadId、完成/取消竞态；版本不丢 materialId/version 关联。受控媒体响应大小/类型/对象 URL 回收，不把盒内 previewUrl 直接交浏览器抓取。 |
| **WP-10 安装包、升级与可选配网 / P7** | 发布与验证、宿主、连接平台；知君 shell 打包配置，SDK release manifests，OS sidecar；BLE 独立适配目录 | 打包骨架可随 WP-01 做；正式分发依赖固定功能范围及上述对应包。BLE 另需 Electron/固件/安全配网合同 | ASAR 外目标架构 sidecar，签名前后哈希与安装包签名，资源加载、凭据存储、升级/回退、退出回收；包内无 Python/模型/用户库/密钥。各 OS/CPU 单独出结果，不用交叉编译替代运行验收；BLE 需 AEAD 与真机配对另验，不能使已绑定盒子 M0 依赖未完成配网。 |

依赖关系：`WP-01 → WP-02/WP-04` 可从本地接口开始；正式链路需要 `WP-00 → WP-02/WP-03 → WP-04 → WP-05`。后续 `WP-06 + WP-07 + WP-08` 合流才具备完整聊天领域验收；WP-09 复用其保护/状态能力，WP-10 按实际发布范围验收。外部授权未就绪时仍能交付本地 host、策略、状态机与模拟合同，但不能跳过真实身份桥验收。

## 4. 验证分层、命令与证据要求

| 层级 | 可证明什么 | 不可据此宣称什么 |
| --- | --- | --- |
| L0 文档/合同检查 | 版本、路径、类型、字段及依赖一致 | 运行实现已存在、真实权限已配置 |
| L1 纯单元与 fake adapter | 策略拒绝、DTO 投影、状态转换、缓存归属、失效回包处理 | OS 凭据可用、网络连通或业务身份 gate 通过 |
| L2 隔离组件/协议 | 本地 IPC/SDK 边界、临时数据上的路由与 worker、模拟时序 | 正式 Consumer、目标 Agent/盒端和部署限额已兼容 |
| L3 正式环境与盒子 | 固定组合下的登录、P2P、业务鉴权、归属与断开 | 其他 OS/CPU、未测权限或全部功能兼容 |
| L4 安装包与升级 | 指定签名产物的运行、升级/回退及资源/进程边界 | 尚未测试的平台或 BLE 固件通过 |

以下命令已存在，供实施时选用；**本轮编写工作包没有执行它们，既有结果仍以同步记录为准**。它们不包含尚未实现的 M0 专用测试。

在知君 `frontend/mindos-web` 执行现有前端回归；Node 需支持当前测试使用的类型剥离参数：

```sh
rtk proxy npm run typecheck
rtk proxy npm run build
rtk proxy node --experimental-strip-types --test tests/*.test.mjs
```

在知君仓库根执行现有构建边界检查：

```sh
rtk proxy bash scripts/check-web-no-electron.sh
rtk proxy bash -n start-desktop.sh
```

后端使用 [本机运行文档的隔离包装](local-runtime.md) 清除独立 metadata/gbrain/MCP 路径覆盖，并将 secret store、数据根指向临时目录。包装内 runner 的已存在入口是 `backend/.venv/bin/python scripts/run_tests.py --isolated-modules <实际测试路径> -- -q -p no:cacheprovider`；`<实际测试路径>` 是需替换的 selector，不是可直接执行的字符串。selector 相对 `backend/`，例如 `tests/test_matters.py`，不能再加 `backend/` 前缀。按改动选用同目录的 `test_matters_independent.py`、`test_chat_imports.py`、`test_routing_source_snapshots.py`、`test_device_scope_isolation.py` 等实际文件；现有用例不能代替新归属/协议合同用例。不得用默认开发 supervisor 在用户数据库上做集成验证。

SDK 仓库已有 `rtk proxy npm test`（包含 build 和本地协议验证）、`rtk proxy npm run verify:packages`、`rtk proxy npm run verify:electron:artifacts`。`verify:electron:real` 虽有现有脚本入口，需先阅读其配置和目标能力，不能把示例身份测试当知君 M0。实施中修改 SDK 后，由该仓负责人在固定环境执行并记录；本文没有声称新流式协议已被现有脚本覆盖。

每个编码工作包应随实现新增自己的单元/组件/负向测试，并在 PR 中填写**实际存在的测试文件、执行命令、输出摘要和未通过项**。现在不提供虚构的 `test:m0`、`test:desktop` 或尚不存在的流/上传测试命令。新增测试只写临时状态；真实环境测试记录配置版本与关联 ID，避免保存票据、签名密钥和业务正文。

## 5. 完成定义与合同变更

一个工作包只有在代码/配置交付、对应层级验证、已知限制和跨仓版本均可核对后才能标为完成；文档写完只关闭规划任务。集成负责人维护状态矩阵：未开始、实施中、本地通过待外部验收、已验收。阻塞项写清缺少哪个输入/责任方及受影响的验收，不把“等待授权”扩成所有本地工作不可进行。

接口、身份、限额、schema 或部署方案变化时，同时更新桌面接口合同、[集成方案](INTEGRATION-0905.md)、[架构](ARCHITECTURE-0905.md) 及当前工作包的依赖/用例；变更图中责任边界时重新导出 SVG。源版本变化另写增量审核，不覆盖既有测试历史。跨仓提交、服务部署及安装包发布分别记录，推送文档不等于部署完成。
