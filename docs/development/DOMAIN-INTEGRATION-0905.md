# 知君盒端领域迁移执行规格

日期：2026-09-05。状态：**拟实施规格，尚未移植服务、升级数据库或处理用户数据**。源代码基线为产品源 `22dc9a3` 同步后的当前工程；相邻服务端基线及未提交改动见 [集成方案](INTEGRATION-0905.md)。本文细化 P4/P6，桌面边界见 [桌面合同](DESKTOP-CONTRACT-0905.md)，排期及依赖见 [工作包](INTEGRATION-WORKPACKAGES-0905.md)。

**当前服务基线：** 家中盒子保留并发新发布 `6b549ad3371b5250a4b6e6f2afd7cd06c61ef6b0`，D03 在独立分支 `dev/zhijun-business-bridge-0906-live` 的 `c16dc17be81240285820b9e86076877911d153c4` 上合流；已部署的 6 个运行文件与该交付一致。原 `ec2854e` + dirty 调研事实及其上传协议描述保留为历史，不能当成现运行基线。新发布的权限、分页、错误边界和 Pocket 改动已保留；这不表示知君领域或上传四层合同已完成集成。

D03 最小资料桥已部署且真实SDK空资料页/一次断开重连已验，见[盒端部署记录](BOX-DEPLOYMENT-0906.md)；本文知君领域迁移仍是待实施规格。bridge 新 scope 空页不代表旧 global/owner 资料已迁移；未来非空数据接入需重新对齐当前服务隐私、保留和生命周期规则。

## 1. 实施边界与进入条件

拟以“知君领域作为 data-engine 同一进程内的独立模块”为实施基线。复用 data-engine 的资料、知识、检索、模型运行时和存储生命周期；桌面不再启动 Python。**这不是跨团队部署批准**：服务端维护者仍需确认模块入口、发布单元、schema 负责人及唯一 worker 负责人。若最终改为独立服务，必须重写内部 API、鉴权及故障恢复合同，禁止两个进程共用 SQLite / Chroma / 同一运行数据目录。

实施前交付一个不含运行数据的依赖清单，逐项记录：源模块、目标模块、调用方、store/表、身份条件、事务边界、错误与回退路径、测试入口。以下清单作为初始范围，不能代替完整 import / SQL 审计。

| 必需输入 | 预期输出 | 进入真实联调或数据迁移的验收条件 |
| --- | --- | --- |
| 固定的知君/data-engine 源版本与上传交付物 | 跨仓版本清单、路由差异表 | 不依赖无法定位的 dirty 文件；同名基础服务逐项确定保留版本 |
| 可信身份桥合同、正式应用路径策略 | 领域请求上下文与逐路由能力清单 | 业务处理器可获得已验证主体；local-debug 关闭；scope 缺失不能回退 global |
| 本文 owner、Claim、保留规则的决议 | 归属矩阵与迁移映射格式 | 同盒不同账号与旧 global 数据的处理有明确规则 |
| 隔离的旧版本测试数据夹具 | schema 清单、升级/恢复脚本设计、演练报告 | 不读取或修改开发机现有用户数据；覆盖历史升级分支 |

外部输入未齐备时，可按显式假设推进模块骨架、注入式适配器、模拟合同及合成数据上的迁移测试；这些结果不能代替真实身份联调，也不能用于批准运行库迁移。

## 2. 模块、存储与基础服务依赖闭包

拟新增单一领域装配入口，由 data-engine lifespan 注入基础服务适配器、注册 routers 和 worker。模块应通过显式适配器使用基础服务；不要复制第二份全局模型、上传、资料或索引单例。

