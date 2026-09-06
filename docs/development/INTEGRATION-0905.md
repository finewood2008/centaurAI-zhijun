# 知君 Electron SDK 与 data-engine 集成方案

更新：2026-09-06。当前方案已由早期只读调研收敛为 v2 完整产品接入实现；**盒端Agent/DE/worker/catalog已正式匹配部署；正式Consumer/SDK/P2P/UI验收仍待完成，Admin生产发布路径未提供**。本文不把源码完成、合成测试或健康检查写成端到端交付。

配套：[架构图](ARCHITECTURE-0905.md)、[桌面合同](DESKTOP-CONTRACT-0905.md)、[领域规格](DOMAIN-INTEGRATION-0905.md)、[执行计划](FULL-PRODUCT-INTEGRATION-0906.md)、[真实验收](REAL-ACCEPTANCE-0906.md)。

## 1. 已确定的集成路线

| 决策 | 当前实现 | 发布边界 |
| --- | --- | --- |
| SDK | 保留已交付 Connectivity SDK 1.2.0，main 独占 sidecar/Core | 原生 API 是整响应 request/close，不宣称已有 stream/abort |
| 产品面 | 15 页、20 路由、2 重定向；170 项共享 catalog | 菜单/路由覆盖不等于逐业务操作实测 |
| 新身份 | `zhijun-desktop` / `zhijun.workspace` / `remote.p2p` / Direct-only | Admin 独立登记，不能借旧 read scope 写业务 |
| D03 | v2 独立签名域、原始 body SHA256、Content-Type 与严格路径 | v1 只读应用、路由与签名行为保留 |
| 长任务 | DE 真实异步任务、有界 poll/cancel，renderer 恢复 SSE | 不通过长 HTTP 请求等待模型，不自动重放不确定写入 |
| 大文件 | v2 512 KiB/0-based 暂存、multipart 引用、blob 分块读取 | 不是把历史 Pocket 1 MiB/1-based 路由直接开放到 PC |
| 领域承载 | Gateway 管理的每 workspace 独立 UDS worker | 不启动旧 server 的全局 watcher/Chroma/模型设施 |
| 数据归属 | account/device/ownershipEpoch 派生 workspace；任务再绑定 client/session | 旧 global 不自动迁移；运行数据不进入 Git |

早期 M0-L/v1 设计与 D04 原生 SDK chunk 扩展建议保留在历史任务记录中。本轮选择真实盒端短任务通道实现产品流式能力，不并行建设第二套 SDK 流协议。

## 2. 共享操作与客户端装配

[product-operations.json](../../frontend/shared/product-operations.json) 当前共 170 项：domain 95、materials 54、models 21。每项包含允许方法、模板、参数名、query、body/response 类型、`maxRequestBytes/maxResponseBytes`。Vue transport 把原 API 调用映射为 operationId；main 和 DE 再验清单及有界 DTO，不能传入任意 URL/header 或编码路径绕过模板。

桌面使用独立 hash router 与连接 provider；原 Web transport 保留本机开发使用。未 ready、缺 product capability 或主体不匹配时拒绝业务调用，不能回退 PC HTTP。generation 改变终止旧请求的投递，释放媒体句柄、上传和录音；新账户不能继承前一主体的页面数据或请求结果。

公开 preload 类型见 [desktop-contract.ts](../../frontend/shared/desktop-contract.ts)、[product-contract.ts](../../frontend/shared/product-contract.ts)。版本号要区分：preload `protocolVersion:1`、operation envelope `version:1` 与 Agent proof `v2` 属于不同协议层，不是同一个升级号。

## 3. 身份与网络合同

### 3.1 v2 的 10 条固定 RPC

以下均相对 `/api/mindos/zhijun`；`{id}` 必须是 32 位小写 hex。

