# 服务端与盒端领域开发任务

日期：2026-09-06。**SERV-01/02/03 已编码并通过本地验证，状态为本地通过待外部验收；SERV-04–20 未开始。** 三端逐请求 Ed25519 桥以 [D03 v1](../BUSINESS-BRIDGE-0906.md) 为准。OS 全套/race/Linux ARM64 构建及 DE 101 项测试通过；新桥已部署家中盒子，未迁移用户数据，真实 Direct 空页及一次断开重连已验，非空资料及跨主体真机验收未完成。返回 [开发任务总表](../DEVELOPMENT-TASKS-0905.md)。合同依据：[桌面接口](../DESKTOP-CONTRACT-0905.md)、[领域迁移](../DOMAIN-INTEGRATION-0905.md)、[工作包](../INTEGRATION-WORKPACKAGES-0905.md)。

## 使用与派发规则

每项以一个独立 PR 为交付边界；跨仓任务分别提交关联 PR，以集成验证关闭任务。优先级 P0 支持 M0，P1 支持完整业务；M0-R 是正式只读链路，M1 是领域/聊天合流。本文件 M1 服务端子项完成不等于聊天端到端完成，仍依赖 WP-06/WP-08。估算为有效开发加本任务验证：S=0.5–1、M=1–2、L=2–4 人日，不含外部等待和发布排期；发现闭包超出 L 时先拆子任务，不能跳过表族或测试来满足估算。

“硬依赖”表示任务最终验收所需输入；没有正式配置时，允许在明确标注假设的模拟适配器、模块骨架及合成夹具上先行开发，不能将本地通过写成真实链路通过。BASE-01–05 的定义和状态以总表为准。正式身份/桥在 BASE-02/03 冻结；资料归属、应用权限和预算在 BASE-04 冻结；领域归属/Claim/部署决议在 SERV-05 冻结。SERV-01–03 已回填实际源码；其余任务除明确写为“现有”的路径外，目标模块与报告路径均为**拟新增**。

仓库简称：ZJ=当前知君仓库；DE=`nexusaos-data-engine`；OS=`nexusaos-centuarai-os`。表和模块路径仅指源码，禁止读取现有运行库、凭据或用户文件用于开发夹具。所有验证只使用隔离目录、合成身份和合成业务数据；真实 M0 验证另由 WP-05 组织。证据至少记录实际版本、测试文件/命令、预期/实际、失败项和脱敏关联 ID，不能仅引用旧工程测试数量。

## M0 可信链路与资料

<a id="serv-01"></a>

### SERV-01：Agent 可信身份桥适配

**WP-03｜M0-R｜P0｜连接平台，控制面配合｜L（2–4 人日）｜本地通过待外部验收**。硬依赖：BASE-01、BASE-02、BASE-03。

涉及现有 OS `remote-agent/internal/p2p/http_channel.go`、`internal/sessionauth/`、`internal/application/registry.go`、`manifests/remote-agent-applications.yaml`；已新增 `remote-agent/internal/mindosbridge/` 签发器及共享合同向量，并完成 config/control/p2p 接线。PR 独占 Agent 身份映射和知君应用策略，不混入流式/上传扩展。

- [x] 从同一份新鲜授权快照取得 Owner 与当前 grant，逐请求重新校验账号、client、设备、应用、purpose、scope、Direct 状态与会话期限。
- [x] 实现独立 Ed25519 签发器和最长 5 秒的逐请求证明，仅在盒内附加 `X-Nexus-Mindos-Bridge`；外来同名头拒绝，缺密钥配置不赋予新权限。
- [x] 仅为 PC 目标的 GET `/api/mindos/connectivity/context` 与 `/api/mindos/materials` 签发证明；manifest/parser 只补精确 context 路径，不扩大其他应用或业务路径。
- [x] 实现逐请求关闭/过期/撤销拒绝、密钥与授权快照的安全文件读取及共享正负向量；真实部署后的失效传播仍须 WP-05 验收。

