# 知君产品架构

本文是现行架构入口，按 2026-09-14 的代码及已核验部署边界更新，覆盖知君桌面及其依赖的盒端、连接和云端服务。代码中的功能恢复不表示旧安装包已更新；开发模式、历史兼容代码和待建设的 OTA 纳管能力不等于已交付能力。

产品定位见 [产品定义](../product/PRODUCT.md)。机器地址、容器 ID、安装包版本及摘要属于发布记录，不是架构常量。

## 1. 架构结论

知君由桌面交互、盒端业务后端和受控基础服务组成，不是一个只调用云端模型的桌面壳。

- **知君 worker 是运行在盒子上的业务后端**：负责对话、个人理解、判断、检索编排、材料审阅、来源授权和回执。它不是大模型推理进程。
- **Remote Agent 是盒端连接与转发组件**：负责经过认证的设备会话、应用路由约束和本地转发，不执行知君领域业务。
- **知君 Gateway 是 Data Engine 侧的工作区网关**：负责工作区身份、lease、操作分发和 worker 生命周期，与 Remote Agent 不同。
- **Remote Gateway 是服务端连接网关**：知君当前主要用它交换信令；它与盒内知君 Gateway 不是同一个网关，也不是最终回答模型。
- **Data Engine / Data Agent 提供资料和基础能力**：负责材料接收、索引、敏感预识别、授权检索、证据交付，以及当前盒端模型能力服务；最终回答由知君组织。
- **Admin / Consumer 是账号与授权控制面**。Admin 上线、连接成功、工作区可用、模型可用与允许外发资料是不同状态，不能相互替代。

## 2. 组件及逻辑拓扑

| 组件 | 运行位置与主要职责 | 不负责什么 |
|---|---|---|
| Vue Renderer | 用户电脑；今日来信、对话、本体、判断、资料边界和设置；展示流式结果与确认界面 | 不直连 Admin、Data Agent 或模型，不保管长期凭据 |
| Electron preload / main | 用户电脑；窄 IPC、操作校验、账号、安全存储、连接与工作区生命周期、原生权限 | 正式桌面不启动本机 Python 业务后端或推理服务 |
| Connectivity / Consumer SDK、原生 sidecar | 用户电脑；连接票据、Direct/合法 TURN 回退、业务传输 | 不组织 Query，不决定材料是否可进入回答 |
| Admin / Consumer | 服务端；账号、设备绑定、应用登记、授权、短期连接票据 | 不是聊天正文代理，登录不等于取得所有盒端资料权限 |
| Remote Gateway / STUN / TURN | 连接基础设施；分别承担信令、地址发现、必要的中继 | 三者不是同一种数据通道；TURN 不是云端模型 |
| Remote Agent | 盒子；P2P 端点、会话校验、应用/profile/路由白名单、本地转发 | 不执行知君对话、不管理个人理解、不替代业务网关鉴权 |
| 知君 Gateway | 盒端 Data Engine 服务内；工作区授权、受控操作分发、worker 管理 | 不提供任意 HTTP、Shell 或文件系统代理 |
| 知君 worker | 盒端独立 Python 进程；知君领域逻辑、领域数据、检索与生成编排 | 不实现 Data Engine 的解析、向量索引和敏感检测算法，不自行启动模型 |
| Data Agent RAG V2 | 盒端 Data Engine 的 App REST 接口；授权检索、敏感交付、证据复核、可选规则管理 | 不接收完整知君对话历史，不生成最终对话答案 |
| DE Capability / 模型出口 | 盒端；已认证的模型描述、流式/结构化生成、实际请求的出口校验 | 不是 RAG REST 的替代接口，不允许绕过材料授权 |
| CentaurOS 推理运行时 | 盒端；模型加载、硬件适配、推理服务与 NPU-only 执行约束 | 不由桌面版本、CSS 或模型名称决定实际硬件 |
| 在线模型供应商或用户配置的中转服务 | 外部服务；接收本轮获准内容并生成回答 | 不等同于 Admin、Remote Gateway 或 TURN，不因配置成功就取得资料授权 |

```text
用户电脑                                      盒子
Vue Renderer
  ↕ preload 窄 IPC
Electron main
  ├── HTTPS → Admin / Consumer：账号、设备、应用、连接票据
  └── Connectivity SDK + 原生 sidecar
        ↕ WSS → Remote Gateway ← mTLS WSS ← Remote Agent（出站信令连接）
        ↕ DataChannel：Direct，或经 TURN 转发加密包 ↔ Remote Agent
                                                  ↕ 允许的 loopback 路由
                                           知君 Gateway（DE 内）
                                                  ↕ 私有 UDS / 签名请求
                                           知君 workspace worker
                                             ├── 知君领域数据库
                                             ├── App REST → Data Agent RAG V2
                                             │               ├── SQLite：材料状态、权限、检测、证据
                                             │               ├── ChromaDB + BM25：授权材料检索
                                             │               └── 文件存储：原件、大正文快照
                                             └── 签名能力调用 → DE 模型能力 / 出口校验
                                                                  ├── 本地推理运行时
                                                                  └── 获准的在线模型
```

这是逻辑调用图，不是“一框一个容器”。当前集成形态中，Gateway 与 Data Engine 在同一服务中，worker 是其管理的独立子进程；知君代码可作为只读目录挂载进承载容器。因此，只更新 worker 不一定要重建 Data Engine 镜像，但可能需要重建/重启承载 Gateway 的容器，让新工作区进程加载新代码。

