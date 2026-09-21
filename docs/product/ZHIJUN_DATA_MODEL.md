# 知君数据模型规格（PRD 附录 A）

## 0. 引用基线

本文的代码事实以 `redesign/memory-v3`（4ad7d39，已推送至 GitHub 待合并）为基线；本文所在分支 `newzhijun` 仅含文档，从 `main`（98c2331）开出；main 尚未包含核心画像、求知引擎、主动机制等模块。

文中凡是描述「今天的实现」的句子都带 `文件:行`，行号一律相对 4ad7d39。凡是本文提出的新设计，一律标注「主张」或「目标态」。

### 0.1 main 与基线的差异

读者如果在 `main`（98c2331）上对照代码，以下内容会找不到，属正常：

| 对象 | `redesign/memory-v3` 4ad7d39 | `main` 98c2331 |
|---|---|---|
| `backend/mindos/zhijun/core_profile.py`（核心画像） | 存在，349 行 | 不存在，只剩 `__pycache__` 里的 `.pyc` |
| `memory_retrieval.py` 的 `GATE` / `ANCHOR_GATE` / `MAX_BOOST` / 成熟度加成 | `:80-83`、`:101-124` | 无此常量与函数 |
| `claims.why_it_matters` / `claims.how_to_apply` 列 | 迁移 `:422-425` | 无，迁移只补 3 列 |
| `claims` 列数 | 29 + 5 = 34 | 29 + 3 = 32 |
| `PREDICATES` 条数 | 21（`ways` 含 `wants_zhijun_to`） | 20，全仓无 `wants_zhijun_to` |
| `JOB_KINDS` 条数 | 14（含 `core_profile`、`proactive_scan`） | 12 |
| `transition` / `system_retract` / `purge_all` 行号 | 1164 / 1645 / 1884 | 1146 / 1624 / 1863 |

---

## 1. 文档定位

知君是**一个让用户直面内心的产品**，形态是可以在任何一台电脑上独立运行的软件，内核是一份属于用户自己的理解库。本文是产品重设计 PRD 的附录 A，只规定数据层：存什么、谁能写、怎么检索、什么能出门、怎么删、怎么带走。

**2026-09-21 定位修正对本文的影响**：新增 `burdens`（心里的事）与 `self_view`（我眼中的我）两个分区与 6 个谓词（4.4）、内观分区的抽取与张力规则（4.9）、「模型可见」与「可带走」两种出门的区分（6.3）、第五条「永不」（6.5）。其余部分不变——证据纪律、信任状态、状态机、统一出门规则当初就是围绕「认识一个人」建的，在新定位下只增不改。MCP 从「主要对外产出」降为一项延伸能力。

### 1.1 四条产品前提（本文不得与之矛盾）

| 编号 | 前提 |
|---|---|
| P1 | 独立软件，一个数据文件夹，无账号，可备份、可拷走、可删除 |
| P2 | 资料（导入文档）只作为理解你的证据，不做文档问答；搜索框搜的是「你的理解与判断」，资料以出处形式出现 |
| P3 | MCP 按分区默认可读：我是谁 / 原则 / 做法 / 方向 默认可读；重要的人 / 正在做的事 默认关；**心里的事 / 我眼中的我、敏感与受限永不外发且无开关** |
| P4 | 目标收敛到 6 类一等对象、约 15 张表、约 60 条路由 |
| P5 | **知君是让用户直面内心的产品，不是提效工具**（2026-09-21 定位修正）。数据层必须记得下内心，并且必须保证内心不会流向其他 AI |

### 1.2 数据层的七条硬约束

**编号独立于 PRD 第 3 节的七条产品原则，两者不是同一份清单。** 这里是那些原则在数据层的可执行形式，右列给出对应关系。

| # | 数据层约束 | 对应 PRD 原则 |
|---|---|---|
| C1 | 凡断言必有出处 | 原则 2 |
| C2 | 只有用户能确认 | 原则 3 |
| C3 | 撤回的永不回流 | 原则 3 |
| C4 | 出门要打招呼并留回执 | 原则 4 |
| C5 | 清空即清空 | 原则 1 |
| **C6** | **袒露不立刻成为素材**：重话推迟抽取，危机对话完全不抽取 | 原则 6、7（PRD 5.5） |
| **C7** | **内心不外流**：`burdens` 与 `self_view` 永不进入任何到达其他 AI 或其他软件的通道，没有开关（第 6 节） | 原则 7；对应 MCP 契约 I6 |

C6、C7 是 2026-09-21 定位修正新增的。它们是「用户肯说真话」这件事在数据层的代价：一个会把最脆弱的话记成档案、或者可能把它交给另一个 AI 的系统，不配被袒露心声。

### 1.3 与其它文档的关系

| 文档 | 关系 |
|---|---|
| `ZHIJUN_PRD_V2.md` | 本文是它的附录 A。产品行为以 PRD 为准，数据层字段与约束以本文为准 |
| `ZHIJUN_MCP_CONTRACT.md` | 配套。MCP 能读到什么，由本文第 6 节的 `可带走()` 唯一决定；契约文档只描述工具签名与回执格式 |
| V3 记忆系统设计文档 | 本文**取代**其中关于 claim 字段、状态机、导出规则的部分。V3 文档声称的「`how_to_apply` 无条件注入」未实现，见第 4.2 节 |
| `docs/product/PRODUCT.md` | 本文**补充**它的数据层部分，不改其产品叙述 |

---

## 2. 五层模型

数据分五层。**只有 L0 与 L1 是事实源，L2 与 L3 全部可从 L0 与 L1 重建，L4 是对前四层的检验记录。**（目标态）

| 层 | 名称 | 内容 | 谁能写 | 可否重建 | 删除时怎么办 |
|---|---|---|---|---|---|
| L0 | 原始 | 对话消息、资料原件与解析快照、用户亲手写下的判断 | 用户；导入通路。**模型永不能改写 L0** | 否。唯一事实源，不可变，带时间戳 | 走三段式删除（第 7 节）。删除会级联影响 L1 的证据行 |
| L1 | 理解 | claim（理解）、实体、证据、复核事件 | 抽取器只能提议 `working`；状态迁移只有用户能触发（`ontology_store.py:1164-1393`）；唯一例外见第 4.4 节 | 否。用户的确认行为本身就是事实，不可再生成 | 打墓碑不删行，`retracted` / `superseded` 永不复活 |
| L2 | 结构 | 实体归并结果、矛盾对、晋升标记、主题聚类 | 整合器（`consolidate.py`）；不改已确认理解，只产出待裁决候选 | 是。重跑 `consolidate.run` 即可 | 直接丢弃重算 |
| L3 | 叙事 | 核心画像、USER.md 投影、上下文包、对话摘要、今日页 | 派生流程，纯函数 | 是。有输入哈希（`core_profile.py:150-170`） | 直接丢弃重算，无需确认 |
| L4 | 检验 | 复核事件、出门回执、判断的事前预期与事后结果 | 系统自动追加，只增不改 | 否。审计证据 | 随本体清空一起清空，不单独删 |

### 2.1 一条硬规则（主张）

**L3 永远不能成为 L1 的输入。** 画像里出现过的句子不能反过来作为新理解的证据，否则模型会给自己的猜测背书。今天 `core_profile.prompt_block:322-349` 已经只输出文本与 `claimIds`，没有回写通路，目标态要把这条写进契约而不是靠惯例。

---

