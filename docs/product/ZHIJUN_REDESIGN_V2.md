# 知君重设计 V2 · 通过对话认识你的良师益友

> 状态：2026-09-02 定稿并开始实现（P1 竖切已落地，见 §11 能力真实性表）。
> 关系：本文取代 `ZHIJUN_PRD_V1.md` 的 §8（旅程）、§13.3（工程阻断清单）、§14–§16（接口变更与 24 周计划）；
> PRD 的 §4 产品原则、§7.2–7.5 对象模型与四层自我、§11 AI 行为规范、§12.1 数据分类**继续有效**，本文不重复。
> `ZHIJUN_MVP_IMPLEMENTATION.md` 归档为历史。接口细节见 `docs/development/zhijun-api-contract.md`。

## 0. 一句话

知君是一个通过对话逐渐认识你、并把这份「认识」交给你掌管的 AI。它记得你的人、事、原则和判断，在你要拿主意时陪你商量，在结果回来时陪你复盘，是你生活里一位有边界的良师益友。

## 1. 差距在哪（不在愿景）

PRD V1 第 28 行已经写了「先成为一面真正懂用户的镜子，再成为有边界的良师益友」。真正的裂缝有三处：

1. **对话不存在**。上线版唯一的「对话」是单轮问答框（`POST /api/mindos/qa`，请求体只有 `question`），无历史、无流式、无工具；系统提示词自称「本地知识库问答助手」，明令不得使用记忆。
2. **「AI 眼中的我」不存在**。Claim / Entity / EvidenceSpan 在代码里是 0 行；用户画像是一份静态 Markdown 模板；实体只是文档详情页的只读徽章。
3. **「参与判断」被做成事后填表**。唯一做完的闭环是「章程 → 记录判断 → 结果 → 复盘」的手填 CRUD，入口是「结果回来了」，不是「我现在要拿主意」。

此外 README 讲的是另一个产品（个人记忆库 / RAG 中间件），与 PRD、三版原型形成四套并存叙事。

## 2. 六个决定（已拍板，2026-09-02）

| # | 决定 | 选择 | 理由 |
|---|---|---|---|
| D1 | 本体的主要构建路径 | **对话优先**；资料导入是加速器，在对话自然时机邀请 | v4 原型的「没有数据不假装懂你」靠**标签**守住，不靠**门禁** |
| D2 | 未确认的理解能否用于对话 | **能**，三层信任：对话记录 / 工作理解 / 已确认本体；工作理解只能带保留语气用、不出设备、不单独触发提醒 | 否则「懂你」的上限等于用户手动确认的速度 |
| D3 | 判断的时态 | **当下商量**为一等模式；事后复盘保留 | DecisionEpisode 本来就有「当时的理由 / 假设 / 信心」 |
| D4 | 人群与频率 | **全生活面、日常对话 + 每周回顾**；人群从「企业主」放宽到「需要自己拿主意的成年人」 | 用户原话「生活里的重要角色」「很多判断」 |
| D5 | 硬件前提 | **软件优先、本地优先**；盒子降为可选部署目标；口径改为「原件不出设备，云模型只收必要片段且可见可审计」 | 代码里盒子相关全是虚构演示 |
| D6 | 知君能不能有看法 | **能**，标注「知君的看法」，永远不是决定 | PRD 禁的是「替用户作决定」，不是「有意见」 |

## 3. 核心循环

```
聊 → 记 → 认 → 用 → 回访 → 长
```

- **聊**：一段持续的关系，不是无限聊天历史；文字为主。
- **记**：每轮对话 → 对话记录 + 后台抽取理解。你亲口说的（self_declared）直接进入本体并标「你告诉我的」；我推测的（hypothesis）进入工作理解。
- **认**：知君在要紧时用一句话确认（「我印象里你…，对吗？」），一键 对 / 部分对 / 只适用于这件事 / 不对 / 先别存；本体页可随时改、撤。
- **用**：每次回复都基于本体，并带来源标签。
- **回访**：承诺与判断到期时知君来问，结果在对话里记下（P2）。
- **长**：模式 → 原则候选 → 章程演进（P3）。

## 4. 三层记忆 × 四层自我

| 信任层 | 内容 | 对话中能否用 | 出设备 / 给其他 Agent | 进入方式 |
|---|---|---|---|---|
| 对话记录 | 全部原文 + 摘要 + 每轮回执 | 检索 | 否 | 自动 |
| 工作理解 | 模型抽取、未确认的 Claim（`trust_state=working`） | 能，必须带保留语气 | 否 | 自动 |
| 已确认本体 | 用户确认过的 Claim（`confirmed`） | 作为事实 | 按授权（`export_allowed` 且非敏感） | 一键确认 / 用户复述 / 本体页编辑 / **用户亲口说的自动确认** |