### 2.1 Data Engine 内部：材料准备与在线检索

下面展开相邻 Data Engine 当前源码的接线，不是某台盒子的运行进程清单。后台扫描、重排等可选能力是否生效还需核验部署开关和模型；不把设计文档中的规划节点当成现行服务。

```text
材料准备面（Data Engine 执行；知君提供资料管理界面）
知君“资料与边界” → 已认证 Gateway materials 操作 / DE Web / 内部受控接收
  → Upload Runtime → 原件文件存储 + 材料登记 / App 归属
  → MaterialWorker：解析、提取
      → SQLite：任务、版本、快照元数据、结构化解析片段
      → 快照正文：小正文在 SQLite，大正文在受控文件目录
  → MaterialIndexingService：结构化分块 → 本地 Embedding
      → ChromaDB：文本块 + 向量 + 版本元数据（增量写 delta）
      → SQLite：有效代、索引路由、材料发布状态
      → 使进程内 BM25 索引失效，按当前有效文本块重建
  └─ [按配置启用] SensitiveScanWorker：后台敏感扫描 / 重扫
      → SQLite：检测版本、扫描任务、覆盖状态、检测事实
      → 满足对应版本与覆盖条件后发布可交付状态

检索交付面（知君每轮调用；不是每轮重新解析原件）
知君 worker：固定 search_materials 工具 / Query 规划
  → Data Agent RAG V2 App REST
      → App 身份、能力与限流检查
      → MaterialRetrievalRepository：授权范围、当前材料版本 / 就绪状态
          ├── Query Embedding → ChromaDB base + delta 授权范围内向量召回
          └── 进程内 BM25：授权来源范围内词面补充召回
      → 版本 / 快照 / hash 校验 → 相关性准入 → 可选本地 CrossEncoder 重排
      → SensitiveDeliveryGuardService：规则、检测与交付决策
          ├── 按请求混合检测：确定性规则 + 配置的本地语义检测
          └── [按配置替代上支] 读取持久预识别事实及其有效性 / 覆盖状态
      → Search 结果；需要时经敏感 Confirm 才交付片段
  → 知君：用户审阅材料并选择本轮子集
  → Evidence Resolve：重读索引块，复核权限、版本、策略及证据绑定
  → 知君：有界材料上下文 + 独立在线外发许可 → DE 模型能力 → 最终回答
```

原件、解析快照和 Chroma 文本块是三个不同层次。Search、Confirm 与 Evidence Resolve 的材料正文来自受控索引块，不是知君读取原件，也不是每轮重新解析文件。向量召回先把已授权来源条件下推到 Chroma，再复核版本与交付条件；不是全库 Top-K 后才做权限过滤。BM25 补充向量召回，相关性准入后才可重排，重排不会扩大 App 权限或绕过敏感交付。

ChromaDB 使用本机 `PersistentClient`，base/delta 是索引代际与增量组织方式，不是两个独立数据库服务。BM25 使用 `rank_bm25.BM25Okapi` 从有效文本块构建进程内词面索引；此链路没有独立 Elasticsearch、SQLite FTS、Redis 或消息队列依赖。查询向量及检测/策略决策另有有界内存缓存，缓存不能替代权限、版本和证据复核。

### 2.2 智能体、后台 worker 与模型的职责

“Agent”不是把所有处理节点都包装成自主智能体。当前产品中应区分下列组件：

| 组件 | 实际实现与职责 | 与知君的关系 |
|---|---|---|
| Data Agent RAG V2 | `DataAgentRagV2Runtime` 加 App REST；组织授权检索、交付与证据检查 | 知君的材料检索接口；不规划知君对话，不生成最终答案 |
| 知君工具与对话编排 | 固定 `search_materials`、确定性 Query 规划、材料审阅和轮次状态机 | 属于知君 worker，不是 DE 内的通用 Tools Agent 或自主多 Agent 系统 |
| MaterialWorker / MaterialIndexingService | 消费材料任务，执行解析、快照、分块、向量化与索引发布 | Data Engine 后台工作流；不是知君业务 worker，也不是回答模型 |
| SensitiveScanWorker / 交付 Guard | 前者按开关后台扫描；后者在请求边界执行检测/策略/确认检查 | 数据交付安全组件，不是可自行批准资料使用的“敏感 Agent” |
| Embedding / BM25 / CrossEncoder | 分别负责向量表示、词面召回、可选本地相关性重排 | 检索服务和算法；不生成最终答案 |
| DE 模型能力 / 推理运行时 | 向知君提供受控流式生成与结构化生成；管理实际模型请求 | 与检索算法模型、敏感语义检测区分，最终回答仍由知君组织 |
| 知识卡、Claim / Projection 等治理组件 | DE 另有 Web/治理接线及按开关启动的后台任务 | 不在当前知君 App RAG V2 的必经链中；不能画成每次对话都执行 |
| 旧 Agent 内容接口 / MCP | 旧 Agent 内容路由返回 `LEGACY_AGENT_API_RETIRED`；MCP 仅健康/迁移提示，历史内容调用已退役 | 不是当前知君 RAG 的接口或旁路；不接入旧 `answer` 生成链 |