## 3. 六类一等对象

目标态只保留六类一等对象。其余一律降级为属性、内部表或下线。

| 对象 | 一句话定义 | 关键字段 | 生命周期 | 与其它对象的关系 | 用户在界面上怎么看见 |
|---|---|---|---|---|---|
| **理解** claim | 知君对「你是谁、你怎么想、你在做什么」的一条可被确认或撤回的断言 | `content`（≤120 字）、`section`、`layer`、`trust_state`、`predicate`、`confidence`、`privacy`、`带走开关` | working → confirmed / context_only / rejected / deferred；confirmed → retracted / superseded | 主语与宾语指向实体；由证据支撑；被复核事件驱动 | 「我的理解」页按八分区分组（`burdens` / `self_view` 单独成组并标注「只有你和知君看得到」），每条带来源标签与「确认 / 改一改 / 不对 / 以后再说」 |
| **实体** entity | 一个被反复提到的人、组织、项目、地点、主题、事件或术语 | `id`、`type`（8 种）、`canonical_name`、别名表 | 由抽取创建；合并需用户裁决（`consolidate.py` 只产候选） | 作为 claim 的主语或宾语；`ent_me` 是唯一的「我」 | 在理解条目里作为可点的名字；合并提示出现在整理页 |
| **资料** material | 你导入的一份文档原件及其解析快照 | `material_id`、`file_name`、`version_number`、版本链、`snapshot_id` | 导入 → 解析 → 抽取 → 可回收 → 可永久清除 | **只作为 claim 的证据出现**，不参与搜索结果的第一层 | 理解条目下方的「出处」链接；点开跳到原文对应位置 |
| **对话** conversation | 你和知君的一次连续交谈及其消息 | `conversation_id`、消息列表、摘要 | 持续追加；可归档；随清空一起清空 | 作为 claim 的证据（`conversation_turn`）；提供上下文 | 会话列表与聊天页 |
| **判断** decision | 你在做某个决定时写下的事前预期，以及事后回看的结果 | `decision_id`、预期、结果、复盘、**事前预期字段**（见 3.1） | 开放 → 已记录结果 → 已复盘 | 可作为 claim 的证据（`kind='decision'`）；可引用 claim 作为依据 | 判断簿页；到期时在今日页提醒回访 |
| **画像** profile | 由已确认理解投影出来的、随时可重建的一段稳定描述 | 行列表、`source_hash`、预算、外发标记 | 纯派生，输入变则失效重算 | 输入是 claim + 事项 + 摘要 + 判断；输出进提示词或导出文件 | 「知君眼中的我」页面；可一键导出 |

### 3.1 折叠对照表：现状 23 类对象如何收敛到 6 类

| 现状对象 | 今天在哪 | 目标态归宿 |
|---|---|---|
| claim（理解） | `ontology_store.py:121-151` | **保留为一等：理解** |
| entity / entity_aliases | `ontology_store.py` | **保留为一等：实体** |
| claim_evidence | `ontology_store.py` | 理解的子结构，不独立成对象 |
| review_events | `ontology_store.py` | 理解的历史，界面上是时间线 |
| 自我校准 self_alignment | `alignment_store`，`claims.self_alignment_json`（迁移 `:417-418`） | **折叠为 claim 属性**，不再是独立对象与 3 张表 |
| 成长章程 growth_charters | `growth_store.py:25-36` | **折叠为「已发布」标记的 claim 集合 + 一次版本快照**；章程正文不再是独立表 |
| 章程草稿 charter_drafts 等 5 表 | `charter_draft_store.py` | 内部草稿状态，不是一等对象 |
| charter_exceptions | ontology.db | 折叠为 claim 的例外说明字段 |
| 照见 reflections / reflection_reviews | `reflection_store.py` | **折叠为 `layer='hypothesis'` 的 claim + 跨时间标记**，不再是 2 张表 |
| 相处方式 | 今天散落在 persona 与提示词 | **折叠为 `ways` 分区的 claim**，谓词 `prefers` / `tends_to` / `decides_by` / `wants_zhijun_to`（`ontology_store.py:61-68`） |
| 学习片段 learning_episodes | `learning_store.py`，1 表 | **折叠为判断的事前预期字段**，不再独立 |
| 事项 work_matters 等 4 表 | `matters_store.py:15-42`，文件头 `:1-4` 明确「These records are not Claims」 | **保留为一等但与 `claims.matters` 划界**，见 3.2 |
| 知识卡片 card_* 9 表 | `card_ledger_store.py` | **下线**。今天 `search.py:180-205` 的 `unified_search` 返回知识卡片 / 资料 / 视觉三类，claims 完全不参与搜索，与 P2 直接冲突 |
| memory_admissions / memory_attention / memory_drafts / memory_policy | `memory_store.py`，4 表 | 内部缓存，降为进程内或单表 |
| routing_* 11 表 | `routing_store.py` | **删除**（单人本地无多 scope） |
| connectivity_* 7 表 | `connectivity_store.py` | **删除** |
| chat_import_* 5 表 | `chat_import_store.py` | 折叠进资料导入通路 |
| alignment_grants / chat_material_grants / chat_material_privacy | 多处 | **删除**，由第 6 节的分区默认 + 逐条开关取代 |
| job_records / material_jobs / material_content_snapshots | `job_store.py:67-84`、`material_pipeline_store.py:85,110` | 折叠为资料的三个子表 |
| document_parts / derived_records | `derived_store.py:85-124`，kind 六种见 `derived.py:45-54` | 折叠为资料的解析产物 |
| Chroma 向量 | `backend/vector_store.py` | 保留为资料的检索索引，不进入本体 |
| conversation / messages / summaries | `conversation_store.py`，7 表 | **保留为一等：对话** |
| growth_decisions / growth_reviews | `growth_store.py` | **保留为一等：判断** |

### 3.2 事项与理解的划界（主张）

两者今天在语义上重叠：`work_matters` 是用户手写可编辑的项目卡，`claims.matters` 是抽取出来的「正在做的事」。目标态的界线是：

| 维度 | 事项 matter | `claims.matters` |
|---|---|---|
| 谁写 | 用户手写，随时可编辑 | 抽取提议，用户确认 |
| 可变性 | 可反复改，改了就是改了 | 改一条等于新建 + 旧条 superseded |
| 有无证据 | 不要求 | 必须有 |
| 能否出门 | 默认不能（P3：正在做的事默认关） | 默认不能 |
| 界面 | 看板 / 清单 | 本体页的一个分区 |

一条规则：**事项可以引用理解作为背景，理解不能把事项当证据。** 否则用户手写的临时清单会变成模型的断言依据。

---

## 4. 理解（claim）规格

### 4.1 字段表

今天 `claims` 有 34 列（DDL 29 列 `ontology_store.py:121-151`，迁移补 5 列 `:415-425`）。目标态如下：