| 方法 | 路径 | query / body |
| --- | --- | --- |
| GET | `/context` | 无 query、空 body |
| POST | `/operations` | 清单操作 JSON |
| GET | `/operations/{id}` | 必填且固定顺序 `after=N&waitMs=N` |
| POST | `/operations/{id}/cancel` | 取消 JSON |
| POST | `/uploads` | requestId/fileName/contentType/size |
| GET | `/uploads/{id}` | 无 query、空 body |
| POST | `/uploads/{id}/chunks` | index/data(base64)/sha256 |
| POST | `/uploads/{id}/complete` | 整文件 sha256 |
| DELETE | `/uploads/{id}` | 无 query、空 body |
| GET | `/blobs/{id}` | 必填且固定顺序 `offset=N&limit=N` |

query 不允许重复键、重排、前导零、编码别名或额外字段。`after` 为安全整数；waitMs 0–8000；offset 0–209715200；limit 1–524288。POST Content-Type 必须恰为 `application/json`，body 为有效 UTF-8 JSON 对象，递归重复 key 拒绝；GET/DELETE 必须完全不带 Content-Type 且 body 为空。catalog 的业务 body 嵌在操作对象内，再执行业务 schema 校验。

Agent 每次重验 Direct、当前设备/Owner/grant/session、用途与 scopes，签名域为 `NEXUSAOS-MINDOS-BRIDGE-V2` 加真实 LF。证明绑定主体、applicationId、ownershipEpoch、authVersion、method、原始 URI、bodySha256 和 Content-Type，有效期最多 5 秒。DE 验签、nonce 单用、时间及本地撤销后才建 workspace principal；renderer 无权伪造这些字段。完整机器合同见 [zhijun-workspace-v2.json](contracts/zhijun-workspace-v2.json)。

### 3.2 v1 兼容和凭据区分

旧 `mindos-person-data-pc/person-data.read` 仅保留 v1 context/materials GET，未增写权限。Consumer access token、P2P ticket、Agent proof 和 worker HMAC 各有用途，不可互换。现有专用 Connectivity session exchange 也不是 v2 传输身份替代品。

历史真实登录、空资料页和一次重连见[部署记录](BOX-DEPLOYMENT-0906.md)，缺失的非空资料/跨账号/撤销验收仍须按矩阵补齐；旧 v1 通过不证明 v2 新应用已获授权。

## 4. 流、上传、资源与错误

### 4.1 有界流与取消

start 返回 job ID；poll 返回 `id/state/events/cursor/hasMore`。事件为 headers/chunk/blob/end/error，网络 chunk 用 base64，preload 暴露 Uint8Array，renderer 使用持续 UTF-8 解码器还原原 SSE。每个短 RPC 仍由 SDK 收完整响应。对 JSON 与 SSE 都保留业务 HTTP 状态和受控错误，409/422 不被通用成功包隐藏。

同 workspace/client/requestId 的相同 start 幂等回放；不同内容 409。任务状态为 queued/running/succeeded/failed/cancelled/interrupted。任务重启后标记 interrupted，不自动再次执行。显式取消不承诺业务回滚；写结果不确定时保留 requestId、提示核对，不自动换 ID 重发。新 session 恢复已有结果需显式相同 start，不能重新赋予旧运行任务权限。

### 4.2 文件与隐私

v2 上传状态为 open/complete/cancelled/failed；received 是字节数、nextIndex 从 0 开始。非末块必须 512 KiB，同块同 hash 重复 ACK，乱序/冲突拒绝，完成校验整文件摘要。完成 uploadId 在盒端解密为匿名临时 UploadFile，业务适配器还原 multipart；调用方负责关闭。版本与对话附件继续沿原关联/保护逻辑，不降级为普通新资料。

响应 bytes 写加密 blob，poll 只返回小描述；保存和预览从 blob 分块读取。预览最多 8 MiB，超限使用受控保存。无活跃任务引用的完成上传可释放，DELETE 幂等且保留去重 tombstone。任务/事件/上传/blob/preview-consent-background ledger 按 workspace 使用 AES-GCM，密钥独立目录 0600；TTL 清理只处理 Gateway 自己的目录。