当前接线没有通用 AgentRunner、Planner Agent 或多 Agent 自主协作运行时的实现依据。将来引入模型工具选择，需要单独定义工具权限、调用预算、人工确认和审计，不以本图宣称已经实现。

## 3. 桌面、连接与工作区建立

### 3.1 正式桌面与开发模式

正式桌面从 `zhijun://desktop/desktop.html` 加载随包交付的页面，经共享合同、preload 和 main 的有限操作目录访问盒子；Renderer 不能指定任意 URL、身份头、长期 token 或文件路径。main 校验原生 sidecar 摘要，通过私有 stdin/stdout JSONL 与其通信。

登录密码由表单经窄接口提交到 main；不能把“不保管密码”写成登录表单从不接触输入。持久化登录凭据由 main 使用系统安全存储保护，连接票据及可复用业务授权不返回页面。原生录音、保存与配网各有独立权限边界；缺少受信配网配置时失败关闭，不自动降级明文协议。

本机 Web 开发使用本机 FastAPI/Vite。仓库保留的旧资料页面、v1 bridge 和模拟适配器不代表正式桌面仍有相同权限。`frontend/mobile-native/` 不在本文核验的 Electron 发布链内，不因目录存在就宣称已交付同等能力的移动端。

### 3.2 Direct、TURN 与控制面

知君以 `applicationId=zhijun-desktop`、`purpose=zhijun.workspace` 和 `remote.p2p` 申请受限连接。main 仅使用面向 Consumer 的窄接口，不绕过它访问旧管理面。

当前流程先使用 `SOVEREIGN_DIRECT_ONLY / DIRECT_ONLY`；只有错误码为 `SDK_DIRECT_UNAVAILABLE`，并且 `detailCode` 为 `DIRECT_TIMEOUT` 或 `ICE_FAILED`，并且带有合法 `failedSessionId` 时，才自动申请绑定该失败会话的独立 `REMOTEOPS_COMPATIBILITY / TURN_ONLY` 票据。不能把第一个 profile 名称写成“整个产品永不使用中继”，也不能描述为同一张 Direct 票据静默转中继。

Direct 的业务包在桌面 sidecar 与 Remote Agent 的 WebRTC DataChannel 之间传递；Remote Gateway 负责信令，不承载该 Direct 业务正文。同一局域网可以选择 host candidate，但**仍不绕过 Remote Agent、Admin 出票或信令链路**，当前没有桌面直拨业务 IP:端口的旁路。

合法回退使用 TURN 转发 WebRTC 加密包，不是 Remote Gateway 明文 HTTP relay。桌面到 Consumer 使用 HTTPS，信令使用 WSS，Agent 到 Remote Gateway 使用设备证书 mTLS；桌面 sidecar 与 Remote Agent 间 DataChannel 使用 DTLS/SCTP，Agent 到盒内业务服务则是受限 loopback HTTP。不能笼统写成“每一跳都是 TLS”或“中继时服务器获得明文”。

- **Direct / Relay**：电脑如何连接盒子。
- **本地 / 在线模型**：本轮由哪个模型服务生成。
- P2P 直连不表示在线模型内容未外发；TURN 也不代表对话自动改用云模型。

连接基础设施仍会处理必要的身份、信令和网络元数据，不能宣称完全不依赖服务器。也不能为了速度开放内部业务端口、关闭防火墙或跳过授权。内部 HTTP 仅监听 loopback 时，电脑直接访问盒子内网 IP:业务端口被拒绝是预期边界，不足以认定服务故障。

### 3.3 工作区与重连

SDK 建连后，还要通过同一受控会话取得 v2 workspace context，校验账号、client、设备、应用、workspace、ownership epoch 和短期有效期；仅登录、网络可达或 v1 只读授权都不足以开放完整产品。

账号会话、短期连接票据、workspace lease、材料授权分别失效。恢复连接保留仍有效的账号状态，但重新出票并核验工作区；不能延长旧 lease、靠保存密码掩盖账号失效或自动重放写入。切换账号、设备或连接代次后，迟到结果不得投递到新页面。

重连有界；连接恢复后可以刷新只读页面，发送消息、修改、删除等结果未知的写入不自动补发。详情见 [连接会话恢复](connection-session-recovery.md)。

连接交互按真实 `connecting → authorizing → ready` 展示建立通道、验证访问权限和打开工作区；长等待显示观察耗时与取消入口，不虚构完成百分比或中继阶段。只有真实路径和工作区就绪后才展示相应安全连接状态，模拟连接不充当安全验收；这一层改善等待反馈，不改变打洞、鉴权预算或资料外发边界。

## 4. worker、数据与进程隔离

`backend/zhijun_worker/` 是盒端入口和受控适配层，业务实现仍主要位于 `backend/mindos/`、`backend/mindos/zhijun/` 和 `backend/mindos/stores/`。不能把“后端”仅理解为 worker 目录内几个文件。

DE 的 `GatewayManager` 按 workspace 管理、按需复用 worker，不是每个页面或会话新建一个进程。`workers.py` 在验证主体后用同一 Python 解释器启动 `-m zhijun_worker`，设置独立持久领域目录、临时 socket/key 和主体文件；worker Uvicorn 仅监听 Unix socket，Gateway 经 `/v1/dispatch` 和受控 operation 分发领域请求。

