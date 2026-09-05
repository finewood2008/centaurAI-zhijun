# 桌面宿主与前端开发任务

日期：2026-09-06；文件后缀沿用 0905 集成基线。返回[开发任务总表](../DEVELOPMENT-TASKS-0905.md)。依据：[桌面接口规格](../DESKTOP-CONTRACT-0905.md)、[接口类型样例](../contracts/desktop-contract-v1.ts)、[工作包](../INTEGRATION-WORKPACKAGES-0905.md)。**以下 23 个任务全部未开始；复选框表示未来实施步骤，不代表已经交付。新增事项分页、幂等和清除页面也仅为开发方案，尚未实现或开放。**

里程碑复用总表：M0-L 为本地合同通过，M0-R 为正式只读闭环，M1 为领域与聊天。P0 为首条 M0 必需，P1 为完整业务。估算为建议开发投入，S=0.5–1、M=1–2、L=2–4 人日，包含对应局部验证和一次评审修订，不含外部注册、设备等待和未知兼容问题导致的额外返工；不能直接相加作为交付日期。

“硬依赖”指本任务整体验收前必须满足；“可先做”限定在草案或 fake adapter 上的局部工作。正式登录、连接、资料读取不能以模拟结果关闭。所有拟新增路径均为设计位置，实施时由 owner 确定真实测试入口并回填证据，不假设已有 `test:m0` 命令。

## 源码定位与分工

已核对现状：[薄壳](../../../frontend/shell/main.js)轮询本机后端；[根启动器](../../../start-desktop.sh)仍启动旧 Electron；[Web 路由](../../../frontend/mindos-web/src/router/index.ts)执行 onboarding guard；[api.ts](../../../frontend/mindos-web/src/services/api.ts)、[sse.ts](../../../frontend/mindos-web/src/services/sse.ts)、[taskRouting.ts](../../../frontend/mindos-web/src/services/taskRouting.ts)各有网络入口；[事项服务](../../../frontend/mindos-web/src/services/matters.ts)有 `pendingConversations`，回复恢复及编辑器另有内存和浏览器缓存。

宿主 owner 独占 `frontend/shell/main.js`、shell 依赖/锁文件、启动器与主进程装配；安全 owner 独占 preload/IPC；状态 owner 独占主进程状态机。各 adapter 以端口接入，由宿主 owner 合并装配修改。前端入口 owner 独占 Vite/desktop 路由；transport owner 独占 `api.ts/taskRouting.ts/sse.ts/chatStream.ts`；缓存任务分别独占指定组件。共享文件不能由两个并行任务同时编辑，应先合并前置任务再接力。

## M0：独立宿主、正式身份与只读资料

<a id="desk-01"></a>

### DESK-01：建立独立 desktop 构建和启动链

**元数据：** WP-01；M0-L；P0；桌面宿主 owner；M；未开始。

**硬依赖：** BASE-01。**可先做：** 核对现有入口与受控静态资源方案，无需等待正式账号。

**路径：** 现有 `frontend/shell/{main.js,package.json,package-lock.json}`、`start-desktop.sh`；拟新增 `frontend/shell/electron/bootstrap/`。Vite/页面入口交给 DESK-12。

- [ ] 将启动目标固定到新 shell，移除该链路对旧 `frontend/main.js`、BackendRpc 和 Python 启动的依赖。
- [ ] 实现开发与构建产物加载配置，缺少资源时显示明确错误，停止旧本机后端轮询。
- [ ] 固定依赖/Node/Electron 兼容范围，保留隔离开关与 fake adapter 显式注入位置。
- [ ] 添加启动与缺资源组件用例；记录实际新增脚本及启动方式。

**验收：** 正向冷启动加载独立入口；负向在 8618 无服务、资源缺失时不启动 Python、不请求旧后端、不回退旧 UI；默认不带 `--no-sandbox`。**交付证据：** 入口 diff、依赖锁、启动截图、脱敏进程树与故障用例结果。完整打包签名另属 WP-10。

<a id="desk-02"></a>

### DESK-02：实现窄 preload 和 IPC 安全边界

**元数据：** WP-01；M0-L；P0；安全 owner；M；未开始。

**硬依赖：** DESK-01。**可先做：** 按接口样例编写运行时校验与 fake handler。

**路径：** 拟新增 `frontend/shell/preload.cjs`、`frontend/shell/electron/security/`、`frontend/shell/electron/ipc/`；`main.js` 装配由宿主 owner 完成。

