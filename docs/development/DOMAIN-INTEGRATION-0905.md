# 知君盒端领域集成与持久边界

更新：2026-09-06。当前已实现独立 UDS worker 与 DE 能力适配；此前“同进程迁移”的建议已由本方案替代。**领域完整盒子/UI 验收、生产 Admin 新应用发布仍待完成**；本文不宣称已迁移用户历史数据。

配套：[架构](ARCHITECTURE-0905.md)、[集成与环境变量](INTEGRATION-0905.md)、[桌面合同](DESKTOP-CONTRACT-0905.md)、[执行计划](FULL-PRODUCT-INTEGRATION-0906.md)。

## 1. 当前装配与进入条件

DE 的 `zhijun_gateway` 是唯一远程业务入口，按可信 workspace 启动知君 [zhijun_worker](../../backend/zhijun_worker/app.py)。worker 只监听私有 UDS，通过 `POST /v1/dispatch` 装配原领域路由；内部源事件走 `POST /v1/events`。它不启动原 `backend/server.py`，也不启动全局 watcher、Chroma、上传/模型运行时。

共享 catalog 中 95 项 domain 操作由 worker 承载；其余 54 项资料和 21 项模型操作由 DE capabilities 提供。worker 的资料/检索/模型/附件依赖通过显式端口回调 DE，不能打开另一份全局 canonical 数据库或直接接收 PC 的身份声明。

| 进入条件 | 当前代码约束 | 正式验证 |
| --- | --- | --- |
| 新应用与逐请求身份 | Agent v2证明 → DE principal → account/device/ownershipEpoch workspace | 新Admin登记、真实ticket、跨主体矩阵待验 |
| 发布代码与catalog一致 | worker启动检查所有domain清单路由实际注册 | 部署SHA、依赖环境与缺路由拒绝待核 |
| 独立路径与进程唯一性 | 根目录0700、主体标记0600、文件锁、UDS私有key | 盒端路径权限、并发进程和重启待验 |
| 基础能力与模型授权 | HMAC能力端口、有效租约、来源和consent复验 | 真实资料/模型与生命周期待验 |

## 2. 模块与数据闭包

| 领域模块 | 装配职责 | 基础能力边界 |
| --- | --- | --- |
| conversations / turn / routing | 消息、回执、SSE、来源预览、授权、学习、表达辅助 | 模型和来源检索经DE；保留preview/revision与取消语义 |
| ontology / memory / extract / consolidate | 实体、理解、证据、候选/确认/撤回、投影 | workspace隔离；不把旧global资料导入新个人理解 |
| growth / nudges / charter | 判断、复盘、章程草稿/工作区/发布、提醒 | 仅有效Owner租约下执行后台工作 |
| matters / artifacts | 持续事项、会话绑定、成果正文和历史 | 事项与成果不自动成为Claim，不扩大来源授权 |
| chat_imports | 附件批次、版本、隐私、引用选择、恢复 | 上传/预览/保护经受限能力；保护先于摄取 |
| onboarding / home / status | 建档、首页与领域状态 | 缺能力返回明确错误，不返回演示业务结果 |

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

## 3. workspace、事务与生命周期

`workspaceId` 为 compact JSON `[deviceId,accountId,ownershipEpoch]` 的 SHA256，64位小写hex；领域 scope 为 `zhijun:v2:<workspaceId>`。目录按此 ID 隔离，worker 内主体不可变。`WorkspaceLock` 检查 `.workspace.json` 与非空未分配目录，第二个进程不能争用相同根。

知君 ontology 继续作为知君理解的事实源，DE 可选 Claim 域不自动双写。独立 domain root 不位于 DE 个人记忆/profile 退役清理目录。旧 `global` 或旧 `device:*` 数据不归首次登录者；显式迁移需独立设计映射、碰撞处理与恢复，不借空库启动完成迁移。

同库事务仍使用原 store 的锁与事务；跨 conversations/ontology/growth/canonical 资料引用不能被称为单一 SQLite 原子事务。新增 Gateway 幂等只防止同客户端相同 start 重复执行，**并不自动修复各领域旧 requestId/fingerprint 的业务语义**。

Gateway 每盒最多4个worker，Owner租约30秒，由有效请求续租；workspace运行任务4、排队8。worker装配顺序为主体/环境校验 → 独占锁 → 初始化领域stores → 绑定能力 → 启动领域和附件worker → 开放dispatch。退出先停任务/解绑能力，再释放锁；线程未停止时不允许第二个worker接管同一目录。

Gateway重启把queued/running任务标记interrupted，不自动重放。后台调用必须登记executionRequestId并绑定活跃执行/租约；没有有效授权不能绕过前台恢复新工作。服务端取消不承诺数据库回滚，客户端保留不确定写入状态。

## 4. DE能力、附件与模型授权