工作区绑定账号、设备和 ownership epoch；worker 进程绑定后不能任意切换主体。换一台电脑不自动创建另一套个人理解，但该电脑的新 client/session 必须重新鉴权；临时任务和资源仍可另绑定客户端会话。

子进程环境不继承 DE canonical 数据路径、模型 API key 或开发标志；启动失败清理临时 socket/key，不删除持久数据。**当前是可信代码下的进程、目录和协议隔离，不是每个 workspace 一个 UID、容器或虚拟机，也不是抵御恶意 worker 的 OS 强沙箱。**

| 数据 | 权威归属及边界 |
|---|---|
| 会话、消息、轮次回执和业务路由记录 | 知君 workspace 领域存储 |
| 工作理解、已确认理解、来源关系、章程、判断、事项、成果与复盘 | 知君对应领域存储；派生理解不是第二事实源 |
| 原始材料、解析、索引、持久化敏感检测事实 | Data Engine；知君不读其数据目录或绕过交付接口读取全文 |
| 检索结果、确认令牌与用户本轮选择 | worker 内短期状态；临时证据不是永久可读授权，令牌不进入历史或日志 |
| 长期 RAG App 凭据 | 独立 `secrets_root/rag-v2/<workspaceId>`，与业务数据和源码分离；DE 应用存储保存 Secret 哈希 |
| workspace 模型配置与云端 API key | DE 的工作区模型存储及加密 Secret Store；不交给 worker 或 Renderer 持有 |
| workspace 签名 key、subject、UDS | Gateway 管理的临时私有运行目录 |
| 桌面账号状态、配置和诊断 | 用户电脑；凭据由 main 安全存储，诊断不记录正文或秘密 |

数据根、Gateway state、secrets 与 runtime root 分开配置且不能相互包含。程序回退、业务数据恢复、账号重置和模型重装是不同操作，不得以“清空一个目录”代替正式流程。

### 4.1 Data Engine 持久化组件

以下路径相对 DE 的 `DATA_ROOT`，实际位置以 `runtime_paths.py` 和部署配置为准；Secret Store 为单独配置的目录。它们不是知君 workspace 领域数据库，知君只能经接口访问获准内容。

| 存储组件 | 当前实现与典型位置 | 保存内容与边界 |
|---|---|---|
| 材料登记与处理数据库 | SQLite：`db/job_store.db`、`db/material_pipeline.db`、`db/derived_content.db` | 材料归属、任务、版本/快照、索引发布状态、结构化解析片段；小快照正文可内联 SQLite |
| 原件与大正文快照 | 受控 `application_originals` 原件目录；`derived_content/materials/` 大正文目录 | 与数据库中的路径、版本和 hash 关联；原件不是可随意重建的检索缓存 |
| 向量与文本块存储 | 持久 ChromaDB；`chroma_data/`、`indexes/generations/`；材料 collection 为 `documents` | 向量、可检索块正文及版本元数据；读取有效 base/delta，不能把向量库当成只有不可读数值 |
| 索引代际登记 | SQLite：`db/index_registry.db`、`db/generation_registry.db` | 物理索引路由、来源有效代、tombstone 与读取栅栏；索引目录存在不代表其已发布可读 |
| App 与检索权限 | SQLite：`db/agent_gateway.db`、`db/agent_access.db`，结合材料归属和策略检查 | 前者保存 App 身份与 Secret 哈希，后者提供主体范围、材料授权及权限修订；不是知君聊天记录库 |
| 敏感规则与检测事实 | SQLite：`db/sensitive_delivery_policy.db`、`db/sensitive_detection.db` | 规则修订、检测版本、扫描任务、覆盖及检测事实；持久事实不是可随意清掉的性能缓存 |
| 确认与证据数据库 | SQLite：`db/sensitive_confirmation.db`、`db/sensitive_evidence.db` | 确认令牌哈希、幂等回执、绑定描述符和证据状态；不持久保存检索正文或明文 Confirm Token |
| 模型配置与凭据 | DE workspace 模型存储、运行配置与独立加密 Secret Store | 模型选择、配置、secret 引用及受保护的 API key；不放进向量索引或用户对话 |
| 其他 DE 治理数据 | 例如 `db/card_ledger.db`、`db/claim_store.db`、`db/projection_registry.db` | 服务其他知识治理能力；不能据文件名推断已启用，或并入知君检索必经链 |
| 非持久检索缓存 | 进程内 BM25、Query 向量 LRU、检测/决策 LRU | 可失效重建；不单独部署 Redis，不作为授权事实源 |

材料任务与快照登记在同一材料 SQLite 库内可事务提交；大正文文件和 Chroma 则依赖快照恢复、索引代际及发布检查协同，不是跨所有存储的一次全局 ACID 事务。DE 启动时对数据根加独占锁，不应让多个后端实例并发使用同一持久 Chroma/数据根。

备份应覆盖关联的数据库、原件/快照、索引及对应受控凭据，并取得一致性；不能在线随意复制单个 `.db` 就宣称完整恢复。索引和内存缓存可在条件满足时重建，但须保留版本、权限及发布边界；持久业务、撤权与检测事实不能当作缓存清空。Secret Store 加密不表示全部 SQLite、原件和 Chroma 已统一静态加密。

## 5. 对话与 Data Agent 检索

