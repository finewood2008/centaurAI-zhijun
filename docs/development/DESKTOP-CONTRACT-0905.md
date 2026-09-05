# 知君桌面集成接口规格 v0.1

日期：2026-09-05。代码基线：知君 `94239a1`（产品源 `22dc9a3`）。配套：[架构](ARCHITECTURE-0905.md)、[集成分析](INTEGRATION-0905.md)、[工作包](INTEGRATION-WORKPACKAGES-0905.md)、[领域迁移](DOMAIN-INTEGRATION-0905.md)。

**状态：可用于拆任务和编写模拟合同的设计稿，未实现、未冻结跨团队协议、未注册正式应用。** 本文中的桌面接口、错误码和数值策略属于拟新增的知君应用合同；它们不是 SDK 已导出的 API。现有 SDK 能力和限制以集成分析为准。

## 本轮文档计划与验收

- [x] 核对已有架构、SDK request/close 及 data-engine 资料列表/策略源码。
- [x] 写明 M0 范围、宿主接口、连接生命周期、资料投影、错误/取消与跨仓合同。
- [x] 输出可类型检查的接口样例及逐包交付/验收，明确未决输入。
- [x] 更新主文档导航，交叉审核引用/类型/图示；检查结果见第10节。

文档交付分支为 `dev/first-integrate-check-0905`，提交及远程同步状态以 Git 记录为准。

涉及文件仅为本文、接口样例、工作包/领域规格及文档导航；本轮不改业务代码、依赖、相邻仓库或运行数据。验收不是“所有外部问题已解决”，而是每个工作包都有输入、输出、依赖和验证标准，未知字段不会变成隐式生产默认值。

## 1. 第一个可交付范围 M0

用户流程：打开新桌面宿主 → 登录 → 选择已绑定盒子 → 建立 Direct 会话 → 完成业务身份桥 → 读取资料分页 → 断开/重连/退出。首个平台按 macOS ARM64 编写开发计划，平台承诺仍以 D05 的发布范围为准。

M0 的业务能力只有 `materials.list`。认证/设备控制及内部健康检查是支撑动作。聊天、首次建档、事项写入、附件上传、资料详情/预览、目录管理、模型管理和 BLE 入口暂不开放；桌面路由不能先执行现有 onboarding guard。列表只展示已审核字段，禁止显示共享目录名称/计数或经由旧 Web transport 回退到 PC 数据。

验收需在关闭 local-debug 的真实盒子上完成成功与拒绝用例。Mock 宿主、fixture 后端和健康检查只证明各自局部行为，不代替这次真实读取。

## 2. 实施基线与待输入决策

| ID | 本规格采用的设计方向 | 必须补齐的输入 | 在输入前可推进 / 不可放行 |
| --- | --- | --- | --- |
| D01 领域承载 | 盒端 data-engine 内的独立知君模块，服务适配隔开基础资料能力 | 服务端维护方确认模块入口、表迁移和生命周期；Claim事实源与owner/device规则 | 可做依赖清单/临时库验证；不可指向运行库合并表 |
| D02 应用身份 | 独立知君 desktop 身份，主进程持凭据 | applicationId、purpose、scopes、Consumer/Gateway/JWKS受信地址、登录合同、客户端注册/安全存储namespace、目标设备授权 | 可实现注入式auth adapter和模拟登录；缺项时真实连接返回配置未就绪 |
| D03 业务身份桥 | 优先盒端可信桥，由已验证连接主体取得业务上下文 | Agent可证明的主体字段、签发/验签方、IPC/内部握手、应用路径授权、TTL/撤销/续期、版本 | 可实现bridge端口和拒绝路径；不可仅以P2P成功设置ready |
| D04 长请求/上传 | 聊天优先扩展现有分帧链路；上传统一当前Pocket parts/complete形态 | SDK/Core/Agent流与取消版本、后台任务备选取舍；上传四层合同及获批会话预算 | 可做流模拟器/上传adapter合同；不能把SDK1.2整包request当stream，也不自动试多个上传路径 |
| D05 交付组合 | 独立desktop构建，sidecar置于ASAR外 | SDK tgz哈希、sidecar输入与二进制哈希、Agent/服务端提交、协议版本、OS/CPU与签名结果 | 可做本地包边界检查；dirty来源未核对前不能宣称可重建发布组合 |

这些是规格编写的工作假设与决策入口，不等于产品/服务方已经批准。决策记录格式为 `ID / 结论 / 负责角色 / 代码或合同版本 / 验收证据 / 生效范围`；不需要在仓库中记录密钥或真实账号 token。

## 3. Renderer → preload → main 的窄接口