### 4.3 分层预算

| 层 | 当前边界 |
| --- | --- |
| SDK/Core 基线 | request 2 MiB、response 16 MiB；宿主 watchdog 15 秒；Core 单会话 1024 request ID |
| 新应用 Agent | 每请求/响应 1 MiB、8 并发、120 rpm、会话传输 1 GiB |
| Gateway | job 上限 600 秒、事件累计 16 MiB并受catalog更小限制；每页32事件/256 KiB、poll最多8秒 |
| Gateway 调度 | 每workspace运行4/排队8、每盒worker4、Owner租约30秒、heartbeat10秒 |
| 文件/磁盘 | 每文件200 MiB；原始分片512 KiB；workspace存储1 GiB，包含元数据与tombstone |

main现以同一会话调度器管理心跳、poll和上传，预留请求数量及编码后字节。确认未派发的限流拒绝才允许同字节有限重试；已派发写入保持不确定结果。真实JS+严格内存Agent配额、虚拟时钟下200MiB/400块在243秒完成，滚动60秒最多101请求；该值不是实际SDK/P2P吞吐。

base64 开销也消耗传输预算。不能通过自动重连刷新额度；额度不足应返回可识别错误。blob 文件字节不计入任务事件 16 MiB，但计入文件与磁盘额度。后台任务、nonce、对象数量和清理均有独立上限，不能把 1 GiB 解释为无限任务数。

## 5. 部署配置与顺序

环境变量以 DE `backend/mindos/zhijun_gateway/workers.py::GatewayConfig.from_environment` 为准。以下仅列变量名与职责，不提供凭据值或把开发路径当生产默认值。

| 变量 | 要求与作用 |
| --- | --- |
| `ZHIJUN_GATEWAY_CODE_ROOT` | 必填绝对路径，指向包含 `zhijun_worker/__main__.py` 的知君 backend 发布目录 |
| `ZHIJUN_GATEWAY_CATALOG` | 必填绝对路径，固定版本共享 product-operations.json；main/DE/worker使用一致清单 |
| `ZHIJUN_GATEWAY_STATE_ROOT` | 必填，Gateway临时任务/加密事件/上传/blob/ledger目录 |
| `ZHIJUN_GATEWAY_SECRETS_ROOT` | 必填，独立workspace加密密钥目录，不能与state混放 |
| `ZHIJUN_GATEWAY_DOMAIN_ROOT` | 必填，持久知君领域目录；每workspace子目录，不在DE个人记忆退役清理范围 |
| `ZHIJUN_GATEWAY_RUNTIME_ROOT` | 必填，短路径运行目录，容纳临时UDS/key/subject；完整socket路径≤100字节 |
| `ZHIJUN_GATEWAY_CAPABILITY_URL` | 默认 `http://127.0.0.1:8618`；仅允许HTTP loopback origin，无凭据/query/fragment及业务path |

上述路径拒绝 symlink；state/secrets/domain/runtime 四根不得重叠或互为祖先，目录0700且归服务用户。code/catalog 是部署输入，不是可由客户端指定的路径。Gateway 子进程使用 DE 当前 Python 解释器，发布环境必须包含知君领域实际依赖。

Gateway 自动为 worker 注入 `ZHIJUN_WORKSPACE_ID/SUBJECT_FILE/KEY_FILE/SOCKET`、`ZHIJUN_CAPABILITY_URL`、`ZHIJUN_PRODUCT_CATALOG` 和隔离的 `CENTAUR*` 路径。worker 只继承少量基础系统变量，不继承模型 Key、DE 数据路径、PYTHONPATH 或 debug。生产保持 `MINDOS_LOCAL_WEB_DEBUG_ACCESS=0`，既有 Agent/DE Ed25519 密钥配置沿 [D03 合同](BUSINESS-BRIDGE-0906.md)，不得写入仓库、截图或日志。