验收：正确绑定的合成向量被转交业务桥；错误账号/设备/应用/scope、伪造身份头、越界路径和重放凭据均被拒。正式向量须来自 BASE-03 确认的合同，不能复用 demo 身份作为授权证明。证据：Agent 单元结果、策略差异、与 SERV-02 共用的脱敏向量及版本；真实资料读取留待 WP-05。

<a id="serv-02"></a>

### SERV-02：data-engine 业务 gate 与会话生命周期

**WP-03｜M0-R｜P0｜后端鉴权负责人｜L（2–4 人日）｜本地通过待外部验收**。硬依赖：BASE-01、BASE-02、BASE-03、SERV-01。

涉及现有 DE `backend/mindos/connectivity_ticket.py`、`connectivity_session.py`、`stores/connectivity_store.py`、`device_context.py`、`backend/server.py`；新增 `agent_bridge.py`、`tests/test_agent_bridge.py` 与合成 fixture。PR 独占桥入口、会话验证和统一安全错误；SERV-13 后续接入领域路由。

- [x] 新增独立 Ed25519 公钥验签入口，保留既有专用票据/JWKS 与会话 API 兼容；Consumer 登录 token 不作为业务证明。
- [x] DE在业务处理前验证签名、期限、持久化原子nonce、账号/client/设备、应用、scope及精确请求绑定；purpose与Direct策略由Agent签发前核对。Agent当前grant/ownershipEpoch与DE本地撤销各自按合同校验，不混同两层版本。
- [x] 将已验证主体注入请求上下文；审查逐方法/路径授权落点，缺主体或缺 scope 时拒绝，不退回 global 或 local-debug。
- [x] 不创建可复用业务会话；每次读重新验证最长 5 秒证明，关闭后 Agent 停止签发；安全错误不输出证明正文。真实重启/撤销传播验收保持待办。

验收：关闭 local-debug 后通过桥的资料请求可到达业务处理器；过期、撤销、旧 epoch、重复 nonce、缺 scope、错误绑定均拒绝。现有 `validate_session` 未使用 method/path，必须证明授权在明确的 gate/策略层生效，不能以返回 principal 代替路由授权。证据：隔离 session/gate 测试、合同向量两端对照、会话清理和安全错误结果；health/validate 成功不算业务验收。

<a id="serv-03"></a>

### SERV-03：资料归属及有界响应投影

**WP-03/WP-04｜M0-R｜P0｜后端资料负责人｜L（2–4 人日）｜本地通过待外部验收**。硬依赖：BASE-04、BASE-05、SERV-02。

涉及现有 DE `backend/mindos/uploads.py`、`services/ingestion.py`、`api_contracts.py`；已新增 `backend/mindos/bridge_materials.py` 及 `backend/tests/test_agent_bridge.py` 隔离合同测试。PR 只覆盖 M0 列表；保留既有 Web 合同的兼容方式必须在 BASE-04 中明确。

- [x] 按 accountId + deviceId + ownershipEpoch 生成的独立 scope 同时约束 items、total 和分页；不读取或迁移 global/其他 scope，不把历史资料自动归给首次登录者。
- [x] 落实 limit 1–50、offset 0–10000 及可选 keyword/type/status 的合同；状态完整包含 uploaded/queued/processing/available/failed。
- [x] 受控响应只输出 materialId/fileName/fileType/status/createdAt 和分页元数据，移除顶层 folders、条目 folder/folderId、路径、预览 URL、正文及共享目录统计。
- [x] 在发送前落实 256 KiB 原始 JSON body 预算及字段长度校验；不能先返回无限目录再靠桌面删除。超限给可识别错误，不静默截断。

验收：空页、连续页、queued 筛选和合法极值返回可校验结果；同盒不同账号及两设备按冻结归属规则断言允许/拒绝；增加其他范围资料或目录不改变当前可见 total。巨量共享目录不导致泄漏或无界响应，未知枚举/非法分页被拒。证据：隔离路由测试、真实序列化字节统计、与 WP-04 同一套 DTO 向量；新投影已部署，桌面仍独立对超限响应拒绝。