拟使用 `window.zhijunDesktop`，接口版本为 `1`；类型见 [desktop-contract-v1.ts](contracts/desktop-contract-v1.ts)。示例不注入 window、不注册 IPC、不导入 SDK、不访问网络。

| 操作 | 输入 | 公开输出与语义 |
| --- | --- | --- |
| `getSnapshot()` | 无 | 当前连接状态/代次/事件序号及公开主体；用于初始加载和事件缺失后重取 |
| `subscribe(listener)` | 本地回调 | 返回 unsubscribe；只发送公开快照，不将 IPC event 对象传给 renderer |
| `beginSignIn(context)` | callId、expectedGeneration | 启动主进程控制的登录过程并返回快照；完成结果通过快照通知。登录手段由D02确定，renderer不提交已有access/refresh token |
| `listDevices(context)` | 已登录代次 | 已审核的设备标识/显示名/在线提示；在线提示不代表可建立连接 |
| `connect(context, deviceId)` | 设备选择 | 仅允许属于最新授权设备列表的目标，服务端仍再授权；main构造固定binding |
| `disconnect(context)` | 当前代次 | 立即失效旧执行，再有界回收连接；保留登录身份，回到选设备 |
| `signOut(context)` | 当前代次 | 失效旧执行/刷新回包，回收连接，清安全凭据与该身份临时缓存 |
| `materials.list(context, query)` | limit/offset及可选keyword/type/status | 已校验列表投影；无相对路径、host、headers或身份字段输入 |
| `cancelRead(context, targetCallId)` | 同一sender/代次的读请求 | 返回已停止投递/未找到；M0仅取消排队或忽略回包，不保证服务端停止 |

`context.callId` 是 IPC 关联标识，建议 UUID，长度 8–100、字符限定字母数字/下划线/连字符；重复的在途 callId 被拒绝。它不替代未来领域写入的 requestId。领域 requestId 标识同一次用户写意图，获准重试时保持不变；重新登录/换设备后不得自动恢复另一个主体的写请求。

main 对每次调用做运行时结构校验、准确 sender WebContents/主frame/应用URL校验。TS类型不等于IPC安全校验。contextBridge不能暴露任意ipcRenderer、fetch、文件路径、宿主对象；业务URL只由main按operation映射。

## 4. 状态、代次与竞争处理

`phase` 可为 signed_out/authenticating/selecting_device/connecting/authorizing/ready/disconnecting/failed。SDK完成connect只进入 authorizing；身份桥按 D03 成功、主体与binding一致且业务会话有效后才进入 ready。ready不是本轮资料读取成功的证据，M0验收还要实际读取资料。

- main唯一生成 `generation`，初值0。登录主体变化、开始切设备/连接、显式断开/退出或会话失败失效时递增；renderer不能自选更高代次。
- 业务调用的 expectedGeneration 必须等于当前 generation；请求排队、发出、完成投影时分别复核，旧结果不会写回新页面。
- beginSignIn/connect/disconnect/signOut 先检查入参代次，再分配新代次。该控制操作的后续完成与新代次及内部operation token绑定，避免它因自身递增被误判为旧请求；新的控制操作会使旧操作迟到结果失效。重复登录及退出后的旧认证回调不得恢复凭据或连接。
- `sequence` 是 main 生命周期内全局递增的快照序号；renderer丢弃旧快照。订阅先注册再getSnapshot，按sequence合并，主进程重启会创建新窗口及新生命周期。
- 显式断开保留登录状态；账号退出删除凭据。会话失效不自动登出，先分类原因，再由用户重连；未知错误不进入无限重连循环。
- native close/watchdog 有界结束后清理资源；无法确认某个远端执行已取消时，记录“结果未知”，不能展示“服务端已停止”。

普通页面切换可取消该页面的 M0只读投递。后续聊天切页只是解除订阅，让同主体任务完成落库；明确停止、换账号/设备、退出遵守流合同。两者不能共用“组件卸载必关闭整场SDK会话”的实现。

## 5. M0 资料请求与响应合同

### 5.1 请求映射

`materials.list` → `GET /api/mindos/materials?limit=20&offset=0`。limit必填1–50，offset必填0–10000；均为整数，不接受重复query、空值或额外字段。建议页面默认20条，属于应用策略。可选 keyword 为trim后1–100字符；type取document/image/audio；status取uploaded/queued/processing/available/failed。

GET没有body。main只构造受控 `Accept: application/json` 及 D03 的内部业务身份机制；renderer不能提交X-MindOS-Session、Authorization或CSRF头。M0不开放folder/folderId/tag/archived/recycled筛选，后续能力按单独合同扩展。