- [ ] 只公开 `zhijunDesktop` v1 方法、公开快照与本地 unsubscribe；剥离 Electron event 对象。
- [ ] 校验 sender WebContents、主 frame、精确受信应用 URL，拒绝相似域名前缀及任意导航。
- [ ] 对 context、callId、操作参数执行运行时 schema 校验；限制重复在途 callId。
- [ ] 保持 contextIsolation、sandbox、禁用 Node；禁止 renderer 注入 URL/headers/token/宿主路径。

**验收：** 正向有效调用进入对应 handler；负向伪 sender、子 frame、外部页面、任意 header/URL/未知字段均在 SDK 调用前拒绝；没有通用 IPC 或 fetch 暴露。**交付证据：** 白名单、preload 导出快照及恶意调用测试结果。

<a id="desk-03"></a>

### DESK-03：实现主进程连接状态与代次控制

**元数据：** WP-02；M0-L；P0；状态 owner；L；未开始。

**硬依赖：** DESK-02。**可先做：** 纯状态机无需真实登录/连接。

**路径：** 拟新增 `frontend/shell/electron/session/`；依据接口样例，非直接把文档 TS 当运行时实现。

- [ ] 实现全部 phase、主进程 generation 与生命周期内全局递增 sequence。
- [ ] beginSignIn/connect/disconnect/signOut 先校验旧代次，再生成新代次及内部 operation token。
- [ ] 统一公开快照、订阅、capabilities；connect 成功只能 authorizing，业务桥通过才 ready。
- [ ] 为新登录、切设备、断开、退出、会话失效实现旧调用失效通知与资源清理端口。

**验收：** 正向完整状态转换可重放；负向两次登录/连接交错、退出后旧认证或 refresh 迟到不恢复凭据/连接；renderer 无法指定更高代次，未 ready 的 materialsRead 为 false。**交付证据：** 状态转移表、虚拟时钟竞态测试及事件序列。

<a id="desk-04"></a>

### DESK-04：实现正式登录适配和系统凭据存储

**元数据：** WP-02；M0-R；P0；Consumer/安全存储 owner；L；未开始。

**硬依赖：** BASE-02、DESK-03。**可先做：** 使用注入式 fake auth/store 验证登录成功/拒绝分支。

**路径：** 拟新增 `frontend/shell/electron/consumer/auth-client.*`、`credential-store.*`；只参考旧 `frontend/consumer-client.js`、`secure-store.js`，不复用运行凭据。

- [ ] 按正式 D02 实现登录回调/用户交互；缺配置返回 CONFIGURATION_REQUIRED。
- [ ] 只在主进程存取 access/refresh 凭据，采用正式应用独立 namespace 和 OS 安全存储。
- [ ] 登录结果写入前核对 DESK-03 operation token/代次；废弃已失效回调。
- [ ] 区分取消登录、拒绝登录、存储不可用，不将原生异常或 token 发给页面。

**验收：** 正向真实账号可登录并仅显示公开身份；负向示例 applicationId、错误回调、存储失败与退出后迟到回调不能得到可用会话；renderer、日志、sessionStorage 均无凭据。**交付证据：** 脱敏注册版本、fake 测试、指定 OS 安全存储及真实登录结果。

<a id="desk-05"></a>

### DESK-05：实现共享刷新与认证撤销竞态

**元数据：** WP-02；M0-R；P0；Consumer owner；M；未开始。

**硬依赖：** DESK-04。**可先做：** 与登录客户端端口对齐后，以 fake 时序测试开发。

**路径：** 拟新增 `frontend/shell/electron/consumer/refresh-coordinator.*`；auth-client 改动由 DESK-04 owner 合并。

- [ ] 为同一身份构建共享刷新 coordinator，防止并发 401 触发多次 refresh 旋转。
- [ ] 在刷新前、落凭据前校验身份与 operation token，退出时失效所有刷新等待者。
- [ ] 处理已确认的撤销/过期与未知错误，向状态机输出分类结果。
- [ ] 仅按 Consumer 正式合同重试其授权读；不顺带重放业务写或让 M0 列表自动重试。

**验收：** 正向并发授权读只进行一次共享刷新；负向 refresh 旋转、退出、换账号交错后旧凭据不可恢复；持续失败有界结束。**交付证据：** 刷新计数断言、凭据写入时序及撤销场景记录。

<a id="desk-06"></a>

### DESK-06：实现授权设备列表与 ticket provider

**元数据：** WP-02；M0-R；P0；Consumer/连接 adapter owner；M；未开始。

**硬依赖：** BASE-02、DESK-05。**可先做：** 已授权设备与无权设备 fixture。

**路径：** 拟新增 `frontend/shell/electron/consumer/devices.*`、`ticket-provider.*`。