### 5.1 REST 薄适配，而非模型自主 Tools Agent

对接依据是 Data Engine 团队的 [知君调用 Data-Agent-RAG-V2 接口指南](../../../nexusaos-data-engine/docs/development/contracts/知君调用Data-Agent-RAG-V2接口指南.md)。此跨仓库链接要求两个仓库同级；单独检出知君时，应取得对方同版合同。

知君用 `retrieval_tools.py` 的固定 `search_materials` 工具封装和自身状态机调用 `/v1/agent/apps/*` REST。当前 `native_model_tools_supported=false`，不要求 OpenAI Function Calling、MCP、`role=tool` 或新的同义检索接口。工具名是内部代码组织方式，不表示模型可以自主执行任意工具。

现有 Query 规划是有界、确定性的规则改写：仅使用已获准的相关用户历史消解指代，保留当前问题；主题不明确时要求补充，不借助手推测发明事实。普通闲聊不强制搜索整个资料库；显式资料问题、相关个人事实问题、已有材料依据的续问等由上下文规划判断是否检索。

Search 只接收独立 Query、检索类型、Top-K、实际 Search 的 `interactionId` 和可选材料范围；不传完整 messages、身份、推理过程或工具声明。默认 Top-K 为 5，不限范围时检索当前 App 获准且就绪的材料；指定 `materialIds` 只能缩小范围，不能扩大权限。

### 5.2 一轮材料问答

```text
用户提交当前问题
  → 读取权威模型路由，准备获准历史与个人上下文
  → 需要材料时组织独立 Query，调用 Search
  → 按 status + detectionNotice 处理敏感交付、暂扣或无结果
  → 用户审阅已允许交付的片段，选择本轮实际使用的子集
  → Evidence Resolve 复核，组装有界上下文
  → 在线模式另行核对外发来源、服务、用途和实际请求
  → 提交轮次，worker 经 DE 模型能力取得流式回答
  → 展示并保存实际模型、来源、引用及终态回执
```

三个决定不能合并：

1. **敏感交付确认**：Data Agent 是否允许把脱敏片段或获准原文交给知君。本地回答模型同样不能绕过这一层。
2. **材料使用确认**：哪些已交付片段用于本轮回答。普通结果也须审阅，默认不选；可选子集、不使用材料继续或取消。
3. **在线外发确认**：所选内容是否可发送到当前外部模型服务。材料已经交付、已经勾选或模型配置成功，都不等于取得外发许可。

只把允许交付、用户选中且最终证据检查通过的片段作为材料上下文；暂扣原文、未选片段、App Secret、Confirm Token 和 riskToken 不进入回答模型。上下文预算及去重仍可能减少实际采用的片段，回执以真实输入为准，不把勾选数当作已发送数。

### 5.3 状态、重试与证据

- HTTP 200 不代表全部材料可用。分别处理 `ok`、`no_results`、`sensitive_content_blocked`、`sensitive_confirmation_required`、`sensitive_check_unavailable`，并检查可选 `detectionNotice`；暂扣不等于无结果。
- 普通 Confirm 同一请求重试复用幂等键；重新 Search 或改变脱敏选择使用新键。稳定轮次/审阅句柄与实际 Search ID 分离，每次新 Search 换 ID，旧界面确认必须拒绝。
- 风险领取须有服务端风险令牌及用户明确确认，结果仍为 `verificationStatus=unverified`。一次性领取响应丢失后不能再次消费原令牌；知君返回 `RAG_RISK_RESULT_UNKNOWN` 并要求重新检索，不自动把风险授权扩大到在线模型。
- 实际使用边界通过 Evidence Resolve 复核证据、版本与策略；撤回、过期或变化后停止使用，不回退读取原件。同一边界可以批量去重，但跨轮次缓存不能跳过新的检查。
- 当前审阅状态仅短期保存在 worker；重启或过期后重新检索、选择。永久消息历史不等于永久证据授权。
- RAG 是 JSON REST，不增加模型 SSE 事件；等待检索或人工确认时还没有开始最终回答流，不能直接归因于模型首 token 慢。

### 5.4 材料维护与可选规则管理

知君区分**资料管理面**与**对话检索面**，不能把 RAG 接口的检索专用合同理解为删除产品的资料管理功能。

- “资料与边界”提供原材料导入、文件夹与列表、上传进度和处理状态、文件详情、知识卡片、回收站及资料搜索入口。用户发起的操作经桌面窄 IPC、Remote Agent、工作区 Gateway 与 `product-operations.json` 中的 `materials` 白名单分发到 Data Engine，保留工作区身份、资源范围和变更校验；不开放任意 HTTP 代理。
- 对话检索仍由 worker 使用 Data Agent RAG V2 App REST。导入、预览、下载原件或管理知识卡片均不等于授权模型使用；进入回答前仍须检索、用户选材、敏感交付及外发授权检查，不能以管理接口读取结果绕过这些步骤。
- 原材料管理页面恢复不同时恢复对话输入框的旧自动附件读取链路，也不改变历史记录或自动重跑旧导入批次。敏感扫描启动、重试和历史回填仍属于 Data Engine 管理端。
- 原材料两页的信息结构参考 Data Engine 普通 Web（不是 Pc 页面）：列表按 50 条分页，提供筛选、上传百分比、后台无闪屏刷新及敏感识别状态；详情按概览、受控原文件预览、解析正文、人工标签、相关内容和版本组织，知识卡片保留为知君的独立辅助区域。`sensitiveScan` 为可选投影，识别完成不代表已脱敏或获得外发授权；不复制 Web 默认 App 配置与未开放的敏感重试接口。
- 分页发行依赖：`get_api_mindos_materials` 的 query 白名单新增 `limit`、`offset`，客户端和盒端 Gateway 使用的 `frontend/shared/product-operations.json` 必须同步部署；旧清单会拒绝分页请求。Data Engine 已有材料管理分发支持这两个参数，本次不修改 Data Engine 源码或新增 App 权限。安装包构建不代表盒端清单已经更新。
- 新版 DE 已退休的旧摘要、智能分析、派生重生成和逐材料隐私复核接口不随页面恢复重新启用。桌面详情保留正文与原件、草稿知识卡片和版本管理，明确说明旧派生功能由 Data Engine 管理；不能在打开详情时继续调用退休接口或轮询不存在的摘要。