worker [capabilities.py](../../backend/zhijun_worker/capabilities.py) 使用内部HMAC协议；DE核验workspace/epoch/时间/nonce/原始body摘要，以及该worker当前执行和租约。外部renderer不能直接访问这些内部端口。Gateway按进程代次生成32字节key，写入私有运行目录，销毁worker时移除；key和主体文件不在日志或Git中出现。

能力调用分成普通JSON和模型NDJSON流。模型配置、凭据与运行时由DE统一维护；worker不继承模型Key，也不另开本地模型server。外发前必须有真实UI来源选择与确认：consent绑定preview revision、configurationRevision、服务/用途、sourceRefs/version以及精确请求摘要；空sourceRefs不表示提示词可免确认。后台不签发新的前台consent，来源变化或撤销后重新验证。

附件传输先在Gateway形成完成态加密upload，再由受控操作引用。盒端还原匿名UploadFile并在调用后关闭。protected chat-import、资料版本/parts、隐私标记、引用授权继续由DE canonical能力落实；普通QA/搜索不能因为附件已上传就消费受保护内容。版本变更不继承旧grant。

Gateway AES-GCM store只保存传输任务、事件、上传/blob和preview/consent/background内部ledger；密钥独立保存。它不取代领域SQLite事务，也不承诺canonical资料或全部领域库都被加密。单文件200MiB、workspace磁盘1GiB、job事件16MiB/catalog更小上限；TTL删除载荷后保留有界去重tombstone，额度满拒绝新写，不能删墓碑后把旧请求当新写。

## 5. 生命周期事件与保留

DE canonical资料更新/隐藏/删除/恢复等事件由持久outbox驱动；Gateway仅在有效Owner租约下取有限批次，以HMAC签名提交worker `/v1/events`，成功后ACK。worker核对事件ID并幂等处理，让来源变更传播到理解、来源快照、相关投影和consent记录。固定scheduler tick也走受控事件入口；不是新增renderer任意事件API。

原有产品清除语义仍须区分：删除会话不等同删除保存的成果；解绑只清绑定；paused/completed是状态，不是删除。当前事项/成果没有通用DELETE路由，旧ontology purge不能被描述为清空全部 `work_*`。用户复制/导出的PC文件也不属于盒端可直接清除范围。

此前规划的事项/成果列表及历史新cursor分页、稳定业务回放改造、清除预览/执行/回执仍是独立增量，**当前源仍是原列表/history合同与 `WORK_REVISION_CONFLICT`，不可在文档中宣布这些拟API已上线**。170项catalog覆盖原可达功能，未凭空增加这些新接口。当前响应超过catalog预算应报错，不能静默截断历史正文；后续有界分页需同时更新源路由、store、catalog和页面。

## 6. 部署、升级与正式验收

环境变量与部署顺序见[集成方案第5节](INTEGRATION-0905.md#5-部署配置与顺序)。发布单元包括DE gateway/capabilities、固定版本知君backend领域代码、共享catalog、Agent和Admin新应用；不能只复制几张同名表或只改原server路由。

空库启动不读写用户原数据。若将来迁移旧资料/本体，需先停写和worker，使用SQLite backup或同一停写点的一致性快照，覆盖相关库/WAL、原件、版本引用、投影、索引和授权；备份不提交Git。升级失败应恢复匹配代码、配置与整组数据，禁止只回退代码保留不兼容schema，也不能用旧快照静默覆盖新写入。

正式验收仍需：真实建档→聊天→确认理解→判断/复盘→事项/成果→附件版本→资料来源事件；同盒多账号/所有权代次、同账号多client、撤销、并发目录锁、进程重启、超过60秒流、模型外发同意/拒绝与真实转写。永久清除只用隔离验收数据。独立本地测试不替代这些UI/SDK/盒端证据。

历史v1空资料页/一次重连见[正式接入记录](M0-PRODUCTION-0906.md)；线上连接FD热修见[故障报告](../reports/CONNECTIVITY-FD-HOTFIX-0906.md)，与新领域上线验收分开记录。

### 硬件反馈增量

真实盒端PDF/DOCX/OCR与合成WAV voice API已有通过证据；内联快照原先缺少正文SHA使privacy门禁拒绝，之后又发现DeletionStore连接释放问题与小模型evidence_invalid；修复后最新资料相关范围189项通过，hardware-candidate5为5/5，safe material正文82、摘要43字符、实体2、关系0。知识首次失败来自包dispatch公开函数被同名子模块覆盖，修复并fresh subprocess回归后，gateway-candidate6为10/10、60请求/21个completed操作，知识CRUD/confirm/search/purge通过。全部为隔离真盒合成主体/输入，正式Consumer/UI/SDK领域闭环仍待验收。具体缺陷与证据见[审核报告](../reports/FULL-PRODUCT-GATEWAY-REVIEW-0906.md)和[硬件报告](../reports/FULL-PRODUCT-HARDWARE-0906.md)；不把这些局部结果回写为旧global迁移或完整UI成功。