- [ ] 从正式 Consumer 获取可访问设备集合，投影 deviceId/displayName/availability。
- [ ] main 根据最新集合检查选择，固定 applicationId/purpose/scopes 和连接 binding。
- [ ] ticket provider 绑定当前身份/设备/代次，续取前重新检查授权上下文。
- [ ] 给出离线、列表失效、目标撤销、票据拒绝的安全错误，不将 ticket 传入 renderer。

**验收：** 正向仅能选择当前授权设备；负向篡改 deviceId/scopes/applicationId、使用旧设备列表或跨代次 ticket 均拒绝；online 提示不能替代服务端授权。**交付证据：** 脱敏设备用例、binding 断言及票据不出主进程的验证。

<a id="desk-07"></a>

### DESK-07：装配 Direct SDK 与业务桥客户端端口

**元数据：** WP-02；M0-R；P0；连接 adapter owner；L；未开始。

**硬依赖：** BASE-01、BASE-03、DESK-06、SERV-01、SERV-02、SERV-03 的正式桥交付。**可先做：** fake SDK/bridge 驱动 authorizing 与拒绝分支。

**路径：** 拟新增 `frontend/shell/electron/connectivity/{native-host,session-adapter,business-bridge}.*`；仅在明确缺口后另提 SDK 仓改动。

- [ ] 使用固定 tgz、匹配 sidecar 与 ticket provider 装配 native host/session，不从开发目录随意寻找产物。
- [ ] 按 D03 绑定真实业务上下文，核对身份、应用、设备、有效期与版本。
- [ ] 统一 SDK/桥失败到状态机，缺桥保持未就绪，禁用本机 HTTP/debug 降级。
- [ ] 验证真实版本只提供已确认能力；M0 不实现流、上传或 BLE。

**验收：** 正向固定组合完成 Direct→authorizing→业务桥→ready；负向 SDK 成功而桥失败、错设备、过期桥、版本不兼容均不得放行资料请求。**交付证据：** 组件哈希/版本引用、桥合同用例及脱敏状态轨迹。

<a id="desk-08"></a>

### DESK-08：实现断开、退出与宿主进程回收

**元数据：** WP-02；M0-R；P0；宿主 owner；M；未开始。

**硬依赖：** DESK-03、DESK-05、DESK-07。**可先做：** fake close/watchdog 的有界清理测试。

**路径：** 拟新增 `frontend/shell/electron/session/shutdown.*`；宿主 owner 修改 `main.js` 生命周期与单实例控制。

- [ ] 显式断开保留登录，退出清凭据与身份临时状态，二者首先使旧代次失效。
- [ ] 有界关闭 session/native host，处理窗口关闭、重复退出和异常 sidecar。
- [ ] 与读调度器挂接待决 Promise 结算；无法确认远端停止时标记结果未知。
- [ ] 实现单实例策略，重启创建新会话生命周期，不恢复旧 session 对象。

**验收：** 正向连接/关闭两轮后进程数量恢复基线；负向 close 卡住、刷新晚到和重复退出均不会永久 pending 或残留可用旧凭据；一次读取消不触发该全局关闭。**交付证据：** 关闭上限、故障注入结果及脱敏进程树。

<a id="desk-09"></a>

### DESK-09：实现资料 query 校验和操作映射

**元数据：** WP-04；M0-L；P0；请求策略 owner；M；未开始。

**硬依赖：** DESK-02，以及 BASE-04 的草案/合成 fixture 子交付；M0-L 不要求 BASE-04 正式冻结或其上游注册任务完成。**可先做：** 依据当前草案编写纯 query validator；真实放行由 DESK-15 依赖完整 BASE-04。

**路径：** 拟新增 `frontend/shell/electron/connectivity/materials-request-policy.*`。

- [ ] 将唯一业务操作 materials.list 映射固定 GET `/api/mindos/materials`，GET 不带 body。
- [ ] 校验 limit 1–50、offset 0–10000、keyword trim 后 1–100 字符、type/status 枚举；包含 queued。
- [ ] 拒绝重复 query、空值、未知字段及 folder/tag/archived/recycled 筛选。
- [ ] 只由 main 填写受控头与 D03 身份机制，不接受 renderer 身份字段。

**验收：** 正向边界值及 queued 筛选得到确定请求；负向小数、超限、重复参数、路径/头注入与未开放筛选在 SDK 调用前拒绝。**交付证据：** 参数矩阵、请求快照和拒绝时零 SDK 调用断言。

<a id="desk-10"></a>

### DESK-10：实现资料响应投影与安全错误映射