| 字段 | 今天 | 目标态 | 说明 |
|---|---|---|---|
| `id` | 有 | 保留 | |
| `subject_entity_id` / `object_entity_id` | 有 | 保留 | 主语默认 `ent_me` |
| `predicate` | 有 | 保留 | 受控，按分区封闭 |
| `content` | 有，≤120 字（`:810-811`） | 保留，维持 120 字上限 | 上限逼迫一条只说一件事 |
| `section` | 有，6 值 | 保留 | 出门规则的分区默认依赖它 |
| `self_model_layer` | 有，4 值 | 保留，改名 `layer` | |
| `trust_state` | 有，4 值 | 保留 | |
| `trust_origin` | 有，6 值 | 保留 | |
| `confidence` | 有 | 保留，但**不参与出门判定**（见 4.6） | |
| `scope` | 有，`long_term` / `context_only` | 保留 | |
| `context_ref` | 有 | 保留 | |
| `privacy_level` | 有，4 值 | 保留 | |
| `export_allowed` INTEGER | 有，两态 | **改为三态 `takeaway`：`on` / `off` / `unset`** | 两态区分不了「用户明确关掉」与「从没设过」，这正是今天 MCP 的歧义源头（第 6.3 节） |
| `valid_from` / `valid_to` | 有 | 保留 | |
| `challenged` / `challenge_note` | 有 | 保留 | |
| `deferred_until` | 有 | 保留，`DEFER_DAYS = 14`（`:93`） | |
| `first_seen` / `last_reaffirmed` | 有 | 保留 | |
| `supersedes_id` / `superseded_by_id` | 有 | 保留 | |
| `retracted_at` / `retraction_reason` | 有 | 保留 | |
| `content_hash` | 有 | 保留 | 唯一约束依赖它 |
| `created_at` / `updated_at` | 有 | 保留 | |
| `promotion_ready` | 迁移 `:415-416` | 保留 | |
| `self_alignment_json` | 迁移 `:417-418` | 保留，折叠自我校准 | |
| `context_json` | 迁移 `:419-420` | 保留 | |
| `why_it_matters` | 迁移 `:422-423`，≤120 字（`:826`） | 保留 | 抽取器会写（`extract.py:726`），检索加成会读（`memory_retrieval.py:113`） |
| `how_to_apply` | 迁移 `:424-425`，校验 `:828` | **主张删除**，理由见 4.2 | |
| `device_scope` | DDL 有，默认 `'global'` | **明确删除** | 见 4.3 |

### 4.2 `how_to_apply` 的去留（主张：删）

列和校验都在（`ontology_store.py:824-828`、写入 `:845`、读出 `:756`），但：

1. **写侧没有人写**：`extract.py:726` 只写 `why_it_matters`；全仓 grep 显示 `how_to_apply` / `howToApply` 只出现在 `ontology_store.py` 与测试里，抽取、资料通路、API 路由都不传它。
2. **读侧没有人读**：核心画像的 `_claim_line`（`core_profile.py:121-129`）与 `projection._line`（`projection.py:29-33`）都不渲染它，`memory_retrieval.maturity_boost:101-124` 也只看 `whyItMatters`。
3. V3 文档声称的「无条件注入」未实现。

**主张**：删列，把「这条该怎么用」并进 `why_it_matters` 的语义（它本来就要求「具体说明未来哪类帮助会因这条内容而改变」，见 `extract.py:112`）。保留为未决项，见第 10 节。

### 4.3 删 `device_scope`（主张）

`device_scope` 在 `backend/mindos/` 下出现 **661 处、539 行、47 个文件**。它服务的是「哪个设备 / 哪个 scope 授权哪个服务读哪个版本」这套多工作区模型。独立单人软件里不存在第二个 scope，全部删除，连带删掉 `routing_*` 11 张表、`connectivity_*` 7 张表、`alignment_grants`、`chat_material_grants`、`chat_material_privacy`，共 **21 张授权相关表**。

### 4.4 受控词表

| 词表 | 值 | 位置 |
|---|---|---|
| `SECTIONS` 6 → **8** | `who` `people` `matters` `principles` `ways` `direction` **+ `burdens` `self_view`** | `:37` |
| `LAYERS` 4 | `observed` `self_declared` `aspirational` `hypothesis` | `:38` |
| `TRUST_STATES` 4 | `working` `confirmed` `retracted` `superseded` | `:39` |
| `TRUST_ORIGINS` 6 | `utterance` `user_confirm` `user_edit` `user_created` `material` `model` | `:40` |
| `ENTITY_TYPES` 8 | `me` `person` `organization` `project` `place` `topic` `event` `term` | `:41` |
| `PRIVACY_LEVELS` 4 | `public` `private` `sensitive` `restricted` | `:42` |
| `SCOPES` 2 | `long_term` `context_only` | `:43` |
| `EVIDENCE_KINDS` 5 | `conversation_turn` `material_span` `user_edit` `decision` `review` | `:44` |
| `STANCES` 3 | `supports` `contradicts` `background` | `:45` |
| `REVIEW_ACTIONS` 8 | `confirm` `partial` `context_only` `reject` `defer` `retract` `reaffirm` `create` | `:46-55` |
| `SURFACES` 7 | `conversation` `ontology_page` `onboarding` `today` `decision_panel` `import` `system` | `:56` |
| `JOB_KINDS` 14 | 含 `core_profile`、`proactive_scan` | `:57` |
| `DEFER_DAYS` | 14 | `:93` |

`PREDICATES`（`:61-67`）现有 21 个，**目标态 27 个**，**按分区封闭，越界整条丢弃**（校验 `:804-806`）：

| 分区 | 谓词 | 状态 |
|---|---|---|
| who 我是谁 | `is` `has_trait` `background` `role` | 现有 |
| people 重要的人 | `knows` `works_with` `relationship` `attitude_toward` | 现有 |
| matters 正在做的事 | `working_on` `committed_to` `happened` `owns` | 现有 |
| principles 我的原则 | `holds_principle` `boundary` | 现有 |
| ways 相处方式 | `prefers` `tends_to` `decides_by` `wants_zhijun_to` | 现有 |
| direction 我要去哪 | `wants_to` `goal` `avoids` | 现有 |
| **burdens 心里的事** | `weighs_on` 压在心里 / `drains` 消耗 / `avoids_facing` 在回避 / `worries_about` 担心 | **新增 4** |
| **self_view 我眼中的我** | `sees_self_as` 自认为 / `blames_self_for` 自责 | **新增 2** |

`avoids_facing` 与 direction 的 `avoids` 有意用不同的词：后者是「不想要的方向」（我不想做管理），前者是「知道该做但在躲」（我知道该和他谈）。混用会让张力检测失效。

**落地时要同时改的五处**（行号相对基线，均在 `ontology_store.py`）：

| 位置 | 改什么 |
|---|---|
| `SECTIONS:37` | 元组加两个值 |
| `PREDICATES:61-67` | 加两个分区各自的谓词元组 |
| `DEFAULT_PREDICATE:69-76` | 加两个默认谓词（建议 `weighs_on` / `sees_self_as`），否则不传 predicate 的调用会 `KeyError` |
| `SECTION_TITLES:78-85` | 加界面名「心里的事」「我眼中的我」 |
| `claims` 表 DDL `:127` 的 `CHECK(section IN (...))` | 加两个值，并给已建库写迁移。**这是唯一一处会让旧库写入直接失败的地方** |

分区越界整条丢弃的校验在 `:804-806`，逻辑不用改——加了词表它自然生效。

分层在界面上的措辞由 `LAYER_TITLES:86-91` 决定：`self_declared` = 你告诉我的，`observed` = 资料里看到的，`hypothesis` = 我推测的，`aspirational` = 你想成为的。**新增两个分区的默认层**：`burdens` 多为 `observed`（从反复出现中看出）或 `self_declared`；`self_view` 几乎总是 `self_declared`，**不接受 `hypothesis`**——替用户推断他怎么看自己，越界且几乎必错。