| 领域入口 / 当前实现 | 需要连同提取的依赖 | 目标输出与关键验收 |
| --- | --- | --- |
| `conversations.py`、`zhijun/turn.py` | turn receipt、routing/context/context_sources、memory、reply assistance、provider/gate、任务排队 | 保留消息 requestId、完成/中止状态及来源快照；流能力由 P5 合流验收 |
| `routing_routes.py`、`zhijun/routing.py` | 来源检查、preview、service/purpose、alignment、charter policy、附件授权 | preview 与 dispatch 均验来源/绑定 revision；旧授权不能随任务恢复而重新生效 |
| `ontology.py`、`memory_routes.py` | extract/consolidate/projection、证据与实体、jobs | 候选/确认/撤回语义保留；global-only 功能先隐藏，不能解除 guard 解锁 |
| `growth.py`、`nudges.py`、`zhijun/charter.py` | 判断/复盘、草稿/工作区/发布/例外、onboarding/home/status | 跨会话关联及乐观锁保持；章程写入不能只迁 growth 三张主表 |
| `matters_routes.py` | MattersStore、ConversationStore、GrowthStore、message_ref/context_sources | 事项、成果、绑定 revision 各自保持；不自动产生 Claim 或授权 |
| `chat_import_routes.py`、`chat_imports.py` | 批次恢复、资料版本/part、privacy/grants、模型选择、material pipeline | 保护先于摄取；完成/重试/取消不破坏附件与版本归属 |
| 基础 `qa.py` / `uploads.py` | 知君加入的受保护资料排除、衍生卡排除、新版本保护继承 | 提取保护策略接口接入 data-engine；普通 QA 不消费受保护附件，新版本不继承旧 grant |