发布顺序与责任：

1. 固定知君、SDK sidecar、Agent、DE、Admin、catalog 的版本与 SHA，检查运行版本漂移并准备代码/配置回退；数据升级另做一致性备份。
2. Admin 维护方通过真实发布路径登记新 application/purpose 的 Connectivity target/policy；不能加入 legacy TARGETS 或放宽 read app。**该生产发布路径目前未提供，属于正式授权联调前置。**
3. 部署支持 v2/pathTemplate 的 Agent 二进制，再装载仅新增新应用的 manifest；旧应用段不改。本次已完成，实际manifest保留原2个应用并仅新增zhijun-desktop。
4. 部署 DE Gateway/capabilities 与固定版本知君 worker/catalog，配置独立数据/密钥/运行根；检查 UDS、进程锁、能力认证和空库启动。本次已按clean heads匹配部署，live8618健康200、未签名v2 context401；来源强制enforce、debug=0。此步骤与此前FD单文件热修分开记录。
5. 桌面选择新应用并通过同一 SDK 通道 context 主体/capability 校验，才开放完整产品。运行真实逐功能与负向验收后记录部署 SHA，再交付安装包。

Admin登记是正式授权联调前置，不阻止先部署受保护盒端组件；本次已完成第3、4步，第2、5步仍待完成。任一步未就绪应 fail closed；不能临时开 local-debug、透传任意 header/path 或退回旧 app。回退旧代码前须确认新 schema/写入兼容；不以旧备份静默覆盖升级后用户数据。

## 6. 验证与剩余工作

当前各模块已有独立本地测试与构建证据，不能简单相加成一次全链路验收。后续必须以真实账号逐项核对：原15页业务动作、非空资料/版本/受保护附件、超过60秒聊天/取消/UTF-8分界、200MiB上传与预览/保存、真实模型授权、麦克风权限/转写、来源删除/恢复、跨client/session/账号/device、撤销、重启和预算失败。

永久删除只用明确隔离的验收数据，不能拿用户现有资料做破坏性测试。新领域空库不代表历史 global 已迁移。安装发布还需平台范围、签名/公证/权限声明与实际产物验收；BLE 是独立可选功能，不作为原产品已有业务上线的隐含前置。

[FD 热修报告](../reports/CONNECTIVITY-FD-HOTFIX-0906.md)记录的是独立线上修复：connectivity store 连接确定关闭、211项必要回归及部署后FD/HTTP指标；不得归入 v2 完整产品已验收。

### 6.1 已执行的硬件与后续问题

PDF/DOCX/OCR盒端解析分别46/48/110字符通过；受限voice API返回40字符、0资料、2个指定短语均匹配。测试语音为合成WAV，实际麦克风/正式UI未测。内联snapshot SHA、DeletionStore连接释放及有界纠错修复后，最新资料相关范围189项通过；hardware-candidate5为5/5，safe material正文82、摘要43字符、实体2、关系0。gateway-candidate6为10/10、60请求/21个completed操作，知识CRUD/confirm/search/purge通过。以上仅为隔离真盒合成主体/输入，非正式Consumer/UI；首次失败与修复过程保留在审核报告。

shell最终117项Node、vue-tsc通过，独立15项配额验证包含在117内，既有4项隔离Electron E2E单独记录。生产Admin入口仍缺，完整v2正式SDK/P2P和UI验收pending。详见[审核报告](../reports/FULL-PRODUCT-GATEWAY-REVIEW-0906.md)与[硬件报告](../reports/FULL-PRODUCT-HARDWARE-0906.md)。

正式盒端匹配部署的版本、备份、文件哈希、健康与拒绝检查见[部署回执](../reports/FULL-PRODUCT-DEPLOYMENT-0906.md)；Admin发布及正式Consumer/SDK/P2P/UI验收保持独立待办。