词表的价值在于封闭：模型不能发明新谓词，越界的整条被丢掉而不是降级保存。**除上述 8 分区 / 27 谓词的扩展外，目标态维持这份词表不变。**

### 4.5 状态机

唯一入口是 `OntologyStore.transition`（`:1164-1393`）。任何自动流程都不能绕过它改 `trust_state`。

文字状态图：

```
（抽取/导入）──► working ──confirm──► confirmed ──retract──► retracted ■
                   │                    │
                   ├─context_only──► working(scope=context_only)
                   ├─reject────────► retracted ■
                   ├─defer─────────► working(deferred_until=+14d)
                   └─partial──┐     └─reaffirm──► confirmed（刷新 last_reaffirmed）
   confirmed ──partial──┐     │
                        ▼     ▼
              新建一条 confirmed（origin=user_edit）+ 旧条 superseded ■
```

转移表：

| 动作 | 合法前置 | 结果 | 行 |
|---|---|---|---|
| `confirm` | 仅 `working` | `confirmed`，清 `challenged` / `deferred_until` | `:1191-1192` |
| `context_only` | 仅 `working` | 保持 `working`，`scope='context_only'` | `:1198-1199` |
| `reject` | 仅 `working` | `retracted` | `:1207-1208` |
| `defer` | 仅 `working` | `deferred_until` = 今天 + 14 天 | `:1214-1216` |
| `retract` | 仅 `confirmed` | `retracted` | `:1223-1224` |
| `reaffirm` | 仅 `confirmed` | 刷新 `last_reaffirmed` | `:1230-1231` |
| `partial` | `working` 或 `confirmed` | 新建一条 `confirmed`（`origin='user_edit'`，`confidence=1.0`），旧条 `superseded` | `:1236-1237` |

三条必须写进契约的性质：

1. **墓碑永不复活。** `retracted` 与 `superseded` 不是任何动作的合法前置（把上表的「合法前置」列取并集即可验证）。被撤回的理解不会因为后续对话重新说了一遍就回到回答里。
2. **`partial` 会搬家。** 旧证据逐条复制到新条（`:1305-1330`），`hypothesis` 在编辑时自动升为 `self_declared`（`:1283`），因为用户既然动手改了，它就不再是推测。
3. **每次迁移都留痕。** 写一条 `review_events`（`before` / `after` / `actor` / `surface`，`:1351-1362`）并 bump revision（`:1364`）。

### 4.6 证据模型

| 字段 | 说明 |
|---|---|
| `kind` | 5 种（`:41`） |
| `stance` | `supports` / `contradicts` / `background` |
| `conversation_id` / `message_id` | 对话证据锚点 |
| `material_id` / `chunk_key` / `locator_json` | 资料证据锚点 |
| `decision_id` | 判断证据锚点 |
| `quote` | 原话片段，资料证据 ≤300 字 |

资料证据的锚点形状由 `context_sources.py:256-258` 决定：`chunkKey = f"{materialId}::snapshot:{snapshotId}:{offset}"`，`locator = {"kind":"text","offset":..,"length":..}`。目标态维持不变，因为它是「点出处能跳回原文」的唯一依据。

**`add_evidence(reaffirm=)`（`:1038-1057`）不经状态机**，只加证据行、刷新 `last_reaffirmed`。这是正确的：多一条出处不等于用户确认了。

### 4.7 唯一强约束

```sql
CREATE UNIQUE INDEX ux_claims_active_hash
    ON claims(content_hash) WHERE trust_state IN ('working','confirmed');
```

见 `:152-156`，冲突抛 `OntologyConflictError`（`:1016-1017`）。含义：**活跃态内容哈希唯一**，同一句话不会以两条活跃理解的形式存在，但墓碑可以重复。目标态保留。

### 4.8 置信与成熟度

`confidence` 是模型的自评，**不参与出门判定，也不参与检索闸门**。真正表达「这条有多站得住」的是成熟度：

| 信号 | 来源 |
|---|---|
| 独立来源数 | `evidence_source_count:1677-1691`，口径 `material_id > decision_id > conversation_id > kind` |
| `promotion_ready` | `consolidate.py:234-237`，`evidence_source_count ≥ 2` 时置位 |
| `why_it_matters` 是否填了 | 检索加成 `memory_retrieval.py:113` |
| 最近是否重申 | `last_reaffirmed` |

### 4.9 内观分区的抽取与张力规则（2026-09-21 新增）

`burdens`（心里的事）与 `self_view`（我眼中的我）不是「多记两类东西」，它们的用处是**产生张力**，而张力是照见的唯一来源（PRD 场景 S3）。规则相应地和其它分区不同。

**抽取必须格外克制**

| 分区 | 成为候选的条件 | 禁止 |
|---|---|---|
| `burdens` | **同一件事在不同对话里被提及两次以上**。第一次只在本地记一个计数，不产生 claim | 把一次抱怨、一次疲惫记成长期困扰 |
| `self_view` | 只接受用户**明确的自我评价原话**（「我这个人就是不够狠」） | 任何推断。`self_view` 不接受 `layer=hypothesis`（4.4） |
| 两者 | 危机对话完全不抽取（PRD 5.5）；重话所在的那一轮默认推迟抽取 | 把袒露当素材 |

误判两个方向的代价不对称：**漏记一条困扰只是少一条理解，错记一条会让用户下次不敢说。** 所有阈值向「宁可不记」倾斜。

**两种张力**

```
张力A（言行不一）= self_view 的一条 self_declared
                 ⟂ ways / matters 里 layer=observed 的记录
  例：sees_self_as「果断」 vs observed「三次决定各拖了一个月以上」

张力B（说了没动）= 同一条 burdens 被重申 ≥3 次
                 ∧ 时间跨度 ≥ 30 天
                 ∧ 相关实体上没有任何 matters / decision 进展
  例：avoids_facing「和林岚那次谈话」被提及三次，跨度六周，无任何相关判断或事项
```

两者都产出 `layer=hypothesis` 的候选，走现有求知引擎的 `tension` 目标（`inquiry.py:43,152-164`），受同一套 7 天冷却与安静领域过滤约束。

**今天的张力检测只覆盖一种组合**：`consolidate.py:153-156` 取 confirmed 的 `principles`，与 7 天内 confirmed 的 `ways` / `matters` 配对，判定为矛盾时产出 `principle_tension`（`:168,181-190`）。本次是把输入扩到上面两类，检测结构不变。

**张力的呈现规则**：第一句永远是观察，不是评价。给出证据（哪几次、什么时候），不给结论。用户可以说「不对」，说了就按现有纠正通路处理，并且这条张力在同一对象上进入更长的冷却。

**有效期**：`burdens` 的困扰会过去，而现有衰减参数是按原则、做法这类稳定内容调的。目标态给 `burdens` 显著更短的默认有效期，具体值待真实数据（第 10 节未决事项）。

---

## 5. 检索与组装

### 5.1 本体是词面检索，不是向量库

`ontology_store.py:8-9` 的文件头已经把理由写清楚：「检索用 jieba 词面重叠 + 时间衰减（个人本体量级 ≤ 1e4 条，不需要向量库）；缺 jieba 时退化为 CJK 字符二元组」。

佐证：

- `ontology.db` 里没有 embeddings 表。
- claim 向量只存在进程内存 `memory_index.CACHE`（`memory_index.py:16`，一个 `OrderedDict`），进程重启即失。
- 向量**只作兜底**：必须 `embedding ≥ .70` 且 `embedding × .5 > 词面分` 才接管（`memory_retrieval.py:311-316`）。