路由装配依据 [server.py](../../backend/server.py#L1238)，保护逻辑依据 [qa.py](../../backend/mindos/qa.py#L329)、[uploads.py](../../backend/mindos/uploads.py#L697)。这里提出的是适配器职责，不是现成的 data-engine 扩展 API。

### 2.1 已识别的表族

| 当前数据库 / store | 需登记的表 | 迁移时必须保留或审计 |
| --- | --- | --- |
| conversations / ConversationStore | `conversations`、`messages`、`conversation_summaries`、`decision_drafts`、`nudge_policies`、`nudge_events`、`turn_receipts` | 消息与会话逻辑引用、requestId 唯一性、metadataRevision、提醒升级分支 |
| conversations / ChatImportStore | `chat_import_batches`、`chat_import_files`、`chat_material_privacy`、`chat_material_grants`、`chat_reference_selection` | 复用会话连接/锁；跨资料库的版本引用；删除会话后仍需保留的保护记录 |
| ontology / OntologyStore | `entities`、`entity_aliases`、`claims`、`claim_evidence`、`review_events`、`ontology_jobs`、`ontology_meta`、`entity_merge_proposals`、`claim_conflicts` | scope、证据、状态、任务租约、条件唯一索引；历史 ALTER TABLE 分支 |
| ontology / AlignmentStore | `alignment_requests`、`alignment_grants`、`alignment_conversations` | 同意与 token 版本；升级后不能恢复已撤销授权 |
| ontology / RoutingStore | `context_lookup_stages`、`routing_modes`、`routing_grants`、`routing_previews`、`routing_audits`、`routing_pending`、`routing_auto_consent`、`routing_auto_exclusions`、`routing_auto_history`、`routing_handling`、`routing_tasks` | 共享 ontology 连接/锁；预览、任务、审计之间的一致性 |
| ontology / MattersStore、charter policy | `work_matters`、`work_matter_bindings`、`work_artifacts`、`work_actions`、`charter_exceptions` | 当前幂等表同时承载正文历史；绑定引用 conversations、decision 引用 growth，不能误认为全在同一事务 |
| growth / GrowthStore、CharterDraftStore | `growth_charters`、`growth_decisions`、`growth_reviews`、`charter_writes`、`charter_drafts`、`charter_draft_actions`、`charter_workspaces`、`charter_workspace_revisions`、`charter_workspace_actions` | 章程正文与 clauses、发布幂等、基线 revision、跨会话范围校验 |

源码入口：[会话](../../backend/mindos/stores/conversation_store.py#L24)、[附件](../../backend/mindos/stores/chat_import_store.py#L19)、[本体](../../backend/mindos/stores/ontology_store.py#L96)、[alignment](../../backend/mindos/stores/alignment_store.py#L17)、[routing](../../backend/mindos/stores/routing_store.py#L11)、[事项](../../backend/mindos/stores/matters_store.py#L16)、[成长](../../backend/mindos/stores/growth_store.py#L25)、[章程草稿](../../backend/mindos/stores/charter_draft_store.py#L246)、[章程例外](../../backend/mindos/zhijun/charter_policy.py#L198)。

schema 清单还须覆盖 data-engine 资料/版本/索引的被引用对象，以及运行路径、投影文件、迁移标记、索引和约束；表名相同不说明 schema 或语义兼容。不能用单次 `CREATE TABLE IF NOT EXISTS` 代替显式、可重复且可识别失败阶段的升级。

### 2.2 事务与 worker 验收

同库写保持现有事务，例如 `work_actions` 与实体更新同一 `BEGIN IMMEDIATE`：成功响应丢失后重试应回放已提交结果，不再次增 revision。跨库引用不是 SQLite 外键原子事务；需记录校验顺序、锁顺序和删除/解绑竞态的处理，选择同事务整合或带恢复记录的补偿流程后再实现，不因同进程就假定原子性。

生命周期必须只有一个调度负责人。data-engine 启动顺序拟为：独占存储锁 → 检查/升级 schema → 注入适配器 → 恢复半完成记录并重新校验授权 → 注册 worker → 开放业务入口。停机反向执行，停止接收新工作，等待有界排空并保存可恢复状态后关闭存储。

验收必须包含：两个服务争用同一目录时第二个拒绝启动；升级中断不启动 worker；租约过期/进程重启不会重复提交结果；paused/failed 任务沿原 job ID 显式恢复且重新验权；恢复不绕过已撤销 grant。源码参考 [领域 worker](../../backend/mindos/zhijun/jobs.py#L358)、[附件恢复](../../backend/mindos/chat_imports.py#L305)、[服务生命周期](../../backend/server.py#L751)。

## 3. 持久归属与 Claim 事实源

建议个人会话、本体、判断、章程、事项/成果使用已验证的 `accountId + deviceScope` 作为授权范围。这是**拟定目标**，当前知君主要使用 `device:<deviceId>`，并未完整实现账号维度。现有 `ontology_jobs.owner_id` 表示会话等任务归属对象，不能直接重命名为账号 owner，也不能借此宣称账号隔离已存在。

| 数据族 | 拟定规则 / 待跨团队确认项 | 验收 |
| --- | --- | --- |
| 个人领域对象及历史/幂等记录 | 与创建主体绑定；所有读、写、关联和恢复任务必须携带可信 owner/device；应用权限单独检验 | 同盒账号 B 不能靠 A 的对象 ID、requestId、cursor 或任务 ID 读写 A 的数据 |
| 盒级资料、目录及统计 | 由产品明确盒级共享还是 owner 私有；目录当前未按 device 隔离，首轮收窄返回 | items、folders、计数、删除/版本操作使用同一归属规则，无侧信道计数泄漏 |
| 旧 `global` 数据 | 不自动归给首次登录者；导入须提供显式映射、碰撞报告和隔离未归属区 | 未映射数据不经正式设备 API 返回；重复导入不产生重复对象或 grant |
| 设备解绑/转让 | 必须明确保留、清除或经授权转移；登录退出只清缓存，不改变持久归属 | 撤销连接后旧身份失效；新设备持有人不会自动继承个人领域对象 |
| 投影与 MCP | 现有 global 文件投影与 device API 视图分开；远程确认不自动写成全局画像 | API 读到的 device 画像不会串写 `USER.md` / `ZHIJUN_PROFILE.md` |

data-engine 可选 `MINDOS_CLAIM_STORE_ENABLED` 域有自己的 owner/device、问卷与 Claim 状态语义，见 [claims.py](../../../nexusaos-data-engine/backend/mindos/claims.py#L58)。拟基线是：**知君 ontology 继续作为知君个人理解的事实源；data-engine 可选 Claim 域保持独立，不自动双写**。若两者必须互通，后续另定命名空间、来源关系、人工确认和冲突规则；不可按同名 `claims` 表直接合并或让事项/成果自动成为 Claim。该选择需两端负责人确认后写入冻结合同。

## 4. 事项与成果远程合同增量

当前 12 个方法/路径组合见 [matters_routes.py](../../backend/mindos/matters_routes.py#L222)。以下是迁移前需要实现的**建议变更**，当前服务端和 SDK 策略尚未提供这些增量；不应提前让客户端发送未支持的参数或调用拟接口。

### 4.1 有界读取与分页

当前事项、成果及历史读取不分页；历史 `record` 含每版正文。建议保留已存在的对象读取路径，为列表/历史增加经版本协商的分页合同：

| 操作 | 拟请求 / 响应增量 | 验收条件 |
| --- | --- | --- |
| `GET /matters`、`GET /matters/{id}/artifacts` | `limit` 默认 20、最大 50；不透明 `cursor`；返回 `items,nextCursor,hasMore`，列表为摘要 DTO | 当前前端须同步适配，不假定每项都有正文；分页有服务端字节预算 |
| 已有两个 `.../history` | 默认 20、最大 50；游标按不可变事件顺序；返回 revision/at/摘要，不含整篇 record | 并发写时无重复历史项；历史不可因晚到的页覆盖新 revision |
| 当前 `GET /artifacts/{id}` | 保持读取当前全文 | 50,000 字符不是 50,000 bytes，按实际 UTF-8 / JSON 编码验证上限 |
| 历史某版本全文 | 需新增、冻结单版读取合同后实现，本文不宣称已有对应路径 | 只读 owner/device 范围内版本；主进程、Agent 路径策略同步更新 |

建议列表/历史单页 JSON 预算为 512 KiB（待冻结的产品预算，低于现有 16 MiB 传输上限），按字节提前停止并返回下一游标；禁止静默截断正文或返回损坏 JSON。若单条摘要也超预算，应返回可识别错误。精确 total 默认不作为远程页面依赖，避免全量计数和跨范围统计。

游标须绑定 owner/device、过滤条件、排序、合同版本及有效期，不能由客户端提供任意 SQL 偏移。可变事项列表采用明确的弱一致语义：`updatedAt DESC,id ASC`，客户端按 ID 去重，刷新首屏获取并发更新，**不承诺分页期间无漏项快照**；如产品需要无漏项，须另做快照机制。历史以稳定事件键分页，不依赖可变标题或当前正文。过期/不匹配游标要求重新读取，不能扩大范围继续查询。

### 4.2 稳定幂等、revision 与不确定结果

当前 `requestId` 为 8–100 位字母/数字/下划线/连字符；`work_actions` 以 `(device_scope,request_id)` 唯一，并同时存 fingerprint、回放结果和历史。当前冲突统一为 `WORK_REVISION_CONFLICT`。事项/成果编辑使用实体 `expectedRevision`，绑定使用独立 `bindingRevision`（请求字段仍叫 `expectedRevision`），不能相互代替。

已确认缺口：创建成果在路由中读取当前事项标题及当前消息正文，再计算 fingerprint。省略 title 的首次请求成功后，事项标题变化可能使相同客户端输入重试变为 409；路由也会在查询幂等记录前重新检查源消息是否仍存在。稳定回放需要同时修改路由与 store，不能只改 fingerprint 函数。

拟合同如下：

1. 幂等键绑定可信 owner/device、合同版本和原 requestId；fingerprint 使用动作名、目标 ID、规范化的**客户端输入**（区分省略与显式值），不再使用重试时变化的默认标题/正文。首次执行时保存解析出的标题、正文和来源快照。
2. 每次先验证当前身份及对象可见性，再查已提交记录；同键同输入回放原结果，同键不同输入返回独立的拟错误 `IDEMPOTENCY_KEY_REUSED`。实体修改冲突另返回拟错误 `REVISION_CONFLICT`，前端保留草稿并读取最新值。这些错误码需与统一错误合同一起实现。
3. 正常情况下，原消息之后删除不改变已保存成果的回放；若成果已依照清除合同删除，则不得从旧回放记录返回正文，应返回已删除/不可见结果。重放不能绕过主体撤销或访问范围变化。
4. 创建/编辑结果与幂等记录同事务提交；先成功后丢包、提交前崩溃、同键并发、修改标题后重试均验收。客户端重试沿用原 requestId；新用户动作才分配新 ID。
5. 建议回放保留窗暂定 7 天，最终由离线重试需求和隐私保留规则共同冻结。清理回放正文后保留最小去重墓碑的期限也需明确；窗口过后返回“结果无法确认”，不得悄悄把旧键当新建。幂等记录与长期历史应拆分职责或独立清理，避免清历史时同时失去去重能力。

在稳定回放实现前，不得为成果创建启用通用断网/超时写重试。客户端保留原操作 ID 与“不确定结果”状态，重新读取可见成果核对；不能自动换 ID 重建。这里与聊天仅一次特定 409 重预览是不同合同。

### 4.3 保留与清除

当前没有事项/成果 DELETE 路由；删除对话不删除已保存成果，旧 ontology purge 也不清 `work_*`。解绑只是清绑定，paused/completed 只是状态变化，不能被描述为删除。源码依据 [事项存储](../../backend/mindos/stores/matters_store.py#L158)、[本体 purge](../../backend/mindos/stores/ontology_store.py#L1827)。

新增清除能力前，产品需要确定清除对象及范围：事项本身、所属成果、正文历史、来源快照、会话绑定、待执行任务、grant/preview、回放结果及备份。建议流程为“范围预览 → 显式确认 → 停止相关任务并撤销可复用授权 → 执行可恢复清除 → 结果回执”；端点和确认字段待冻结，本文不添加实际 DELETE API，也不执行删除。

验收应证明：删除后正文不从历史、幂等回放、投影或重启恢复重新出现；关联会话/判断是否删除严格按选定范围处理；最小审计/去重墓碑不含被清除正文；隔离范围外对象不变。备份保留期和恢复后的再清除机制须在产品说明中列明。用户主动复制/导出的 PC 文件不属于盒端清除可直接删除的对象。

## 5. 迁移顺序与恢复交付

| 步骤 | 输入 → 输出 | 通过条件 |
| --- | --- | --- |
| MIG-00 冻结 | 源版本、身份、归属及合同决议 → 版本化依赖/schema/API 清单 | 服务端、桌面、Agent 对新增字段/路径/预算达成一致；未定能力保持关闭 |
| MIG-01 空库装配 | 独立模块和基础服务适配器 → 空数据目录可启动 | 单入口、无重复单例/worker；路由 gate 与错误合同通过 |
| MIG-02 隔离副本升级 | 历史版本合成夹具、显式 owner 映射 → 升级后库与迁移报告 | 表/索引/约束/状态完整；未归属记录隔离；重复执行不改结果 |
| MIG-03 原子性与保护 | 任务/消息/资料/版本/来源夹具 → 故障注入报告 | 同库原子提交；跨库半完成可恢复；保护先摄取；撤销后不能恢复授权 |
| MIG-04 有界远程读写 | 分页/幂等/清除实现 → P4 验收报告 | 同盒不同账号、两设备、预算边界、丢包重试、清除后重放全部通过 |
| MIG-05 与 P5/P6 合流 | 已验证的消息协议/上传版本 → 完整知君闭环 | 建档、聊天、判断、事项/成果、附件在正式鉴权下通过；此时才宣称业务集成完成 |

升级必须先阻止新写入、停止 worker 并排空。备份覆盖同一停写点的所有相关 SQLite（含正确处理 WAL）、文件/资料/版本映射、投影和索引版本，以及配套代码与配置版本；不能只复制 ontology.db。使用 SQLite backup 或受控停机快照等一致性方式，记录摘要与校验结果，备份不提交到 Git。

恢复演练交付：匹配的旧代码、整组数据快照、schema 标记和恢复步骤；故障后先停服务再整体恢复，禁止只回退代码而保留新 schema。恢复后检查完整性、逻辑引用、授权撤销状态和 worker 唯一性，再开放业务。新版本已接受写入后的回退需要停写、差异数据处置及负责人决策，不用旧快照静默覆盖新数据。

schema 验收至少包含：空库、每个受支持旧版本、缺少可选表、历史列迁移、重复升级、各阶段中断、外键/逻辑引用检查、唯一约束、owner 映射碰撞、升级前后实体/历史/授权数量与摘要、旧版本整组恢复。索引可重建不代表原件/授权可以丢失；若索引需重建，开放查询前必须证明保护过滤仍生效。

## 6. 本轮交付与仍待冻结

本轮仅定义可执行规格。领域模块、schema 升级、分页、稳定幂等和清除能力均未实现，本文的数值、拟错误码及新增读取能力也未成为已部署合同。

跨团队必须冻结：同进程装配及发布责任；owner/device 与旧 global 归属；Claim 事实源；分页/回放保留窗及清除范围；基础保护适配器与唯一 worker 生命周期。冻结结果应回填 [集成方案](INTEGRATION-0905.md) 和 [工作包](INTEGRATION-WORKPACKAGES-0905.md)，据此收敛本地原型并进入对应真实联调及迁移验收。