**元数据：** WP-04；M0-L；P0；响应策略 owner；M；未开始。

**硬依赖：** DESK-09，以及 BASE-04 的草案/合成 fixture 子交付；M0-L 不要求 BASE-04 正式冻结。**可先做：** 使用合成原始 body 验证限额/字段；真实放行由 DESK-15 依赖完整 BASE-04。

**路径：** 拟新增 `frontend/shell/electron/connectivity/{materials-response-policy,public-error}.*`。

- [ ] 在 JSON 解码和投影前限制原始 body 为 256 KiB；超限整体拒绝，不返回部分列表。
- [ ] 校验条数、total 安全整数、分页回显、hasMore 和字段长度；接受 queued，拒绝未知状态。
- [ ] 仅保留规定条目与分页字段，删除顶层 folders、条目 folder/folderId、路径/正文/previewUrl。
- [ ] 返回 Promise Result 对应的安全错误；可保留已确认 status/code/traceId，不猜测丢失的 401 原因。

**验收：** 正向同一 validator 接受 fake/真实响应；负向超限、恶意文件名、错误类型、分页不一致均有界拒绝；巨大 folders 不能经“删后变小”绕过预算。**交付证据：** 字节边界测试、公开 JSON 快照及日志敏感字段断言。

<a id="desk-11"></a>

### DESK-11：实现读调度、去重和取消结算

**元数据：** WP-04；M0-L；P0；调度 owner；L；未开始。

**硬依赖：** DESK-03、DESK-10。**可先做：** fake request 延迟/失败/永不返回的时序模型。

**路径：** 拟新增 `frontend/shell/electron/connectivity/read-scheduler.*`；状态机对接由状态 owner 合并。

- [ ] 限制 2 个真实在途读、8 个排队读；按主体/代次/规范化 query 去重，健康检查共用额度。
- [ ] 排队、发出、投影交付各校验 generation；超额立即给出确定错误，M0 不自动重试。
- [ ] cancelRead 只影响同 sender/代次/目标调用，取消一个订阅不影响其余订阅。
- [ ] 取消立即且仅结算一次 READ_CANCELLED；代次失效结算旧 Promise，真实 SDK 完成才释放已发出槽。

**验收：** 正向去重请求只发一次并服务未取消订阅；负向快速取消/重复取消/迟到完成不会突破 2/8、双重结算或永久 pending；SDK1.2 不支持单读 abort 时绝不使用 session.close。**交付证据：** 调度计数、Promise 结算断言、取消与切代次压力用例。

<a id="desk-12"></a>

### DESK-12：实现独立入口、状态页和快照订阅

**元数据：** WP-01/WP-04；M0-L；P0；前端入口 owner；M；未开始。

**硬依赖：** DESK-01、DESK-02、DESK-03。**可先做：** 依据 v1 fake host 开发各状态页。

**路径：** 拟新增 `frontend/mindos-web/src/main-desktop.ts`、`src/router/desktop.ts`、`src/desktop/` 与 desktop HTML；现有 Vite 配置及必要 package scripts 由本任务独占。

- [ ] 建立 desktop 构建入口和独立路由，不导入旧 main 的会话 provision/onboarding guard。
- [ ] 实现登录、选设备、连接/授权中、错误和断开操作，只呈现合同中的 M0 能力。
- [ ] 先 subscribe 再 getSnapshot，按 sequence 合并；调用携带当前 generation，组件卸载取消订阅。
- [ ] 明确 Web/Desktop 构建选择，host 缺失时失败而非回退 fetch；开发模拟状态有明确标识。

**验收：** 正向所有 phase 能操作且快照逆序不闪回旧状态；负向未连接、IPC 不可用、手输聊天/建档路由不触发旧网络调用；Web 构建无 SDK/Electron 包。**交付证据：** 页面状态截图、网络零调用断言、构建边界检查结果。

<a id="desk-13"></a>

### DESK-13：实现 M0 资料列表与分页状态

**元数据：** WP-04；M0-L；P0；资料页面 owner；M；未开始。

**硬依赖：** DESK-10、DESK-11、DESK-12。**可先做：** fake 合同页面，可与宿主策略并行。

**路径：** 拟新增 `src/desktop/MaterialsPage.vue`、`src/services/transports/desktop-materials.ts`（均在 `frontend/mindos-web/`）；旧 `RawMaterialsPage.vue` 仅参考，不直接复用其目录/详情请求。