**目标态明确写死这条：个人本体规模 ≤ 1e4 条，词面 + 时间衰减够用；向量只服务资料检索，不进入本体。** 这直接支撑 P1（一个数据文件夹，可拷走），因为没有向量库就没有需要重建的索引。

### 5.2 检索数字

| 参数 | 值 | 位置 |
|---|---|---|
| `GATE` | .12 | `memory_retrieval.py:80` |
| `ANCHOR_GATE` | .08（people / principles 分区放宽，每区最多 1 条） | `:81` |
| `MAX_BOOST` | .12 | `:82` |
| `CORRECTION_SIMILARITY` | .35 | `:83` |
| 基础打分 | `max(direct, .8 × contextual)` | `:289-303` |
| 主题交集 | +.22 | `:292`、`:295` |
| 实体名精确命中 | +.25 | `:301-302` |

成熟度加成（`:101-124`，夹在 `[-.03, .12]`）：

| 信号 | 加成 |
|---|---|
| 独立来源数 | +.03 × min(3, n-1) |
| `promotionReady` | +.04 |
| 有 `whyItMatters` | +.03 |
| 30 天内重申 | +.05 |
| 90 天内重申 | +.02 |
| 超过 365 天未重申且单来源 | -.03 |

**关键约束**：保留未加成的 `relevance` 做阈值判断，加成后的 `score` 只做排序（`:329-334`）。加成只改变已相关候选的先后，永不把不相关的放进来。

其它口径：

- `confirmed_background`（`:222-244`）只取 `who` 分区的 `is` / `role` / `background`，默认 4 条 / 600 字。这是身份与角色，不是性格预测。
- self-overview 走六分区轮询（`:267-278`），避免一个活跃分区把小分区挤掉。**目标态扩到八分区**，但 `burdens` / `self_view` 在轮询里权重减半：内观分区贵在准不贵在多，让它们和其它分区等权会挤占本来就紧的画像预算（5.4）。

### 5.3 两套打分必须合一（主张）

今天有两套互不相干的打分：

| 实现 | 算法 | 用途 |
|---|---|---|
| `memory_retrieval` | 词面 + 主题 + 实体 + 成熟度加成，`GATE=.12` | 对话上下文组装 |
| `ontology_store.search_claims:1119-1151` | `lexical_similarity + 0.05 × exp(-days/30)`，`k=12` | 本体页搜索、去重 |

**主张**：以 `memory_retrieval` 的口径为准，`search_claims` 退化为它的一个调用形式（关掉成熟度加成、放开闸门）。理由是用户在搜索框看到的排序必须和知君在对话里想起来的顺序一致，否则「为什么它记得这个却搜不到」会变成反复的信任事故。

同时，P2 要求搜索框搜的是「你的理解与判断」。今天 `search.py:180-205` 的 `unified_search` 返回 `knowledge` / `materials` / `unavailableMaterials` / `visualMaterials` 四类，**claims 完全不参与搜索**。目标态必须反过来：第一层结果是理解与判断，资料只作为每条结果下面的出处出现。

### 5.4 画像生成规则

| 规则 | 值 | 位置 |
|---|---|---|
| 预算 | 1200 字（外发）/ 600 字（本地） | `core_profile.py:21-23` |
| 分区上限 | who 4、people 3、principles 4、ways 3、direction 3 | `:39` |
| **新增分区上限（目标态）** | **burdens 2、self_view 2**。刻意压低：内观分区贵在准，不贵在多，而且每一条都占用用户读画像时最敏感的注意力 | 新增 |
| **新增分区的外发** | **只进 `模型可见()`，永不进 `可带走()`**（6.3）。导出包与 MCP 拿到的画像里这两段根本不渲染 | 新增 |
| 事项上限 | matter 2 + committed_to 3 + working_on 2 | `:40` |
| 近期上限 | 3 主题 / 4 待办 / 2 到期 | `:41` |
| 最近对话数 | 3 | `:42` |
| 到期天数 | 7 | `:43` |
| 丢弃顺序 | recent → direction → ways → people → principles → matters → who | `:38` |
| **新增丢弃顺序（目标态）** | recent → direction → ways → people → **burdens** → principles → matters → **self_view** → who。`self_view` 排得靠后，因为它是张力检测的锚，掉了就照见不出来；`burdens` 排中间，因为单条困扰的时效性强于原则 | 新增 |
| 第一轮保底 | 每段至少留 1 行 | `:249-259` |
| 排序 | `(-来源数, -lastReaffirmed, id)` | `:84-87` |
| 外发过滤 | `externalOk = privacy ∈ (public,private) ∧ not claim_local` | `:126`、`:262-268` |
| 未授权行处理 | 只丢不阻塞对话 | `:322-349` |

**`source_hash` 不含当天日期**（`:150-170`），只签名理解 / 事项 / 摘要 / 判断。这条很重要：画像不会因为「今天是新的一天」而失效重算，只有输入真的变了才重算。目标态保留。

### 5.5 上下文预算

| 参数 | 值 | 位置 |
|---|---|---|
| 证据条数 | `12 if (complex or queries) else 8` | `context_plan.py:361` |
| 同一资料最多 | 2 段 | `:367-369` |
| 事项最多 | 3 条 | `:364-366` |
| 去重相似度 | .72 | `:374` |
| 推测每轮 | ≤1 条 | `:378-381` |
| 授权询问门槛 | .45 | `:403-410` |
| 提示词字符上限 | 6000（外部）/ 1800（本地） | `:423` |

---

## 6. 统一出门规则

这是本文最重要的一章。今天有 **4 个出门通道、3 套互相矛盾的规则**。

### 6.1 现状：4 个通道，3 套规则

| # | 通道 | 实现 | 判定 |
|---|---|---|---|
| 1 | 界面导出 | `mindos/ontology.py:377-387`（注册 `:417`） | **完全不检查 `exportAllowed`**。`confirmed` 全导，包含 `sensitive` 与 `restricted` |
| 2 | 画像投影 | `projection.render:67` | `exportAllowed ∧ privacy ∈ (public,private) ∧ scope != context_only ∧ not claim_local` |
| 3 | 上下文包 | `context_pack.exportable_claims:24-34` | 与 #2 **完全一致** |
| 4 | MCP | `zhijun_mcp/personal.py:44-84` | **不看 `exportAllowed`**。只把历史上有 `note=='导出开关'` 且 `after.exportAllowed is False` 的复核事件当作**永久拒绝**，持久化到 `denials` 表（`store.py:38`、`preserve_denial:91-93`、`denied_ids:95-97`），并沿 `supersedesId`、`evidence.locator.claimId`、`decisionId` 递归传染 |

补充两条事实：

- `OntologyStore.export_payload:1870-1882` **零调用方**，是死代码。真正活着的导出是上表的 #1。清理时直接删掉这个方法，别照着它改。
- 通道 #4 的语义是「默认放行，除非曾经被明确关掉」；通道 #2 / #3 的语义是「默认拦截，除非明确打开」。**两者对同一条从未设过开关的理解给出相反答案**，这是最危险的一处。

### 6.2 `claim_local` 把资料派生的理解全部锁死（必须解决）

`SourcePolicy.claim_local`（`source_policy.py:63-77`）在 `:74` 有这么一条：只要 claim 的任何一条证据带 `materialId`，整条判为本地专用。