**本轮本地证据与未验项：** OS 全套 Go 测试、race 及 Linux ARM64 构建通过，DE 101 项通过，包含验签/重放/错误绑定与隔离资料投影。新列表采用只读 SQLite 查询，避免旧列表修复逻辑写入；新 scope 空页不代表历史迁移。家中Agent/DE已部署，debug=0、健康及正式负向检查通过；新版已真实登录，context/空资料页和一次断开重连通过；非空资料、两设备/同盒不同账号、撤销/过期及退出后重新登录的真机证据仍由 DESK-15 补齐。新增盒端隔离55项桥测试及实际server.app 5项检查通过；当前live分支 `c16dc17` 基于 `6b549ad`，本机隔离111项+6个subtests通过，保留新发布权限/分页/错误边界/Pocket改动，详见[盒端部署记录](../BOX-DEPLOYMENT-0906.md)和[D03复现](../BUSINESS-BRIDGE-0906.md#当前-live-分支回归复现)。上述勾选确认实现及局部验证，SERV 正式验收与 M0-R 均未关闭。

## 领域基础、数据和生命周期

<a id="serv-04"></a>

### SERV-04：建立完整依赖闭包与迁移清单

**WP-07｜M1｜P1｜后端架构负责人｜M（1–2 人日）｜未开始**。硬依赖：BASE-01。

涉及现有 ZJ `backend/server.py`、`backend/mindos/zhijun/`、`stores/` 及领域路由；拟新增版本化模块/API/schema/worker 清单。此 PR 只交付源码审计及机器可检查清单，不移植运行数据。

- [ ] 从路由装配追踪 import、运行期注入、store、SQL、文件投影和基础服务调用，不只统计主文件。
- [ ] 登记领域规格中的全部表族、索引、约束、ALTER 分支、跨库引用、路径配置和恢复任务；记录现有测试入口。
- [ ] 对 DE 同名模块逐项确定复用、适配、迁移或保留独立的处理，标明唯一维护者和目标依赖。
- [ ] 为每个模块指定 SERV-06–18 的交付归属，并记录尚无法定位的动态依赖及关闭方式。

验收：每个已注册领域路由可追溯到目标模块/存储/测试；任意删除一个已知表族或动态依赖时清单审查失败。证据：固定提交下的 import/SQL 核对结果及无孤立项清单；仅生成 `CREATE TABLE IF NOT EXISTS` 不算迁移设计完成。

<a id="serv-05"></a>

### SERV-05：冻结领域部署、owner 与 Claim 规则

**WP-07｜M1｜P1｜后端架构，产品/数据负责人配合｜M（1–2 人日）｜未开始**。硬依赖：BASE-03、BASE-04、SERV-04。

涉及领域迁移规格及拟新增归属映射格式/决议记录；参考现有 ZJ `device_context.py`、`stores/ontology_store.py` 和 DE `claims.py`。此 PR 冻结合同，不迁移数据库。

- [ ] 确认同进程独立领域模块、schema/worker 唯一责任及发布单元；若改独立服务，重写 IPC/存储边界后再排实现。
- [ ] 冻结个人对象 accountId + deviceScope 的授权范围、应用权限关系、解绑/转让及历史记录处理。
- [ ] 定义旧 device/global 数据的显式映射、冲突报告和未归属隔离规则；禁止自动归给首次登录者。
- [ ] 确认知君 ontology 与 DE 可选 Claim 域的事实源关系；基线为各自独立、不自动双写；规定投影/MCP 的范围。

验收：同盒不同账号、旧 global、共享资料、设备转让各有确定的允许/拒绝与映射例子；`ontology_jobs.owner_id` 仍是任务对象归属，不能误当账号列。证据：负责人决议、合成映射合法/冲突向量；待决项明确阻止对应正式迁移，不阻止隔离原型。

<a id="serv-06"></a>

### SERV-06：单一领域装配入口与基础服务适配

**WP-07｜M1｜P1｜后端装配负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-04、SERV-05。

涉及现有 ZJ `backend/server.py`、`mindos/zhijun/provider.py`、`context.py` 等；拟新增 DE 领域装配包及模型/检索/资料/存储适配接口。目标包名在本 PR 确定。PR 独占 DE lifespan 接入点和适配接口，领域实现按 SERV-07–09 分开。

- [ ] 建立显式注入的领域入口与协议接口，复用 DE 模型、资料、索引及存储生命周期。
- [ ] 提供合成 store/provider 的测试装配，不复制第二份全局单例，不从默认数据路径自动加载。
- [ ] 将领域启用与 schema/身份能力检查挂钩；初始化失败时不开放领域路由或启动 worker。
- [ ] 定义启动/停止和路由注册的单一调用顺序，给后续 store、worker、gate 提供受控接入点。

验收：临时空目录可启动/关闭领域骨架；重复装配、缺依赖或 schema 未就绪时确定失败，无重复模型/worker/目录占用。证据：隔离装配与故障注入测试、实例/资源关闭计数；桌面进程不参与启动 Python。

<a id="serv-07"></a>

### SERV-07：会话及附件领域模块与 schema 迁移

**WP-07｜M1｜P1｜后端会话/附件负责人｜L（2–4 人日）｜未开始**。硬依赖：BASE-05、SERV-05、SERV-06。

涉及现有 ZJ `stores/conversation_store.py`、`chat_import_store.py`、`conversations.py`、`chat_imports.py`、`chat_import_routes.py` 及 SERV-04 分配的会话依赖；目标为 DE 领域包对应模块。PR 独占 conversations 数据库迁移及共享连接接口。

- [ ] 迁入会话、消息、summary、decision_drafts、nudge policies/events、turn_receipts 全表族及附件五类表；保留历史升级分支和唯一约束。
- [ ] 适配 turn receipt、消息状态、请求 ID、引用/版本与隐私数据读取接口；保持 ChatImportStore 复用会话连接和锁。
- [ ] 加入可信 owner/device 查询范围及显式历史映射，未归属记录进入隔离区。
- [ ] 编写空库、旧版、缺可选表、重复升级、升级中断的合成夹具；附件 worker 暂不自行启动。

验收：升级后实体/历史/保护记录数量及摘要符合映射，receipt 不重复、消息 revision 不丢失；错误 owner、坏引用和重复映射被拒或进入明确隔离报告。证据：迁移阶段标记、前后摘要、store 合同测试；完整聊天流由 WP-08 合流验证。

<a id="serv-08"></a>

### SERV-08：本体、路由、alignment 与事项存储迁移

**WP-07｜M1｜P1｜后端本体存储负责人｜L（2–4 人日）｜未开始**。硬依赖：BASE-05、SERV-05、SERV-06、SERV-07。

涉及现有 ZJ `stores/ontology_store.py`、`alignment_store.py`、`routing_store.py`、`matters_store.py`、`zhijun/charter_policy.py`；目标为 DE 领域包。PR 独占 ontology schema 与共享连接，后续分页/幂等改动串行落在本任务之后。

- [ ] 迁移领域清单中的本体、alignment、routing 全表族及 work_matters/bindings/artifacts/actions、charter_exceptions，保留索引、租约和约束。
- [ ] 保持共享 ontology 连接/锁，适配会话及 growth 引用接口；标出尚未实现的跨库原子边界供 SERV-10 完成。
- [ ] 对个人对象、授权、任务与历史增加已冻结归属映射，保留任务对象 owner_id 原语义。
- [ ] 用合成旧库验证 scope、证据状态、撤销信息、revision 与历史升级；不默认恢复授权或启动任务。

验收：漏表/索引/约束可由 schema 清单检查发现；映射冲突、跨账号对象关联拒绝；迁移不使撤销 grant 复活。证据：schema 差异、旧版升级矩阵及 store 回归；仅事项四表通过不能关闭任务。

<a id="serv-09"></a>

### SERV-09：成长、章程及工作区模块迁移

**WP-07｜M1｜P1｜后端成长/章程负责人｜L（2–4 人日）｜未开始**。硬依赖：BASE-05、SERV-05、SERV-06、SERV-07。

涉及现有 ZJ `stores/growth_store.py`、`charter_draft_store.py`、`growth.py`、`nudges.py`、`zhijun/charter.py`、`charter_artifacts.py` 及分配依赖；目标为 DE 领域包。PR 独占 growth 数据库迁移和章程模块。

- [ ] 迁移 growth 三表、charter_writes、草稿/action、workspace/revision/action 全部表及列升级。
- [ ] 适配判断、复盘、章程解析/发布与提醒关联，保留 expectedRevision、基线 revision 和幂等语义。
- [ ] 按可信主体校验跨会话/判断引用，明确删除/解绑时的保留接口。
- [ ] 以旧版合成夹具核对 clauses、正文、历史、发布结果和跨会话范围；不触发真实模型调用。

验收：合法草稿到发布结果保留原语义；旧 revision、跨 owner 关联、重复发布与畸形旧结构产生确定结果。证据：升级摘要及现有章程/成长用例的目标适配结果；只迁三张 growth 主表不算完成。

<a id="serv-10"></a>

### SERV-10：同库事务与跨库故障恢复

**WP-07｜M1｜P1｜后端事务负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-07、SERV-08、SERV-09。

涉及迁入的 matters/routing/conversation/growth store；拟新增跨库恢复记录和协调模块。PR 独占跨库协调接口；仅在此任务内统一修改共享事务边界。

- [ ] 对照闭包列明各操作参与的数据库、锁顺序、校验点和提交点，区分同进程与同事务。
- [ ] 保持实体更新与 work_actions 等同库记录原子提交；采用冻结的同事务整合或可恢复补偿方案处理跨库关联。
- [ ] 处理会话删除/解绑、判断关联变化与提交并发的竞态；恢复前重新验证主体和对象可见性。
- [ ] 在每个提交前后注入故障，记录可恢复阶段及重复执行行为，禁止依赖分散 try/except 假装原子性。

验收：同库失败不留下半实体；跨库半完成可被识别并恢复/补偿，无悬空的已开放结果；并发锁顺序无死锁，重试不重复加 revision。证据：故障矩阵、逻辑引用检查及恢复记录摘要。

<a id="serv-11"></a>

### SERV-11：唯一 worker、启动恢复与有界停机

**WP-07｜M1｜P1｜后端任务运行负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-06、SERV-10。

涉及迁入的 `zhijun/jobs.py`、`chat_imports.py` 和 DE lifespan/实例锁；拟新增领域 worker 装配。PR 拥有 worker 注册/恢复，lifespan 修改经 SERV-06 负责人串行合入。

- [ ] 实现独占锁→schema 检查→服务注入→恢复并重新验权→worker→业务开放的启动序列。
- [ ] 对任务租约、原 job ID、paused/failed 状态定义恢复规则；显式恢复不能恢复已撤销授权。
- [ ] 停机时先拒绝新工作，再有界排空/记录恢复点，最后关闭连接和存储。
- [ ] 覆盖双实例争目录、升级中断、租约过期、工作中重启和关闭超时的隔离场景。

验收：第二实例拒绝占用目录；升级失败不启动 worker；重启不重复提交已完成结果，撤销后恢复拒绝；停机无悬挂任务/连接。证据：合成任务的原 ID/状态转换、实例计数与超时退出结果。

<a id="serv-12"></a>

### SERV-12：受保护资料与版本摄取策略

**WP-07，WP-09 前置｜M1｜P1｜后端资料保护负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-03、SERV-07、SERV-08、SERV-10。

涉及现有 ZJ `qa.py`、`uploads.py`、`chat_imports.py`、`zhijun/context_sources.py` 中保护逻辑；目标为 DE QA/上传扩展点与领域策略模块。PR 独占保护接口；上传协议本身由 WP-09 负责。

- [ ] 提取资料、版本/part、chat privacy/grants 与衍生卡的授权策略，接入 DE 普通 QA、检索及资料摄取。
- [ ] 使保护记录先于摄取/索引可见性生效；新版本继承保护属性，不继承旧 grant。
- [ ] 统一取消、重试、批次恢复及资料替换时的版本引用检查，防止借旧授权访问新内容。
- [ ] 覆盖来源 preview 与执行时重新校验，避免恢复任务绕过已撤销来源授权。

验收：授权会话按合同使用附件；普通 QA 不返回受保护附件和衍生卡；新版本、跨设备 upload/material ID、撤销后重试均不能借旧 grant 获权。证据：隔离摄取/检索/恢复矩阵及对象版本引用，不使用真实文件。

<a id="serv-13"></a>

### SERV-13：领域路由装配、权限及错误合同

**WP-07｜M1｜P1｜后端 API 负责人，Agent 配合｜L（2–4 人日）｜未开始**。硬依赖：SERV-02、SERV-08、SERV-09、SERV-11、SERV-12。

涉及迁入的 conversations/routing/ontology/memory/growth/nudges/matters/chat_imports/onboarding/home/status 路由及剩余 SERV-04 分配的领域逻辑；DE gate 和 OS 应用策略由既有负责人协同。PR 独占路由注册表及 DTO/错误映射。

- [ ] 将领域逻辑按依赖清单接入适配器，登记每个方法/路径的 owner、scope、来源校验、请求/响应预算及能力开关。
- [ ] 对读写路由统一验证可信主体，不仅依赖 write_guard；global-only 能力保持关闭或完成设备实现后单独开放。
- [ ] 保留 preview→dispatch 的来源快照与绑定 revision 检查，不把本地保存成果当作执行授权。
- [ ] 与 Agent/桌面同步已实现合同的逐条策略和安全错误；未实现的新路径/字段不提前开放。

验收：已登记独立 API 在合成主体下保持业务结果；匿名、跨 owner、过时 preview、缺 scope、未启用功能均拒绝。来源/任务/事项不绕过统一 gate。证据：逐路由正负矩阵、未迁移依赖清零及能力列表；无消息协议时不得宣称完整建档/判断闭环。

## 事项合同、清除与迁移验收

<a id="serv-14"></a>

### SERV-14：事项列表、成果及历史有界分页

**WP-07｜M1｜P1｜后端事项 API 负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-08、SERV-13。

涉及迁入的 `matters_routes.py`、`stores/matters_store.py`；拟新增游标编解码/摘要 DTO 和合同测试。PR 独占分页读取改动；与 SERV-15 串行合入共享文件。

- [ ] 冻结版本化分页增量：列表/历史默认 20、最大 50，拟单页 512 KiB；摘要不含全文，现有当前全文读取保留。
- [ ] 将游标绑定 owner/device、过滤条件、排序、合同版本及有效期；历史采用不可变事件顺序。
- [ ] 实现事项弱一致排序 `updatedAt DESC,id ASC`、摘要字节预算及 nextCursor；不承诺分页快照或静默截正文。
- [ ] 单版历史全文先冻结新合同再实现并同步策略；核对 50,000 字符在实际 UTF-8/JSON 编码下的预算。

验收：多页和同时间戳历史读取稳定，错误范围/过滤/过期游标拒绝；并发事项更新按声明的弱一致语义去重/刷新，不伪称无漏项。单条超预算有明确错误，无损坏 JSON。证据：字节边界、历史顺序、游标篡改/越权用例和前端合同向量；拟预算未确认不能当生产默认已存在。

<a id="serv-15"></a>

### SERV-15：稳定客户端输入幂等与 revision 冲突

**WP-07｜M1｜P1｜后端事项事务负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-10、SERV-13、SERV-14、SERV-16；回放接口原型可先行，保留/墓碑行为按 SERV-16 冻结结果验收。

涉及迁入的 `matters_routes.py`、`stores/matters_store.py` 和 action/history schema；PR 同时修改路由与 store，不能只修 fingerprint 函数。

- [ ] 以可信 owner/device、合同版本、原 requestId 建立去重范围，fingerprint 使用规范化客户端输入并区分省略/显式值。
- [ ] 首次执行保存解析后的默认标题、消息正文和来源快照；重试先验证当前身份/对象可见性，再查回放，不重新解析已变化的标题或要求源消息仍存在。
- [ ] 将实体与回放同事务提交，分别实现实体 revision 和 bindingRevision；冻结并区分键复用与 revision 冲突错误。
- [ ] 分离长期历史与回放清理职责，提供 SERV-16/17 的保留窗、最小墓碑和已清除回放接口，客户端旧键不能被静默当新操作。

验收：成功后丢包、改事项标题后重试、源消息删除后重试、同键并发均只产生原结果/原 revision；同键不同输入拒绝。跨 owner 或已清除结果不得回放正文。证据：路由级失败回归、事务故障矩阵和历史/回放计数；在此任务通过前不得开启通用写重试。

<a id="serv-16"></a>

### SERV-16：冻结清除范围、回放保留与恢复规则

**WP-07｜M1｜P1｜产品/后端数据负责人｜M（1–2 人日）｜未开始**。硬依赖：SERV-05、SERV-10。

涉及领域规格及拟新增清除范围/状态机/保留合同；参考现有 `ontology_store.py` purge 与 `matters_store.py`。PR 仅冻结接口和数据规则，不添加未经确认的 DELETE 路由。

- [ ] 确定事项、成果、正文历史、来源快照、绑定、任务、grant/preview、回放、投影与备份的选定清除范围及关联对象保留规则。
- [ ] 冻结预览→显式确认→停止任务/撤销授权→可恢复清除→回执流程和失败状态；路径/确认字段另经 API 评审落定。
- [ ] 确定回放窗口（规格建议 7 天，尚未定案）、最小无正文墓碑期限及窗外“结果无法确认”行为。
- [ ] 明确备份保留、恢复后的再清除和用户主动导出文件边界；解绑/paused/completed 不等于删除。

验收：每类数据都有保留/删除/墓碑/备份处理结论；删除对话不删除已保存成果等现状与新合同差异明确。证据：负责人决议、合成清除预览和范围外保留样例；不能以旧 ontology purge 声称 work_* 已清除。

<a id="serv-17"></a>

### SERV-17：可恢复清除与回放防复活

**WP-07｜M1｜P1｜后端数据生命周期负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-11、SERV-15、SERV-16、SERV-18；SERV-18 交付独立投影清理端口，本任务负责清除协调，不反向阻塞该端口开发。

涉及领域 stores、worker 撤销、投影清理接口；拟新增清除协调器、操作记录及已冻结路由。PR 独占清除调度，调用各 store 明确的清理接口。

- [ ] 实现与冻结预览一致的确认校验、任务停止/授权撤销和分阶段持久清除记录。
- [ ] 清理选定实体、全文历史、来源快照和回放正文，保留合同要求的无正文审计/去重墓碑。
- [ ] 实现中断恢复和重复清除，阻止旧任务、旧 requestId、投影重建或恢复程序使正文重新出现。
- [ ] 接入备份保留/恢复后再清除流程；不删除范围外会话、判断或用户导出文件。

验收：各阶段崩溃后继续执行达到同一结果；清除后历史、回放、重启和任务恢复均不能重新返回正文；其他 owner/范围对象不变。证据：隔离清除前后摘要、故障/重复执行矩阵以及恢复后的再清除用例。

<a id="serv-18"></a>

### SERV-18：设备投影与 Claim 事实源隔离

**WP-07｜M1｜P1｜后端知识/投影负责人｜L（2–4 人日）｜未开始**。硬依赖：SERV-05、SERV-08、SERV-09、SERV-12、SERV-13。

涉及迁入的 `zhijun/projection.py`、`memory_routes.py`、`ontology.py`、`zhijun/memory_index.py` 及 DE `claims.py`/MCP 接口；PR 独占投影和事实源适配。

- [ ] 实现已冻结的知君 ontology 事实源边界，保留候选/确认/撤回语义；不自动向 DE Claim 域双写。
- [ ] 为个人索引/投影建立 owner/device 范围；global 文件和 MCP 只按已批准用途访问。
- [ ] 防止设备 API 的确认、事项成果和任务恢复自动写入全局 USER.md/ZHIJUN_PROFILE.md 或生成授权。
- [ ] 接入撤回、清除和索引重建接口，补齐投影旧版本/未归属数据隔离测试。

验收：设备 A 的确认及索引查询不会暴露给不具备范围的 B；事项保存不生成 Claim，撤回后查询/投影按合同更新；global-only 能力未实现时保持关闭。证据：合成多主体查询/投影差异及 DE Claim 未发生意外写入的断言。

<a id="serv-19"></a>

### SERV-19：整组迁移、故障注入与恢复演练

**WP-07｜M1｜P1｜后端迁移负责人，验证配合｜L（2–4 人日）｜未开始**。硬依赖：BASE-05、SERV-07、SERV-08、SERV-09、SERV-10、SERV-11、SERV-12、SERV-17、SERV-18。

涉及拟新增迁移/备份恢复脚本、合成旧版夹具与演练报告；只操作临时目录。PR 独占迁移 orchestration 与证据格式，不在开发机运行库执行升级。

- [ ] 固定受支持旧版本矩阵及显式 owner 映射，按 MIG-00–04 在隔离副本执行升级、重复执行和各阶段中断。
- [ ] 以受控停写点备份全部 SQLite（正确处理 WAL）、资料/版本映射、文件、投影、索引及代码/config/schema 版本，不仅备份 ontology.db。
- [ ] 验证每阶段数量/摘要、唯一约束、外键/逻辑引用、未归属记录、撤销和 worker 唯一性。
- [ ] 停服务后整组恢复匹配旧代码/数据，演练恢复后再清除；新版本已接收写入时走差异处置，不能静默覆盖。

验收：空库、各旧版、缺可选表、owner 碰撞、阶段中断均产生可复核结果；混用旧代码/新 schema 或不完整备份被拒。恢复后无未处理悬空引用/重复 worker/撤销复活。证据：脱敏迁移报告、备份清单/摘要、恢复检查结果；备份与业务数据不提交 Git。

<a id="serv-20"></a>

### SERV-20：服务端合同合流与发布前交接

**WP-07，配合 WP-05/WP-08/WP-09｜M1｜P1｜后端集成负责人，验证配合｜M（1–2 人日）｜未开始**。硬依赖：SERV-03、SERV-13、SERV-14、SERV-15、SERV-17、SERV-18、SERV-19。

涉及拟新增服务端验收矩阵、固定版本清单、领域能力/路由说明和发布交接记录；PR 只做集成修正与证据归档，未实现功能退回所属任务。

- [ ] 在固定 Agent/DE/知君组合下核对逐路由权限、错误、字段、预算、schema 与能力开关，关闭所有 local-debug 旁路。
- [ ] 汇总同盒不同账号、两设备、撤销、分页、稳定重放、跨库中断、清除后恢复的正负结果。
- [ ] 与 WP-06/WP-08 交换领域消息/恢复测试向量，与 WP-09 交换保护/版本向量；标注尚未完成的消息或上传端到端场景。
- [ ] 交付运行/升级/回退步骤、准确组件哈希、已知限制和未验事项；将文档状态按实际证据更新。

验收：服务端所有已开放能力都有可定位的正负向证据，无未说明的路由、schema 或权限差异；M0 真机、聊天合流、上传合流的完成结论各自独立，不把服务端组件测试当全产品验收。证据：可重现验收矩阵和关联 PR/版本清单；本任务完成不自动授权迁移运行库或发布安装包。

## 排序与共享文件责任

M0 关键依赖为 `BASE-01/02/03 → SERV-01 → SERV-02 → SERV-03 → WP-05`；SERV-03 的资料夹具/投影可基于 BASE-04 草案提前开发。领域调查 SERV-04、合同 SERV-05 及隔离装配可与 M0 并行；三组存储由独立负责人实现，SERV-08 使用 SERV-07 的会话引用接口，SERV-09 与 SERV-08 可并行。

DE `server.py`/lifespan 由 SERV-06 装配负责人统一合入，鉴权入口由 SERV-02 负责人审查；ontology schema 由 SERV-08 负责人维护，其他任务不得并行独立编号升级版本；matters 路由/store 按 SERV-14→15→17 串行；DE uploads/QA 由资料负责人协调 SERV-03→12 与后续 WP-09。接口草案可以并行，共享文件修改必须由指定负责人合流，不以 Git 能自动合并代替语义验证。

本清单 20 项：L 16 项、M 4 项，合计 **36–72 人日**的初步有效工作量，其中 M0 的 SERV-01–03 为 **6–12 人日**；这是人力估算，不是自然日承诺。对外部身份桥或历史 schema 出现未知改造时，先更新 BASE/任务边界与估算，再排期。