解析、索引、敏感预识别及后台重扫属于 Data Engine。启用持久预识别交付时，Search 读取已持久化检测事实并检查覆盖状态；未启用时，当前运行时仍可走按请求的混合检测，因此不能承诺所有部署的检索都不等待检测模型。源码中 `MINDOS_SENSITIVE_PRECOMPUTE_SCAN_ENABLED` 与 `MINDOS_PRECOMPUTED_SENSITIVE_DELIVERY_ENABLED` 默认均关闭，后者要求前者开启；源码默认值不是某台盒子的实际配置。此分支由 DE 团队维护，知君只消费统一的状态、提示和交付结果。

`/api/mindos/rag-v2/*` 浏览器适配层和 `/api/mindos/zhijun/*` 工作区控制协议均不能充当第二套 RAG 数据面。

“偏好 → 知君敏感规则”是可选管理能力，经可信 worker 使用同一长期 App 调用规则接口。内置规则只读，自定义规则容量、revision 和检测预算以服务端为准；更新/删除使用精确版本并发控制，相似规则须另行确认。

规则保存可见不表示新识别语义已覆盖全部材料；低频查询展示 `active` / `applying`，应用中继续使用旧 active 规则，不阻塞 Search。知君不实现检测队列、规则发布/回滚或内部扫描管理器。

## 6. 模型与流式回答

worker 的 `CapabilityProvider` 通过已认证的 DE `model.describe`、`model.stream`、`model.complete_json` 使用模型。它不是从桌面或 worker 任意直连模型 URL：正式流经反向能力接口，由 DE 的模型 transport 构造凭据并发起实际本地/在线请求。RAG REST 则直接使用 App 凭据，不经过这条反向模型能力通道。

在线调用同时经过知君来源授权和 DE 的出口许可/实际请求校验；服务、模型配置、来源、用途或请求变化后必须复核。两层不是任选其一，敏感交付确认也不能替代它们。

上游流经 DE 能力协议、worker 轮次协议及桌面 transport/SSE 消费。中转站有调用记录只说明上游收到请求，不证明正文、合法终态和回执到达知君。worker 区分 text/usage/done/error；缺少结束或中途失败不能伪装为完整成功，已经收到的部分内容与错误应按轮次协议保留。

本地/在线由用户明确选择，在线失败不自动回落本地。实际去向以消息 `provider`、`model`、`external` 与路由审计为准，不看模型自称。详见 [模型路由](task-routing.md)。

NPU-only 是盒端**本地模型推理执行约束**，由 CentaurOS 的运行时、硬件适配与部署配置落实；不意味着 Electron、Python 编排或数据库都不能使用 CPU。Ollama-compatible 等协议名称不能证明硬件路径；不能因为 worker 更新就宣称 NPU 路径已重新验收。

还须分别核验检索模型：当前相邻 DE 的 `embedder.py` 设备选择分支为 CUDA/CPU，不能据最终回答模型使用 NPU 就把 Embedding、CrossEncoder 或敏感检测也标为 NPU。若部署要求所有本地模型推理均为 NPU-only，这些链路还需由 DE/运行时团队适配并单独验收；本图不把它们画成已满足该要求。

## 7. 安全与信任边界

| 边界 | 检查与凭据 | 不可替代的约束 |
|---|---|---|
| Renderer → main | 来源限制、窄 IPC、operation catalog、参数校验 | 不开放任意网络、Shell 或文件路径 |
| 桌面 → Remote Agent | Consumer 授权、短期票据、应用/profile 与连接协议 | 登录不等于任意盒子/路由可访问，连接票据不是 App Secret |
| Agent → 知君 Gateway → worker | Agent bridge proof、workspace/lease、所有权代次、操作白名单、worker 签名和重放检查 | 不由 Renderer 自报可信账号/设备，不复用旧代次 |
| worker → RAG REST | `X-App-Id` / `X-App-Secret`、应用能力、材料及策略版本 | 每次提问不轮换凭据，遇到停用/撤权不自动恢复权限 |
| worker → DE 模型出口 | worker proof、活动 execution/lease、服务与配置、来源、用途、实际请求许可 | 材料交付与使用确认不等于在线外发许可 |