后果：**今天从导入资料得出的理解永远出不去，即使用户把逐条开关打开。** 通道 #2 / #3 / #4 都调用 `claim_local`，一律拦截。

这与产品决定 P2 直接冲突。P2 说资料只作为理解你的证据，理解本身是你的；但实现说「碰过资料的理解一律不许走」。

三个目标态选项：

| 选项 | 做法 | 代价 |
|---|---|---|
| **① 推荐** | 资料派生的理解**可带走，但不带原文片段**。出门时剥掉 `quote` 与 `locator`，只保留「有 N 处出处」的计数与出处类型 | 需要在序列化层加一道剥离；对端拿不到原文，只拿到断言 |
| ② | 仅当证据**全部**来自用户原话时才可带走 | 大量真实理解出不去；用户会觉得「我明明确认过」却传不出去 |
| ③ | 维持现状，但在界面上明示「这条来自资料，不会外发」 | 诚实，但 P2 名存实亡 |

**推荐 ①。理由：资料只作为证据，理解本身是用户的。** 把文档原文关在本地是对的，把用户经过确认的判断也关在本地是过度保护。列入第 10 节未决事项。

### 6.3 两种「出门」必须分开（2026-09-21 新增，最重要的一条）

定位修正引入了一个过去不存在的区分。「出门」现在是两件不同的事，混为一谈会直接毁掉产品前提：

| | `模型可见()` | `可带走()` |
|---|---|---|
| 去哪 | **知君自己调用的模型**（BYOK 在线模型或本机模型） | **其他 AI 或其他软件**：MCP、导出包、上下文包、USER.md 投影 |
| 目的 | 让知君能当知己与导师 | 让别的工具用上这份理解 |
| `burdens` / `self_view` | **可见**（否则知君在在线模式下根本无法承担这个角色） | **永不，且无开关** |
| 用户的出路 | 切到本机模型，一个字不出这台电脑（PRD 8） | 无需出路，本来就不出去 |

这个区分必须对用户明说，不能含糊（PRD 8 节）。今天 `core_profile.py:126` 的 `externalOk` 属于第一列，第 6 节其余部分讲的全部是第二列。

### 6.4 目标态：唯一判定函数

```
可带走(c, 通道) =
    c.trust_state == confirmed
  ∧ c.privacy ∈ {public, private}
  ∧ c.section ∉ {burdens, self_view}        # 新增，无开关可绕过
  ∧ c.scope == long_term
  ∧ 取带走开关(c) == on
  ∧ ¬本地血统(c)
  ∧ 通道许可(通道, c.section)

取带走开关(c) = c.takeaway if c.takeaway != unset else 分区默认(c.section)
分区默认 = {who:on, principles:on, ways:on, direction:on,
            people:off, matters:off,
            burdens:永不, self_view:永不}      # 不是 off，是不可设置
```

```
模型可见(c) =
    c.trust_state == confirmed
  ∧ c.privacy ∈ {public, private}
  ∧ c.scope == long_term
  ∧ ¬本地血统(c)
  # 注意：不含 section 与带走开关的判断。burdens / self_view 在这里是可见的
```

四个带走通道**必须共用 `可带走()`**，不得各自实现。画像装配用 `模型可见()`。

**为什么 `burdens` / `self_view` 用硬编码的分区判断，而不是把它们的 `privacy` 默认设成 `sensitive`**：`sensitive` 是用户可以逐条改的，而这条约束不该可改。用户在某个瞬间点掉一个开关，不该导致他三个月前最脆弱的一句话流向另一个 AI。**这是唯一一条我们替用户做主的规则，理由是它保护的是产品前提本身。**

### 6.5 五条「永不」

| 规则 | 判据 | 说明 |
|---|---|---|
| **内观分区永不** | `section ∈ {burdens, self_view}` | **新增。** 无开关，不可绕过。只对 `可带走()` 生效，不影响 `模型可见()`（6.3） |
| 敏感与受限永不 | `privacy ∈ {sensitive, restricted}` | 用户打开开关也不行。开关只能在 `public` / `private` 范围内起作用 |
| 被撤回的永不 | `trust_state ∈ {retracted, superseded}` | 墓碑不回流。由状态机保证（4.5 节） |
| 仅当时情境的永不 | `scope == context_only` | 只适用于当时那件事的理解，换个场合就是误导 |
| 本地专用血统的永不 | `本地血统(c)` | 证据带 `locator.localOnly`、来自本地对话。**资料证据是否算，取决于 6.2 的选项** |

### 6.5 逐条开关的语义（主张）

今天 `set_export_allowed:1621-1639` 是两态布尔，写一条 `action='edit'`、`note='导出开关'` 的复核事件。目标态改三态：

| 状态 | 含义 | 行为 |
|---|---|---|
| `on` | 用户明确允许 | 按判定函数放行 |
| `off` | 用户明确拒绝 | **永久拒绝**，且沿 `supersedesId` 传染到后续修订版本（沿用今天 `personal.py:63-65` 的传染逻辑） |
| `unset` | 从没设过 | 取分区默认 |

`off` 的永久性来自原则 3：用户说过不的东西，不该因为换了个通道或者改了一次措辞就重新放出去。今天 MCP 已经这么做了（持久化到 `denials` 表），目标态是把这个行为提到判定函数里，让四个通道都遵守，而不是只有 MCP 遵守。

### 6.6 输入 → 判定 → 四通道行为

| 输入（一条理解） | `可带走()` | 界面导出 | 画像投影 | 上下文包 | MCP |
|---|---|---|---|---|---|
| who 分区，confirmed，private，unset | 是（分区默认 on） | 导出 | 进外发画像 | 进上下文 | 可读 |
| people 分区，confirmed，private，unset | 否（分区默认 off） | 不导出 | 仅本地画像 | 仅本地上下文 | 不可读 |
| people 分区，confirmed，private，**on** | 是 | 导出 | 进外发画像 | 进上下文 | 可读 |
| matters 分区，confirmed，private，unset | 否 | 不导出 | 仅本地 | 仅本地 | 不可读 |
| 任意分区，confirmed，**sensitive** | 否 | 不导出 | 不出现 | 仅本地 | 不可读 |
| 任意分区，confirmed，**restricted** | 否 | 不导出 | 不出现 | 不出现 | 不可读 |
| 任意分区，**working** | 否 | 不导出 | 不出现 | 可进本地上下文并标「待验证」 | 不可读 |
| 任意分区，**retracted / superseded** | 否 | 不导出 | 不出现 | 不出现 | 不可读 |
| 任意分区，`scope=context_only` | 否 | 不导出 | 不出现 | 仅当同一情境 | 不可读 |
| 曾被设 `off` 的任意条 | 否 | 不导出 | 不出现 | 不出现 | 不可读（永久） |
| **`burdens` / `self_view`，confirmed，private** | **否（永不，无开关）** | **不导出** | **进知君自己的模型画像** | **不出现** | **不可读（永久，无开关）** |

最后一行是定位修正带来的新行为，也是全表唯一一条「用户无法通过任何开关改变」的：它保护的是袒露本身的前提（6.3、6.4）。

两个口径说明：
- 「画像投影」这一列问的是 `模型可见()`，即这条会不会进入**知君自己调用的模型**；其余三列问的是 `可带走()`，即会不会到达**其他 AI 或其他软件**。两者在 6.3 已分开定义。
- 「不出现」与「仅本地」的区别：前者连本地提示词也不进，后者进本地模型但不进外部服务。