- [ ] 显示审核字段，默认 20 条，支持允许的 keyword/type/status 筛选及 queued 文案。
- [ ] 实现分页、空态、只读用户重试与超限/拒绝提示；筛选变化重置 offset。
- [ ] 按主体/代次/查询保存当前列表，切换/断开同步清理，旧 Promise 只结算不回写新页面。
- [ ] 页面卸载取消本地读投递；不开放详情、目录、上传、删除或固定周期列表轮询。

**验收：** 正向空页、多页和 queued 筛选正确；负向 A 设备慢响应晚于切 B 后不得显示 A 行，目录/预览字段不可见，超限结果不显示半页；连续刷新受 DESK-11 限制。**交付证据：** 分页 fixture、状态切换截图与迟到回包用例。

<a id="desk-14"></a>

### DESK-14：建立 M0 隔离合同与故障回归集

**元数据：** WP-05；M0-L；P0；测试 owner；L；未开始。

**硬依赖：** BASE-05、DESK-02、DESK-03、DESK-11、DESK-13。**可先做：** BASE-05 后建立 fixture、断言和故障矩阵；正式 auth 任务未完成时只运行 fake 局部层。DESK-08 的关闭合同先以 fake 端口覆盖，真实 OS/SDK 清理由 DESK-15 合流验收。

**路径：** 拟新增 `frontend/shell/tests/`、`frontend/mindos-web/tests/desktop-*.test.mjs`；测试 owner 独占用例，业务修复回交模块 owner。

- [ ] 将接口规格 M0-01–10 分配到 IPC、状态机、adapter 与页面层，列出无法由 fake 证明的真机项。
- [ ] 使用临时凭据 store/状态目录和合成资料，模拟认证/桥拒绝、乱序回包、超时、断开、超限。
- [ ] 增加进程/订阅/Promise 泄漏断言，覆盖 2/8 调度、queued、原始 256 KiB 上限。
- [ ] 固定测试入口和可复现实例，运行受影响的现有前端回归与构建边界检查。

**验收：** 正向隔离集可重复运行且通过；负向不存在访问用户运行库、真实凭据或默认 supervisor 的路径；故意移除代次或 sender 校验时对应测试失败。**交付证据：** 实际命令、测试结果、fixture 来源及 L1/L2 与未验 L3 清单。

<a id="desk-15"></a>

### DESK-15：执行正式 M0 跨仓联调与验收

**元数据：** WP-05；M0-R；P0；集成测试 owner；L；未开始。

**硬依赖：** BASE-01、BASE-02、BASE-03、BASE-04、BASE-05、DESK-04、DESK-05、DESK-06、DESK-07、DESK-08、DESK-14、SERV-01、SERV-02、SERV-03。**可先做：** 编写验收步骤与证据模板。

**路径：** 拟新增 `docs/development/M0-ACCEPTANCE-0905.md` 和按 BASE-05 约定保存的脱敏证据；跨仓改动由原 owner 修复。

- [ ] 固定 OS/CPU、客户端/SDK/sidecar/Agent/后端版本及正式注册配置，核对 local-debug 关闭。
- [ ] 同环境完成登录→选盒→实际资料分页→断开两轮，记录受控样例中的 queued 与字段投影。
- [ ] 用合法测试账号/设备执行两设备切换、同盒不同账号、撤销/过期、断网与旧响应负向矩阵。
- [ ] 核对无本机后端、无凭据泄露和退出残留；缺环境项标为未验，问题交回对应任务复验。

**验收：** 正向正式资料 API 读通过；负向越权/失效和隔离矩阵符合冻结合同；health/connect/fake 成功不能替代资料读。缺第二设备或第二账号时 M0-R 不关闭。**交付证据：** 脱敏关联 ID、版本哈希、逐场景预期/实际及最终验收矩阵。

## M1：全域传输入口与状态归属

<a id="desk-16"></a>

### DESK-16：收敛普通 RPC 与路由任务网络入口

**元数据：** WP-06；M1；P1；transport owner，宿主策略 owner 配合；L；未开始。

**硬依赖：** DESK-14、DESK-18；真实领域调用放行另依赖 WP-07 对应 API 合同。**可先做：** M0 合同稳定后盘点 operation；无需等待所有领域表迁移完成。

**路径：** 现有 `src/services/api.ts`、`taskRouting.ts`；拟新增 `src/services/transports/{contract,web,desktop-rpc}.*`，均在 `frontend/mindos-web/`。宿主配套拟新增 `frontend/shell/electron/ipc/domain-rpc.*`、`connectivity/domain-operations.*` 及对应请求/响应 validator；同步桌面合同/preload类型，装配与共享策略由宿主 owner 合入。