对话检索最小 App 能力为 `mindos.read`、`mindos.search`、`mindos.sensitive.confirm`；规则管理按需增加 `mindos.sensitive.policy.write`，原文领取按部署策略增加 `mindos.sensitive.original.read`。资料管理通过独立的 Gateway `materials` 操作通道及工作区资源鉴权，不要求把 RAG App 的历史 import/upload.status 或版本管理接口重新用作材料管理入口。

这不代表已有 App 已自动缩权：相邻 DE 当前 `rag_v2_provisioning.py` 的历史能力集合仍含 import/upload.status。知君不使用这些能力，实际授予范围须核验；能力治理由 DE 团队按完整集合、修订与现有 App 状态受控变更，本次 worker 升级不会自动扩权、缩权或轮换 Secret。

Gateway 在验证后的工作区激活边界管理长期 App 凭据；业务请求不得遇错就 create/rotate。明文 Secret 文件仅供可信运行账户读取，不进入源码、发行包、业务数据目录、日志或模型上下文。模型 API key 由 DE 的工作区 Secret Store 管理，不从部署级配置自动继承给新工作区。

RAG 客户端允许回环 HTTP，非回环必须 HTTPS；实际 Gateway 能力地址限制在回环。worker 的进程隔离不等于网络/文件系统强沙箱，OS 和容器安全须另行验收。

资料文本是参考数据，不是系统指令或操作授权。用户理解的确认、撤回与来源限制不能被模型输出替代；来源/主体变化后，旧许可、迟到响应或派生记忆不得悄悄回流。

## 8. 发布、部署与升级

### 8.1 独立发布单元

| 变更 | 更新单元 | 不能替代的工作 |
|---|---|---|
| 页面、IPC、连接状态机、SDK/sidecar | 桌面安装包 | 重启盒子不会更新已安装页面 |
| 对话、Query、材料确认、领域逻辑 | 知君 worker 源码及其运行进程 | 重打桌面包不会更新旧 worker |
| RAG REST、知君 Gateway、模型能力与上游流解析 | Data Engine 发行/镜像，由对应团队维护 | 只换 worker 挂载不会更新镜像内 DE 实现 |
| 盒端连接、应用/profile/路由 | Remote Agent 及受控配置 | 不等于更新知君业务或资料索引 |
| 账号、设备绑定、授权、出票 | Admin / Consumer | 不证明桌面和盒端合同配套 |
| 服务端连接信令 | Remote Gateway 独立服务 | 不属于盒端 manager 的容器升级范围 |
| 模型与推理适配配置 | CentaurOS 推理运行时/模型发行 | 不由桌面版本或模型名称代替验收 |

operation catalog、接口合同和依赖必须联合校验。逻辑独立发布不代表任意新旧版本都能混用。

### 8.2 当前事实与 manager / OTA 目标

当前核验的集成盒采用“固定 DE 镜像 + 知君只读源码挂载 + 分开的持久数据/凭据目录”。最近的 worker 更新经 SSH 维护流程完成：核验目标与摘要、隔离冒烟、停止写入并一致性备份、切换运行容器/代码、核验健康与鉴权。**这不是 centauros-manager 升级，也不是正式 OTA。**

`centauros-manager` 是盒端签名发行的检查、应用、状态与受控恢复执行器；Remote Agent 是运行期连接服务，二者不是同一个程序。OTA 涉及发行获取、验签、授权及应用，单次 SSH 执行不能称为 OTA；manager 的 fetch 也不等于已经 apply。

统一纳管知君需要把实际拓扑、worker 产物、固定镜像、catalog、数据/Secret 保留策略、兼容性及健康检查纳入支持的签名发行/profile，再按对应合同执行 `check → apply → status`，失败使用受支持的事务恢复。当前保留业务组件的发行或未纳管该容器的 profile，不会自动更新知君；`retained` 不代表最新或健康。

manager 不是任意配置写入器：当前合同不负责桌面包、Admin/云端 Gateway 发布、裸机重装、驱动/依赖安装、数据库迁移或自动批准 NPU；不能任意改 Compose 的端口、挂载、环境和服务集合。已有复杂集成实例不能直接套通用 Compose，Remote Agent 路由修改也须进入其受支持的签名配置合同，不能把随手改 `/etc` 当正式升级。

参见 CentaurOS 仓库 [盒端管理器说明](../../../nexusaos-centuarai-os/docs/box-manager.md)。未来 manager 纳管属于待完成集成，不把它写成已实现现状。

### 8.3 验收与恢复

分别验证桌面构建/签名/启动、盒端实际代码与配置、接口/鉴权和真实用户业务。macOS Developer ID 签名不等于 Apple 公证；Windows 构建成功不等于真机安装或盒子联调通过。入口见 [桌面说明](../../frontend/shell/README.md) 与 [Windows 发布指南](windows-release.md)。

旧代码和一致性备份保留不等于允许自动恢复数据库。新代码启动可能产生写入或迁移，未知结果先保留现场，评估同版本前向恢复或兼容回退；不得覆盖新业务数据、权限撤销或审计账本，也不自动重放结果未知的模型/写请求。

## 9. 性能与故障定位