### 6.7 出门要留回执

每次通过 `可带走()` 放行的批次，写一条回执：时间、通道、对端标识、用途说明、放行条数、被拦条数与拦截原因。今天 `context_pack.receipt_summary` 已有雏形（`ontology.py:310-311`）。目标态统一成一张 `takeaway_receipts` 表，进第 8 节的导出包。

---

## 7. 删除、墓碑与清空

### 7.1 三段式删除

合同写在 `lifecycle.py:1-22`：

| 阶段 | 行为 | 关键约束 |
|---|---|---|
| 预览 deletion-impact | 展示全部关联影响，返回一次性 `confirmToken` | TTL 600 秒（`:54`）；**绝不返回物理路径 / 绝对路径 / 内部 artifact key** |
| 受控回收 recycle / unrecycle | 移出活跃索引，原文件进回收目录，可恢复 | 必须携带 `confirmToken` 与依赖决策；缺失、过期或不匹配返回 409 |
| 永久清除 purge | 先清向量、派生、治理，再处理物理文件 | 严格顺序（`:664-732`）；任一失败回滚并重建索引（`_restore_active_index:678-685`），禁止「原文件已删除但索引仍能命中」 |

目标态保留这个三段式，但简化依赖图（知识卡片下线后，`_detach_inactive_card_sources` 之类的分支可以删掉）。

### 7.2 资料删除对理解的影响

`detach_material:1848-1868` 的行为：

| 理解状态 | 有其它证据 | 结果 |
|---|---|---|
| `working` | 无 | `system_retract("evidence_purged")`（`:1861`） |
| `working` | 有 | 保留，删掉该资料的证据行 |
| `confirmed` | 无 | **保留，但删掉该资料的证据行** |
| `confirmed` | 有 | 保留，删掉该资料的证据行 |

**副作用必须讲清楚**：第三行的 confirmed 理解会变成一条「用户确认过但已经没有出处」的断言。它的 `evidence_source_count` 下降，直接影响检索的成熟度加成（来源数那一项 `memory_retrieval.py:106`）和画像的排序（`core_profile._rank:84-87`）。用户会感觉到「知君好像没那么记得这件事了」，但界面上什么也没说。

**产品对策（主张）**：

1. 删除时追加一条 `review_event`，`action='evidence_lost'`，记下失去的证据条数与资料标题。
2. 该理解在本体页标注「证据已删除，仍保留你的确认」，并提供「重新确认」与「一并撤回」两个动作。
3. 删除预览里必须显示「这次删除会让 N 条已确认理解失去出处」，而不只是显示卡片与派生数量。

### 7.3 墓碑不回流

由状态机保证：`retracted` 与 `superseded` 不在任何动作的合法前置里（4.5 节转移表）。`system_retract:1645-1668` 也只对 `working` 生效，第一行就检查 `row["trust_state"] != "working"` 并直接返回。

**唯一允许的自动状态变化**只有两个触发点：

| 触发 | 原因码 | 位置 |
|---|---|---|
| challenged 超 30 天未确认 | `decayed_contradicted` | `consolidate.py:229-231`，`CHALLENGE_DECAY_DAYS = 30`（`:28`） |
| 资料删除且无其它证据 | `evidence_purged` | `ontology_store.py:1861` |

两者都只对 `working`，都写 `actor='system'` / `surface='system'` 的复核事件。目标态不增加第三个。

### 7.4 清空即清空

`purge_all:1884-1909` 今天删 9 张表（`:1892`）、非 `ent_me` 实体（`:1894-1895`）、2 个 meta 键（`:1899-1901`）。

**它漏掉的（同库，清不掉）**：

| 表 | 来源 | 为什么必须清 |
|---|---|---|
| `work_matters` / `work_matter_bindings` / `work_artifacts` / `work_actions` | `matters_store.py:15-42` | 用户手写的项目、目标、下一步 |
| `memory_policy` / `memory_admissions` / `memory_attention` / `memory_drafts` | `memory_store.py` | 记忆准入与注意力记录 |
| `routing_*` 11 张（含 `context_lookup_stages`） | `routing_store.py` | 授权与预览历史 |
| `learning_episodes` | `learning_store.py` | 学习片段 |
| `charter_exceptions` | ontology.db | 章程例外 |

另外 `conversations.db`（13 张）与 `growth.db`（9 张）根本不在 `purge_all` 的范围内。

**目标态**：清空是**删掉整个数据文件夹里的内容文件并重建空库**，不是逐表 DELETE。P1 说「一个数据文件夹，可备份可拷走可删除」，那么「清空」就应该等价于「删掉文件夹再新建」。逐表 DELETE 的做法每加一张表就多一个遗漏点，今天已经漏了 20 多张。

---

## 8. 可携带格式

### 8.1 今天缺什么

| 缺口 | 事实 |
|---|---|
| 导出不是文件 | `GET /api/mindos/ontology/export`（`ontology.py:377-387`）只是 JSON 响应，**无 `Content-Disposition`**，浏览器不会当文件下载 |
| 导出不完整 | 不含对话正文、不含资料原件、不含判断、不含事项 |
| 没有导入 | **全仓没有任何 ontology import / restore 路由或脚本**（已 grep 确认） |
| USER.md 不可回灌 | `projection.py:29-33` 生成的是人写给人看的 markdown 行，**没有 id、没有版本、没有证据锚点**，机器无法安全地读回来 |

### 8.2 `.zhijun` 导出包（目标态）

包根目录名形如 `my-knowledge.zhijun/`，是一个普通目录（或其 zip），要求**没有知君也能读懂**：所有结构化数据用 JSONL，所有人类可读内容用 Markdown，校验用标准 `sha256sum` 格式。

| 包内路径 | 格式 | 说明 |
|---|---|---|
| `manifest.json` | JSON | 见 8.3 |
| `README.md` | Markdown | 人类可读的导读：这是什么、每个文件是什么、怎么用文本编辑器打开。**不含任何知君专有术语的缩写** |
| `claims.jsonl` | 每行一条 JSON | 第 4.1 节的字段，`layer` / `section` / `trust_state` 用全称而非编号 |
| `entities.jsonl` | 每行一个实体 | 含别名数组 |
| `evidence.jsonl` | 每行一条证据 | 带 `claim_id` 外键与锚点（`chunk_key` / `locator`） |
| `review_events.jsonl` | 每行一条 | 完整历史，`before` / `after` / `actor` / `surface` |
| `decisions.jsonl` | 每行一个判断 | 含事前预期、结果、复盘 |
| `matters.jsonl` | 每行一个事项 | 用户手写的项目卡 |
| `conversations/<conversation_id>.jsonl` | 每行一条消息 | 原文，不做摘要替换 |
| `materials/<material_id>/original.<ext>` | 原件 | 按原始扩展名保存 |
| `materials/<material_id>/snapshot.txt` | 纯文本 | 解析快照。offset 与 `evidence.jsonl` 的 `locator` 对齐 |
| `profile.md` | Markdown | 画像的人类可读投影，带生成时间与 `source_hash` |
| `takeaway_receipts.jsonl` | 每行一条回执 | 第 6.7 节的出门记录 |
| `SHA256SUMS` | 文本 | 每个文件一行哈希，用标准 `sha256sum` 格式，任何系统都能校验 |