自我层沿用 PRD：`self_declared 你告诉我的` / `observed 资料里看到的` / `hypothesis 我推测的` / `aspirational 你想成为的`。
纠正过的理解打墓碑（`retracted` / `superseded`）永不回流：每轮组装上下文时把词面相近的墓碑作为「不得再复述」块送给模型，抽取阶段命中墓碑的候选直接丢弃（用户本人再次陈述例外）。

「自动确认」的安全阀：只有 `self_declared`、quote 是用户消息的精确子串、置信度 ≥ 0.8、谓词在词表内时才直接 `confirmed`；否则一律 `working`。每轮最多 4 条。

## 5. 「我的本体」

六个抽屉 + 一个 feed：**我是谁 · 我的人 · 我的事 · 我的原则 · 我的做法 · 我的方向** + **知君最近学到的**（待确认的工作理解，取代原来的治理工作台）。

每条理解显示：层标签（文字）、信任状态、来源（可点到对话轮次或资料）、首次出现 / 最近重申、适用范围；动作：对 / 部分对 / 只适用于这件事 / 不对 / 撤回 / 重申。谓词受控词表按分区固定（`who: is/has_trait/background/role`；`people: knows/works_with/relationship/attitude_toward`；`matters: working_on/committed_to/happened/owns`；`principles: holds_principle/boundary`；`ways: prefers/tends_to/decides_by`；`direction: wants_to/goal/avoids`）。

## 6. 良师益友的行为清单（可测试）

记得（自然引用过去）· 追问一个好问题（默认 150 字内、最多一个问题）· 有看法（带「知君的看法」标签）· 敢挑战（只用用户确认过的原则与历史，强度由章程的 `challengeStyle` 控制）· 回访 · 诚实（分清事实 / 你说的 / 我猜的，不知道就说不知道）· 有边界（不替你决定；医疗 / 心理危机 / 法律 / 投资 / 信贷 / 人身安全按 PRD §11.2；`quietDomains`；不出设备）。深度模式沿用 PRD 五段结构。反模式：无成长分、无人格评分、无打卡、无伪思维链、不声称情感。

## 7. 参与判断的四种模式

商量（当下：还原上下文 → 摆选项 → 一个关键问题 → 知君的看法 → 侧栏实时判断草稿 → 一键入判断簿）· 回访（到期问结果）· 提醒（事件触发、每日 ≤ 3、说明「为何现在」、遵守 `quietDomains`、可按主题静默）· 周回顾（可选）。P1 只落地了「商量」的对话侧提示，草稿面板与回访在 P2。

## 8. 信息架构（4 个入口替代 15 个路由）

| 入口 | 路由 | 内容 |
|---|---|---|
| 对话（默认） | `/`、`/c/:id` | 会话列表 + 消息流 + 作曲区；回复下方有出处条与待确认候选 chip；首次使用显示「先让我认识真实的你」并进入建档对话（7 个问题，一次一个） |
| 我的本体 | `/me`、`/me/inbox` | 六分区 + 知君最近学到的 |
| 判断 | `/judgments` | 现有判断簿（章程 / 判断 / 结果 / 复盘） |
| 资料与边界 | `/data` | 原材料、设置（模型与隐私）、回收站、知识档案、搜索的枢纽；本体投影预览 |

旧路由 `/materials*`、`/knowledge*`、`/search`、`/graph`、`/recycle-bin`、`/settings`、`/today` 保留但不进侧栏；`/qa`、`/generate`、`/governance`、`/corrections` 删除。视觉换成原型的米纸 / 墨 / 朱砂 / 宋体（令牌名保留、只换值）。

## 9. 技术方案（P1 已落地部分）

```
backend/mindos/stores/ontology_store.py      实体 / 理解 / 证据 / 复核事件 / 本体任务（SQLite, ontology.db）
backend/mindos/stores/conversation_store.py  会话 / 消息 / 摘要 / 出设备回执（SQLite, conversations.db）
backend/mindos/zhijun/                        provider（fake / ollama / openai-compatible / anthropic）
                                              gate · persona · context · extract · jobs · projection · turn · confirm
backend/mindos/conversations.py               /api/mindos/conversations（SSE）
backend/mindos/ontology.py                    /api/mindos/ontology
backend/mindos/zhijun_status.py               /api/mindos/zhijun/status
frontend/mindos-web/src/pages/{ConversationPage,OntologyPage,DataHubPage}.vue
frontend/mindos-web/src/services/sse.ts       fetch + ReadableStream 的 SSE 客户端（带自定义头）
scripts/e2e_zhijun_phase1.py                  真实后端端到端（演示模型，不出网）
```