服务端已支持该分页与状态集合：[后端列表](../../../nexusaos-data-engine/backend/mindos/uploads.py#L389)。参考[PC策略](../../../nexusaos-data-engine/frontend/electron/connectivity/request-policy.js#L76)及其响应枚举尚缺queued，而后端摄取队列已经返回该值。因此知君新main的query校验、响应投影及UI状态须同步包含queued，不能原样复制旧PC策略；实际Agent/盒端组合再通过合同测试。集合外的未来status先报告合同不兼容，不静默转换成“available”。

### 5.2 公开投影

仅保留 `items[{materialId,fileName,fileType,status,createdAt}]` 与 `total/limit/offset/hasMore`。其中 materialId为非空标识，fileName为纯文本，createdAt保留经验证的时间字符串。最大长度分别为256/512/64字符；禁止控制字符，不将fileName当HTML或路径使用。参考现有投影实现，但**删除其中的folder/folderId**：[response-policy](../../../nexusaos-data-engine/frontend/electron/connectivity/response-policy.js#L332)。

顶层 folders、条目folder/folderId、宿主路径、previewUrl、正文、诊断对象均不透出M0。服务端返回items须为数组且不超过请求limit；total为非负安全整数，分页回显与请求一致。hasMore按 `offset + items.length < total` 校验/计算；并发写入可能造成下一页变化，不承诺跨页事务快照。

拟沿用参考PC资料列表256 KiB的原始响应body限制；必须在JSON解码/投影前限制，不能先接收无限数据再删folders。若共享folders本身使原响应超限，M0返回响应超限，并推进服务端受控列表投影；不得靠抬高限制或前端隐藏掩盖问题。上层业务投影不修复后端目录隔离，目录写仍不开放。

### 5.3 错误与读取消

异步方法统一返回 `Promise<Result<T>>`（subscribe是本地订阅接口）：成功包含当前generation与data；失败包含结构化public error，不透原生Error/stack/token。错误分为配置、未登录、未就绪、代次失效、输入拒绝、权限拒绝、会话失效、网络/超时、配额、合同不兼容、响应超限、业务失败和读投递取消。

HTTP业务失败尽量保留status/code/traceId；只有经核对的安全message进入UI。没有traceId就省略，不伪造服务端关联ID；SDK错误不强塞HTTP状态。当前服务端某些401/403已丢细分code，adapter不得猜测“过期”还是“撤销”，用受限提示要求重连/核验权限。

M0默认不自动重试。用户可重试只读列表；纯排队取消不发请求，已发出请求取消只丢回包并继续计入真实在途预算。SDK1.2没有单请求abort，不能用 session.close 终止一次列表读取而误伤其他请求。未来读取重试须有次数/退避/同主体代次约束。

取消成功时，被取消订阅对应的 `materials.list` Promise立即且只结算一次 `READ_CANCELLED`；底层迟到完成不再结算它，仍供其他未取消订阅使用。代次失效或断开时也要结算旧待决调用（如 `STALE_GENERATION` 或已确认的会话错误），避免永远pending；这些失效结果只结束旧调用，不能写入新主体页面。调度槽释放与Promise结算是两件事，仍按真实SDK完成时刻释放额度。

## 6. 调度与能力声明

M0拟采用：最多2个在途业务读、最多8个排队读；同主体同query的重复读取合并；取消一个订阅不影响仍在等待的订阅。健康检查与业务请求共享调度和实际额度，窗口后台不做固定周期列表轮询。服务端配置若更严格，取各层最小值。

当前参考PC Agent是8并发/120每分钟/64MiB会话，Core还有限1024次和默认60秒；本规格的2/8是拟应用策略，并非SDK新增限制。已发出但被本地取消的请求仍占真实在途槽，直到SDK结束；否则renderer可用快速取消突破并发预算。

快照中的capabilities由“已部署服务合同 + 主进程策略 + SDK/Agent版本 + 当前主体授权”共同得出。D05清单尚未齐备时不通过猜测URL或200响应探测开放功能。M0只可能开启materialsRead；streamChat、uploads、matters、provisioning始终为false。

## 7. 身份桥的跨仓输入/输出

推荐责任：Admin证明账号/客户端/设备授权；SDK/Core传递经过验证的连接binding；Agent只向已批准盒内目标转发；可信桥把该连接转换成data-engine接受的业务上下文；data-engine执行数据范围与领域权限。

D03必须交付这些可核对字段与行为，而非仅一条路由名称：

| 项目 | 必须确定的合同 |
| --- | --- |
| 主体来源 | accountId/clientId/deviceId/applicationId/scopes/purpose的验证者、传递载体、audience与授权路径；缺字段拒绝，不从renderer补信任 |
| 会话绑定 | 连接ID/业务sessionID如何关联；换设备/应用不能复用；本地循环地址不是身份依据 |
| 交换与密钥 | 优先Agent/盒内可信交换；签名验证、密钥归属与轮换、请求防重放、期限；如走专用JWT路径须验证现有512byte Bearer上限 |
| 生命周期 | 连接与业务会话的期限取交集；刷新/续期、撤销、owner变化、Agent/后端重启、旧代次清理和失效错误 |
| 数据范围 | 设备级资料可读范围、同盒多账号与转让处理、各路由guard；不能把旧global数据直接归给登录者 |
| 可观测性 | 只记录callId/连接关联ID/版本/操作码/耗时/安全错误；原始ticket、token、资料正文不进日志 |

当前代码没有实现这条闭环，`X-MindOS-Session`也不在Agent外部header白名单内。本文不擅自增加HTTP交换路由、信任头或私有签名格式；这些由D03明确后进入跨仓合同测试。M0可先在本仓通过注入的 fake bridge 验证状态机，但真实连接必须等待真实桥交付。

## 8. 后续流与上传合同的最小完成定义

**流：** 明确stream/request/业务requestId的关联；response head、顺序chunk、terminal end/error、单请求cancel和确认；UTF-8增量解码、队列/背压限额、首帧/空闲/总deadline、断线后的持久状态查询、会话级失败的影响。推荐沿已有Agent chunk扩展Core及sidecar，不把同一JSON line无限放大。协议版本/协商位置由D04落实；出现不兼容时关闭聊天入口。

**上传：** 优先统一到现有未提交Pocket的init/parts/complete/DELETE；固定1MiB分片、1-based编号、Idempotency-Key、初始化字段、状态联合类型、同主体续传和完整性检查。必须同步main policy、Agent应用manifest与后端，而不是改两个URL。请求/会话总额度、上传副本归属、版本上传和chat-import保护各有验收；不通过重连绕过会话配额。冻结前仍不开放M0上传。

## 9. M0 的必须通过用例

| 编号 | 输入/事件 | 期望 |
| --- | --- | --- |
| M0-01 | SDK连通但业务桥缺失/拒绝 | 不进入ready，不发renderer业务请求，无debug回退 |
| M0-02 | 正式身份读取20条资料，包含queued记录及对应筛选 | 只含规定字段、正确状态与分页，无共享folders及条目目录信息 |
| M0-03 | 错误设备/账号/应用或已撤销身份 | 按层拒绝，不能用health成功替代资料鉴权 |
| M0-04 | A请求在途，切B后A回包 | generation不同，旧UI/缓存/事件全部不可写入B |
| M0-05 | 两次登录/connect交错，退出时认证或refresh晚到 | 新控制操作胜出，旧结果不能恢复凭据或连接 |
| M0-06 | 伪sender/子frame/任意header/query/无效分页 | main在SDK调用前拒绝 |
| M0-07 | 资料body超过256KiB/字段类型错误 | 有界拒绝，不输出部分可信列表，不打印原body |
| M0-08 | 取消排队/已发出的读，或调用中切代次 | Promise有界且仅结算一次；排队取消不发送，已发出时不关闭整个session，在途槽待真实完成释放 |
| M0-09 | 会话额度/超时/Direct失败 | 有限结束并分类提示；不无限重试、不切到未批准transport |
| M0-10 | 关闭窗口/重启 | 无sidecar残留，退出凭据与草稿按合同清理；重启不恢复旧session |

通过记录应含客户端/SDK/sidecar/Agent/后端固定版本、测试方法、结果与脱敏关联ID。类型样例和模拟用例通过后才能进入真实联调，但两者的完成状态分别记录。

## 10. 本轮文档验证记录

2026-09-05：6份新增/修改Markdown的153处本地链接及代码块闭合检查通过；架构文档4段Mermaid均通过解析和浏览器SVG生成，图源码未变，保留既有SVG。接口样例通过以下严格类型检查（知君仓库根执行）：

```sh
rtk proxy frontend/mindos-web/node_modules/.bin/tsc --noEmit --strict --target ES2022 --module ESNext --moduleResolution Bundler --skipLibCheck docs/development/contracts/desktop-contract-v1.ts
```

独立交叉审核后补齐queued状态、取消调用结算与登录竞态，统一迁移编号、外部输入边界和后端测试selector写法。本轮未修改业务代码，未运行服务或业务回归，未连接真实盒子；既有产品回归结果仍以同步记录为准。