### 8.3 `manifest.json` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `formatVersion` | string | `.zhijun` 格式版本，与产品版本解耦 |
| `exportedAt` | ISO8601 | |
| `exportedBy` | string | 产品名与版本 |
| `scopeOfExport` | enum | `full`（全量备份）/ `takeaway`（只含 `可带走()` 放行的内容） |
| `counts` | object | 各文件条数，用于导入前校验 |
| `files` | array | 每项含 `path`、`sha256`、`bytes`、`contentType` |
| `redactions` | array | `takeaway` 模式下被剥离的内容类型（例如 `material_quote`），对应 6.2 选项 ① |
| `contentHashAlgorithm` | string | `claims.content_hash` 的算法说明，供导入端重算校验 |

### 8.4 导入 / 回灌语义（目标态）

| 规则 | 说明 |
|---|---|
| **幂等** | 同一个包导入两次，结果与导入一次相同。依据是 `claim.id` + `content_hash`（`ux_claims_active_hash`，`:152-156`） |
| **冲突处理** | 目标库已有相同 `content_hash` 的活跃理解时：若 `id` 相同则跳过；若 `id` 不同则把导入的那条作为新证据并入已有条（`add_evidence`），**不新建第二条** |
| **墓碑优先** | 目标库里该 `id` 已是 `retracted` / `superseded` 的，**导入不复活**，记一条跳过日志。原则 3 高于导入完整性 |
| **证据缺失时降级** | 导入的 claim 若其引用的资料原件或对话不在包里，该条**降级为 `working`**，并写一条 `origin='material'` 或 `origin='utterance'` 的占位说明。绝不以 `confirmed` 落地一条无法验证出处的断言 |
| **开关沿用** | `takeaway` 三态原样导入。曾被设 `off` 的保持 `off` |
| **复核事件只增** | `review_events` 按时间戳合并去重，不覆盖 |
| **回执不导入** | `takeaway_receipts.jsonl` 只作审计，导入时跳过 |

---

## 9. 简化对照表

| 维度 | 现状 | 目标 |
|---|---|---|
| 表数量 | **57** 张 | **约 15** 张 |
| 一等对象 | **23** 类 | **6** 类 |
| 路由 | `/api/mindos/*` 的 `add_api_route` 注册约 **204** 条；全后端含装饰器注册共约 **388** 条 | **约 60** 条 |
| 授权相关表 | **21** 张 | **2** 个概念（分区默认 + 逐条开关） |
| 出门规则实现 | 4 个通道 / **3** 套规则 | 4 个通道 / **1** 个 `可带走()` |
| claim 打分实现 | **2** 套 | **1** 套 |

### 9.1 现状 57 张表的构成

| 库 | 张数 | 构成 |
|---|---|---|
| `ontology.db` | 35 | 核心 9（`ontology_store.py`）+ learning 1 + alignment 3 + reflection 2 + memory 4 + matters 4 + routing 11 + `charter_exceptions` 1 |
| `conversations.db` | 13 | 核心 7 + chat_import 5 + reply_assist 1 |
| `growth.db` | 9 | growth 4 + charter_draft 5 |

另：`backend/mindos/` 下不同表名共 **94** 个（含 card_ledger 9 张、connectivity 7 张等不在上述三库的表）。

### 9.2 删除清单

| 删除对象 | 张数 | 理由 |
|---|---|---|
| `routing_*`（含 `context_lookup_stages`） | 11 | 单人本地无多 scope |
| `connectivity_*`（含 2 张 `consumer_*`） | 7 | 同上 |
| `card_*`（知识卡片） | 9 | 与 claims 完全不相连，且占据了搜索的第一层，与 P2 冲突 |
| `alignment_grants` / `chat_material_grants` / `chat_material_privacy` | 3 | 由分区默认 + 逐条开关取代 |
| `chat_import_*` | 5 | 折叠进资料导入通路 |
| `charter_draft_*` | 5 | 折叠为 claim 集合的草稿状态 |
| `reflections` / `reflection_reviews` | 2 | 折叠为 `hypothesis` claim |
| `learning_episodes` | 1 | 折叠为判断的事前预期字段 |
| `memory_*` | 4 | 降为进程内或单表 |
| `alignment_*`（conversations / requests） | 2 | 折叠为 claim 属性 |
| `charter_exceptions` | 1 | 折叠为 claim 字段 |
| `OntologyStore.export_payload` | 方法 | 零调用方的死代码（`:1870-1882`） |
| `device_scope` 列与相关分支 | 661 处 / 47 文件 | 见 4.3 |

---

## 10. 未决事项

以下需要产品拍板，工程不自行决定。

| # | 议题 | 选项 | 倾向 |
|---|---|---|---|
| 1 | **`how_to_apply` 去留** | ① 删列，语义并进 `why_it_matters`；② 保留列并补齐抽取与渲染通路 | 倾向 ①。写侧读侧今天都是空的（4.2 节），保留一个没人用的列只会让下一个人以为它在工作 |
| 2 | **资料派生理解能否出门** | ① 可带走但剥掉原文片段；② 仅证据全部来自用户原话时可带走；③ 维持现状并在界面明示 | **倾向 ①**。资料只作为证据，理解本身是用户的。今天 `source_policy.py:74` 的一刀切与 P2 直接冲突（6.2 节） |
| 3 | **事项与理解的边界细则** | 3.2 节给了五条划界，但「用户在事项卡里写的一句话，要不要自动提议成 claim」没定 | 倾向不自动提议。自动提议会让手写清单变成模型断言的来源 |
| 4 | **知识卡片的下线迁移** | ① 直接删除 9 张表；② 一次性转成资料；③ 一次性转成 claim 候选让用户逐条裁决 | 倾向 ②。卡片是用户写过的内容，当资料保留出处比当断言更安全，但需要确认有多少真实数据 |
| 5 | **清空语义的向后兼容** | 目标态把清空改成「删文件夹重建」，与今天的逐表 DELETE 行为不同 | 需要确认：老用户升级后第一次清空，是否需要一次迁移提示 |
| 6 | **`takeaway` 三态的迁移** | 今天是两态布尔，历史数据全是 0 或 1，没有 `unset` | 需要拍板：历史的 0 算 `off`（永久拒绝）还是算 `unset`（取分区默认）。前者更保守，后者更符合用户预期 |
| 7 | **`burdens` 的有效期与衰减** | 现有参数按「原则、做法」这类稳定内容调；困扰会过去 | 需要真实数据。第一版给一个显著更短的默认值并记录实际重申间隔，不猜（4.9 节） |
| 8 | **张力 B 的阈值** | 「重申 ≥3 次、跨度 ≥30 天、无相关进展」是拍脑袋的初值 | 需要真实使用校准。误判代价不对称：说早了像逼问，说晚了只是没说。第一版取保守值 |
| 9 | **「重话」的本地判定阈值** | PRD 5.5 要求重话推迟抽取，判定是关键词加上下文，不依赖模型自觉 | 倾向向「宁可不记」倾斜。漏判会把袒露变成档案，误判只是少记一条（4.9 节） |
| 10 | **两个内观分区是否进 `.zhijun` 导出包** | ① 进包但整体加密、只有本人能解；② 进包不加密（包本来就在用户自己手里）；③ 不进包 | 倾向 ①。导出包是「带我走」，用户自己的东西该带得走；但包可能被误发，而这两个分区是全库最敏感的内容。需与第 8 节的包格式一起定 |