| 现象 | 首先核查 | 不能直接得出的结论 |
|---|---|---|
| 登录或设备列表失败 | Consumer 账号、绑定、应用授权 | 不是直接证明 worker 或 NPU 故障 |
| 连接慢、转中继 | 出票、信令、Direct/Relay、Agent、工作区授权阶段 | 总耗时不能全算成打洞或业务处理 |
| 已连接但 ACCESS_DENIED / 合同不符 | 主体、lease、workspace、catalog 与版本 | 不能通过跳过鉴权或扩大路由解决 |
| 本体/判断读取慢 | 桌面读取调度、工作区检查、worker 查询和领域存储 | 不把所有页面加载算成模型推理 |
| 检索等待或空结果 | Query、RAG status/detectionNotice、App 权限、证据状态 | 暂扣不是无结果，人工确认不是模型超时 |
| 云端已调用但空白/半途报错 | 上游流、DE 能力流、worker 终态、连接与前端消费 | 上游计费不能证明端到端成功 |
| 更新后行为未变 | 实际进程路径、镜像、代码挂载、catalog、安装版本 | 仓库源码或一个版本标签不能证明线上更新 |

连接使用脱敏 `connection-timing.jsonl` 与 `connection-report.cjs` 分析；connect 指标可能包含 ticket 耗时，不能重复相加。检索错误保留安全 code、traceId、Retry-After 和结果未知语义；模型以执行回执定位。诊断不记录密码、票据、Secret、Confirm/risk token、敏感原文或完整上下文；不能以减少必要安全检查冒充性能优化。

## 10. 权威实现与文档导航

| 主题 | 本仓库代码入口 |
|---|---|
| 桌面与连接 | `frontend/shell/main.js`、`preload.cjs`、`runtime/desktop-runtime.cjs`、`runtime/product-session.cjs`、`production/adapter.cjs`、`production/sdk-runtime.cjs`（后五项相对 shell） |
| 公开操作 | `frontend/shared/desktop-contract.ts`、`frontend/shared/product-operations.json` |
| worker 适配 | `backend/zhijun_worker/`：app、workspace、auth、capabilities、model、consent |
| 对话、上下文和回执 | `backend/mindos/zhijun/`：routing、turn、context_plan、context_sources；`backend/mindos/stores/` |
| RAG 与审阅 | `backend/zhijun_worker/data_agent_rag_v2.py`、`backend/mindos/data_agent_rag.py`、`backend/mindos/zhijun/retrieval_tools.py` |
| 确认交互与材料入口 | `frontend/mindos-web/src/` 下的 `components/conversation/RagSensitiveDialog.vue`、`services/taskRouting.ts`、`pages/RetrievalMaterialsPage.vue` |
| 规则设置 | `backend/mindos/sensitive_rule_routes.py`；前端 `SensitiveRulesPanel.vue`、`services/sensitiveRuleStatus.ts` |
| 桌面发布 | `frontend/shell/package.json`、`electron-builder*.yml`、`frontend/shell/scripts/` |

外部源码依据：Data Engine 仓库的 `backend/mindos/zhijun_gateway/`（manager、workers、rag_v2_provisioning）、`backend/mindos/zhijun_capabilities/`（models、model_stream、workspace_models_store）及 RAG V2 合同；Admin 仓库的 `remote-gateway/README.md` 与 Consumer 会话服务；CentaurOS 的 Remote Agent/profile、P2P 与 manager 合同。相关代码由各团队维护，不以本仓库副本替代外部权威版本。

Data Engine 内部图的核验入口（均为同级外部仓库）：

| 主题 | 权威源码 |
|---|---|
| 服务启动、后台任务与路由接线 | [server.py](../../../nexusaos-data-engine/backend/server.py) |
| App REST 与 RAG 组合运行时 | [application_search_router.py](../../../nexusaos-data-engine/backend/mindos/agent/application_search_router.py)、[rag_v2_runtime.py](../../../nexusaos-data-engine/backend/mindos/rag_v2_runtime.py) |
| 材料解析、快照与索引发布 | [material_worker.py](../../../nexusaos-data-engine/backend/mindos/material_worker.py)、[material_snapshot_saga.py](../../../nexusaos-data-engine/backend/mindos/material_snapshot_saga.py)、[material_indexing.py](../../../nexusaos-data-engine/backend/mindos/services/material_indexing.py) |
| 授权检索、向量与词面索引 | [material_retrieval.py](../../../nexusaos-data-engine/backend/mindos/services/material_retrieval.py)、[vector_store.py](../../../nexusaos-data-engine/backend/vector_store.py)、[lexical.py](../../../nexusaos-data-engine/backend/lexical.py) |
| 敏感扫描与预识别交付分支 | [sensitive_scan_worker.py](../../../nexusaos-data-engine/backend/mindos/sensitive_scan_worker.py)、[precomputed_sensitive_detection.py](../../../nexusaos-data-engine/backend/mindos/services/precomputed_sensitive_detection.py)、[config.py](../../../nexusaos-data-engine/backend/config.py) |
| 存储路径与检索模型执行 | [runtime_paths.py](../../../nexusaos-data-engine/backend/runtime_paths.py)、[embedder.py](../../../nexusaos-data-engine/backend/embedder.py) |

相关说明：[对话与材料](conversations.md)、[模型路由](task-routing.md)、[记忆与判断](memory-and-decisions.md)、[本机开发](local-runtime.md)、[知君接口](zhijun-api-contract.md)、[Consumer 合同](consumer-api-contract/CONTRACT.md)。历史 v1 bridge、旧资料函数和一次性维护脚本不属于新的正式集成入口。