- [ ] 按 operation 盘点现有普通 API、routing preview/grant/exception，标记只读、写、取消和授权合同。
- [ ] 抽出 Web/Desktop transport 端口；桌面只映射已批准 operation，不把任意 path/method 泛化为 IPC。
- [ ] 按 SERV-13 的路由矩阵实现 main handler 与各 operation 的参数/响应/权限/预算校验；在 SDK 调用前后复核主体与代次，新增操作经 preload 窄合同暴露。
- [ ] 保留错误码与 requestId，禁止把所有 409/401 自动重试；未批准能力给出确定拒绝。
- [ ] 分批切换 api/taskRouting 的底层调用并加入覆盖检查，桌面不读取 renderer sessionToken。

**验收：** 正向 SERV-13 已批准普通操作从页面经 preload/main/SDK 到领域 API，Web 行为回归通过；负向未知操作、伪身份、非法正文、越界响应及换设备后的旧授权/预览不可执行，桌面没有直接 fetch 回退。**交付证据：** renderer/main/后端一一对应的 operation 覆盖表、允许/拒绝合同用例及受影响回归。该任务负责端口与宿主逐操作适配，服务端业务实现归 SERV 系列；盘点结果超过 L 时按独立领域 operation 组拆子任务后派发。

<a id="desk-17"></a>

### DESK-17：收敛 SSE/聊天入口与能力门控

**元数据：** WP-06；M1；P1；transport owner；M；未开始。

**硬依赖：** DESK-16；真实聊天放行另依赖 WP-08 流/任务合同及对应验证。**可先做：** 提供模拟事件端口和未支持返回，不假装 SDK1.2 有 stream。

**路径：** 现有 `src/services/sse.ts`、`chatStream.ts`；拟新增 `src/services/transports/chat-port.*`，均在 `frontend/mindos-web/`；扩展 DESK-12 的 desktop 路由/导航，路由改动由前端入口 owner 串行合入。

- [ ] 将第三个 fetch 入口收敛到可注入 chat port，Web 使用原有流解析，desktop 绑定未来版本化端口。
- [ ] 保留有限预览 409 恢复及稳定 requestId，已开始返回事件后不得重新发送消息。
- [ ] 使切页 unsubscribe 与显式停止分离，未知传输版本保持聊天 capability 关闭。
- [ ] 与 WP-08 owner 交付事件/取消 adapter 接口与 fixture；WP-08 独占后续协议实现和同文件修改接力。
- [ ] 按真实能力矩阵接入 M1 导航/业务路由；只有身份 ready 且已部署相应领域/消息合同才触发建档守卫，能力失效立即阻止新调用。事项新读写/清除入口分别等待 DESK-21–23 的合同交付。

**验收：** 正向 Web 聊天与模拟端口保持事件语义，合成能力矩阵能挂载相应业务路由；负向 SDK 整包响应不能伪装首帧到达，未 ready/未支持时导航及手输深链均不发建档或聊天请求，流开始后错误不重放。**交付证据：** 三入口覆盖检查、路由/守卫矩阵、事件 fixture 和重试边界测试；实际领域主链与长流验收由 CONN-07 合流记录，不作为本任务模拟接线的反向依赖。

<a id="desk-18"></a>

### DESK-18：建立全域缓存归属与生命周期基础设施

**元数据：** WP-06；M1；P1；缓存基础 owner；M；未开始。

**硬依赖：** DESK-03、DESK-12。**可先做：** 独立 scoped storage 与内存 registry 测试，可与真实身份桥并行。

**路径：** 拟新增 `frontend/mindos-web/src/shared/identityScope.ts`、`src/composables/scopedState.ts`；桌面状态订阅接线由 DESK-12 owner 合并。

- [ ] 定义账号/客户端/设备/对象缓存 key 与 schema 版本；由 main 根据 D02 和验证主体提供经过审核、无凭据的稳定不透明 scope 标识，不信任 renderer 自报 clientId。当前 v1 快照只有 accountId/deviceId，新增标识是本任务拟做的合同变更，须同步接口文档/类型、main 投影与兼容检查。generation 用于执行失效，不简单混入持久草稿 key。
- [ ] 定义同主体重连保留草稿、账号/设备切换不可见、退出清理的生命周期通知。
- [ ] 增加内存、sessionStorage、对象 URL、订阅统一注销端口；旧无归属缓存不自动迁入。
- [ ] 区分用户主动导出的文件与应用临时缓存，不误删导出文件；记录清理范围。

**验收：** 正向同账号/客户端/设备重连 scope 稳定且可找回允许保留的草稿；负向换账号/客户端/设备后标识变化，同对象 ID 不可串读，renderer 伪造 scope 不能改变授权，旧无归属键不接管，退出后旧订阅不能写入新 scope。**交付证据：** 更新后的合同/类型、scope 投影测试、key/清理策略表、存储隔离与失效事件用例。