- **上下文组装**（`context.py`）：人格 → 建档 / 深入指令 → 章程 → 已确认理解（词面检索 k=12）→ 未确认印象（k=6，带保留语气指令）→ 被纠正块 → 资料片段（`qa.build_evidence`，本地通道默认不带）→ 摘要 → 近期 12 轮。本地通道总预算 6000 字符，外部 24000。外部通道只送 `public/private` 的理解，`sensitive/restricted` 永不外发。每轮写回执（送出了哪些理解、多少字符、有几条被纠正提示）。
- **模型通道**（`provider.py`）：`ZHIJUN_PROVIDER=fake|ollama|openai|anthropic`；默认沿用设置页快照（外部开启且 provider=openai → OpenAI 兼容，否则本地 Ollama，`num_ctx` 默认 8192 可配）。Anthropic 走官方 SDK（流式 + `output_config` 结构化输出 + 服务端 fallbacks），**本机策略禁止访问 anthropic.com 时不得启用**。
- **抽取**（`extract.py` + `jobs.py`）：轮次结束后入队，后台单线程 worker 处理；用产生该轮回复的同一通道，不产生新的出设备。
- **投影**（`projection.py`）：`memory/ZHIJUN_PROFILE.md`（完整已确认视图）与 `memory/USER.md`（可导出子集），让旧的 `/api/memory/context` 与 MCP `memory_get_user_profile` 零改动变成「只读已确认本体」。
- **并发**（`gate.py`）：同一会话同时只允许一轮（409）；本地通道并发 1、外部 3（429）。

保留不动：`vector_store / index_registry / generation_store`、`parser / embedder / watcher / video`、`services/search_service.py`、job 模式、`runtime_paths + conftest` 数据根契约、`secret_store`、`mindos/agent/` 网关、`growth.db`。

## 10. 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 叙事收敛 | README 重写、设计文档、契约、删除碎片文件、主题换色 | 本轮完成（gbrain / consumer_api / 旧 Electron 壳的删除推后到 P3，避免大面积改 server.py 与测试） |
| P1 能聊、能记、能认 | 对话 SSE、两个存储、抽取 / 确认 / 投影、对话页 + 本体页 + 四入口 + 主题、建档对话、演示模型端到端 | **本轮完成（削减版）**；未做：主题线程、工具调用、就地编辑、Playwright 截图基线更新 |
| P2 能商量、会回访 | 商量模式 + 实时判断草稿 → `growth_decisions`、回访会话、提醒策略与每日 ≤ 3、议题线程 | 未开始 |
| P3 像良师 | 整合器（去重 / 矛盾 / 晋升 / 衰减）、原则-行为张力提醒、资料 → observed 理解、导出 / 全量删除、`server.py` 拆分与旧面退役 | 未开始 |
| P4 | 只读 Context Pack（confirmed ∧ export_allowed）、移动采集、盒子部署 profile、语音、Electron 薄壳 | 未开始 |

## 11. 能力真实性表（本轮）

| 能力 | 状态 |
|---|---|
| 多轮对话、流式、会话持久化、出设备回执 | 已实现，56 个后端测试 + 端到端脚本验证 |
| 从对话抽取理解（自动确认 / 工作理解 / 墓碑抑制 / 复述即确认） | 已实现；真实模型下的抽取质量**未评测**（本机无模型），演示模型用规则抽取 |
| 我的本体（六分区 + inbox + 五动作 + 手写新增 + 投影） | 已实现 |
| 建档对话（7 问） | 已实现（演示模型按脚本提问；真实模型按提示词） |
| 对话页 / 本体页 / 资料枢纽 / 四入口 / 新主题 | 已实现，`npm run build` 与 node 测试通过；**未做浏览器截图回归** |
| Ollama / OpenAI 兼容 / Anthropic 通道 | 代码已实现，解析逻辑有单元测试；**本机未接真实模型联调** |
| 商量草稿面板、回访、提醒、整合器、资料 → 理解 | 未实现（P2 / P3） |
| 硬件盒子、语音、移动端、Context Pack | 未实现（P4） |

## 12. 指标

北极星（候选）：每周至少一次把真实判断带给知君、且事后认为有帮助的用户比例。过程指标：工作理解确认率（点「对」的比例）、回复中正确引用过去上下文的比例、inbox 中位深度（目标 < 8）、`review_events.surface` 中 `ontology_page` 占比（> 50% 说明对话内确认失败）。反指标沿用 PRD §5.3。

## 13. 风险

1. 小模型抽取质量 × 「原话直接确认」：入口三道闸 + 每轮 ≤ 4 + 外部模型开启时抽取走外部；纯本地明示「知君会记得更少」。
2. 隐私：工作理解与章程随 prompt 外发：sensitive / restricted 永不外发，其余需一次性同意，每轮回执可查。
3. 过度抽取：上限、按会话分组、先别存、衰减（P3）。
4. 本体页退化成治理台：P1 刻意不做批量确认。
5. 上下文组装延迟：资料检索与理解检索并行，无资料跳过。