<a id="desk-19"></a>

### DESK-19：隔离聊天草稿、失败恢复与待处理对话

**元数据：** WP-06；M1；P1；聊天缓存 owner；L；未开始。

**硬依赖：** DESK-18；与 transport 接线合并时依赖 DESK-16。**可先做：** 在合成主体下迁移组件缓存，不依赖真流。

**路径：** 现有 `src/components/conversation/Composer.vue`、`src/composables/useReplyRecovery.ts`、`src/pages/ConversationPage.vue`、`src/services/matters.ts` 中 pendingConversations，均在 `frontend/mindos-web/`；不改 taskRouting 共享文件。

- [ ] 将输入/辅助句 origin/undo/失败草稿、replyRecoveries 按 scope 与 conversation 归属。
- [ ] 将 pendingConversations 的创建/绑定回包绑定 scope，防止跨设备复用半完成对话。
- [ ] 同主体恢复保留原文与来源校验；换主体使挂起写、恢复回包与授权弹窗不可再投递。
- [ ] 检查组件卸载/重新挂载与退出清理，保留产品当前“不移除来源后自动发送”的恢复语义。

**验收：** 正向同主体失败后可恢复原文；负向相同 conversation/matter ID 跨主体不出现旧草稿、旧辅助句或旧绑定，迟到恢复不能覆盖新输入。**交付证据：** 聊天恢复与对话管理回归、scope 切换 fixture、半完成绑定竞态测试。

<a id="desk-20"></a>

### DESK-20：隔离事项与章程编辑状态和操作键

**元数据：** WP-06；M1；P1；编辑器缓存 owner；L；未开始。

**硬依赖：** DESK-18；真实事项写放行另依赖 WP-07 的幂等/归属合同。**可先做：** 仅用合成 DTO 验证编辑器状态，不触发真实写入。

**路径：** 现有 `src/components/matters/MatterWorkspace.vue`、`src/components/conversation/CharterWorkspaceEditor.vue`，均在 `frontend/mindos-web/`。

- [ ] 按 scope/object/revision 管理事项编辑、selectedArtifact、pendingMessage 与 operationKeys。
- [ ] 迁移章程 buffer 与 legacy buffer 读取规则，拒绝自动接管无身份旧缓存。
- [ ] 同主体编辑保留可恢复草稿；切主体中止旧提交投递，不能把旧 requestId 自动用于新主体写。
- [ ] 保留未保存 A 文档时保存 B 回复的保护，明确冲突/来源变化/退出的工作稿处理。

**验收：** 正向同主体重连保留允许的未保存工作稿并按原 requestId 处理同次写意图；负向跨主体同 ID、旧 revision、旧 operationKeys、A 未保存而保存 B 均不污染或覆盖正文。**交付证据：** MatterWorkspace/章程现有回归、scope 隔离、dirty/冲突与幂等键用例。

<a id="desk-21"></a>

### DESK-21：接入事项、成果与历史分页和单版读取

**元数据：** WP-06/WP-07；M1；P1；事项读取页面 owner；L；未开始。

**硬依赖：** DESK-16、DESK-18、SERV-14。**可先做：** 根据 SERV-14 草案编写摘要、游标与历史 fixture；真实页面合同验收等待服务端冻结。

**路径：** 现有 `frontend/mindos-web/src/components/matters/MattersHome.vue`、`src/services/matters.ts`；拟新增 `src/components/matters/{ArtifactList,MatterHistory,ArtifactVersion}.vue`。`matters.ts` 与 DESK-19 的 pendingConversations 改动串行合并；MatterWorkspace 挂载由 DESK-20 owner 接力，不并行改共享组件。

- [ ] 将事项/成果/历史列表接入冻结后的摘要 DTO、limit/nextCursor，列表不继续依赖携带全部 markdown 的旧响应。
- [ ] 以 scope、筛选和排序绑定分页状态；筛选/主体切换重置游标，按稳定对象 ID 去重并提供显式刷新，不承诺弱一致分页无漏项。
- [ ] 历史按服务端不可变顺序展示，通过已冻结的单版读取操作按需获取正文；区分当前版、历史版和未保存工作稿。
- [ ] 展示过期/不匹配游标、单页/单版超限与无权限错误，禁止静默截正文或在合同缺失时自行猜 URL。

**验收：** 正向多页、同时间戳记录、历史单版与当前版均可独立读取，重复翻页不重复渲染；负向并发更新后可明确刷新、旧 scope 游标不沿用、单版超限不展示残缺正文，打开历史不覆盖未保存编辑。**交付证据：** 服务端合同版本、摘要/全文请求矩阵、游标过期与越权 fixture、弱一致刷新及字节预算用例。

<a id="desk-22"></a>

### DESK-22：呈现事项写幂等、revision 冲突与不确定结果

**元数据：** WP-06/WP-07；M1；P1；事项编辑器 owner；M；未开始。

**硬依赖：** DESK-20、DESK-21、SERV-15。**可先做：** 以冻结前错误草案演练“成功丢包/冲突/结果未知”页面，错误码仍以正式合同为准。

**路径：** 现有 `frontend/mindos-web/src/components/matters/MatterWorkspace.vue`、`src/services/matters.ts`；拟新增事项写操作状态模块。上述共享文件在 DESK-20/21 合并后接力修改。

- [ ] 为一次用户写意图固定原 requestId 和规范化原始输入；省略值保持省略，不在再次提交时重新解析来源或默认标题。新的编辑意图生成新键。
- [ ] 分别处理实体 revision、bindingRevision 冲突与幂等键复用错误；409 保留工作稿，展示服务端最新版本并让用户选择下一步，不自动覆盖。
- [ ] 成功回包丢失/超时时呈现结果未知，依冻结合同核验或允许用户明确再次提交同一原请求；不自动重放、不生成新键伪装同次重试。
- [ ] 换主体使旧写投递失效；对超出回放窗、已清除结果或来源变化给出对应状态，不把“无法确认”显示成已成功或直接创建新成果。

**验收：** 正向同次用户重试保持键和输入，服务端只形成一次成果/修订；负向同键改内容、两个编辑者 revision 冲突、成功丢包及换设备均不覆盖草稿或产生后台重放；新意图使用不同键。**交付证据：** 请求键/输入快照、冲突截图、成功丢包与保留窗 fixture，以及与 SERV-15 的前后端合同用例。

<a id="desk-23"></a>

### DESK-23：接入清除预览、显式确认和进度清理

**元数据：** WP-06/WP-07；M1；P1；事项生命周期页面 owner；L；未开始。

**硬依赖：** DESK-21、DESK-22、SERV-16、SERV-17。**可先做：** 使用合成预览与清除状态设计页面；真实入口等待清除范围、确认字段和路由全部冻结。

**路径：** 拟新增 `frontend/mindos-web/src/components/matters/MatterClearDialog.vue`、事项清除状态模块；修改事项页与 `src/services/matters.ts` 的受控操作映射。与 DESK-21/22 同一事项 owner 串行接力，transport 新操作由 DESK-16 owner 审核。

- [ ] 展示 SERV-16 冻结的对象、全文历史、来源快照、关联对象、备份/回放保留范围，区分清除与暂停/完成/解绑。
- [ ] 按正式合同提交预览及显式确认；过期预览或范围变化重新核对，不自创 DELETE 路由或绕过确认。
- [ ] 展示持久清除操作的进行中、失败、可恢复和完成状态；断线后通过已冻结状态读取恢复展示，不在前端猜测任务已完成。
- [ ] 按完成回执失效选定 scope/object 的摘要、历史、正文、草稿和恢复状态，阻止旧在途回包复活已清除内容；用户导出文件和范围外对象不删除。

**验收：** 正向预览范围与确认结果一致，中断后可恢复状态，完成后重新打开列表/历史/回放入口均不出现已清除正文；负向取消确认、过期预览、旧请求迟到、换主体和服务端部分失败不误报完成或误删范围外内容。**交付证据：** 合成范围确认截图、持久任务故障矩阵、缓存/迟到响应防复活测试及与 SERV-17 对齐的清除回执用例。

## 派发与验收约束

每个任务交付 PR/提交、实际测试文件与命令、结果摘要、未验项以及影响的合同版本。M0-L 与 M0-R 分别记录，不能用文档或 fake 测试状态代替正式环境通过。服务端桥、领域迁移、流和上传的实现任务见总表；本文件不擅自增加信任头、正式 ID 或流式 SDK API。

建议并行：DESK-03 状态机、DESK-09/10 策略、DESK-12 页面按共享类型同步；正式注册等待期间推进这些局部任务。DESK-16→17 由同一 transport owner 接力，DESK-18 完成后 DESK-19/20 可并行。DESK-21→22→23 依服务端冻结合同接力，事项服务和编辑器共享文件遵守上述 owner 边界。装配与共用锁文件修改统一回交宿主 owner；M0 仍需 DESK-14/15 合流验证，新增事项页面另进入总表 M1 验收。
