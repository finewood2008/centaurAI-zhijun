# 知君 P1 接口契约（对话 · 本体 · 确认）

> 版本 p1-2026-09-02。前后端共同遵守；改动先改本文。所有路径挂在现有网关之下：
> 请求必须带 `X-Requested-By: centaur-vdb`（写路由还要求 loopback），票据模式再带 `X-MindOS-Session`。
> 错误体沿用现状：`{"detail": "文案"}` 或 `{"detail": {"code": "...", "detail": "文案"}}`。

## 1. 标签契约（模型输出 → 前端徽章）

助手正文用四种行内标记标注认识论来源，前端把它们渲染成文字徽章（不能只靠颜色）：

| 标记 | 含义 | 来源 |
|---|---|---|
| `【你告诉我的】` | self_declared，用户亲口说过 | 已确认本体 |
| `【资料里看到的】` | observed，来自导入资料 | 工作理解或已确认 |
| `【我推测的】` | hypothesis / 未确认的工作理解，必须带保留语气 | 工作理解 |
| `【知君的看法】` | 知君自己的意见，不是事实也不是决定 | 模型 |

资料引用用 `[m1]`、`[m2]` 行内标记，与 `provenance.materials[i]` 一一对应（i 从 1 起）。

## 2. 对话 `/api/mindos/conversations`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/` | body `{mode?: "chat"\|"onboarding", title?: string}` → `Conversation` |
| GET | `/?limit=50` | `{items: Conversation[]}`，按 lastMessageAt 倒序 |
| GET | `/{id}` | `{conversation: Conversation, messages: Message[]}` |
| DELETE | `/{id}` | 删除会话及消息、回执（不删已抽取的 claim，其证据保留 quote） |
| POST | `/{id}/messages` | body `{content: string, depth?: "brief"\|"deep"}` → **SSE** |
| GET | `/{id}/messages/{messageId}/receipt` | `TurnReceipt` |

```ts
interface Conversation {
  id: string; title: string; mode: 'chat' | 'onboarding'; status: 'active' | 'archived'
  messageCount: number; createdAt: string; updatedAt: string; lastMessageAt: string | null
}
interface Message {
  id: string; conversationId: string; seq: number
  role: 'user' | 'assistant' | 'system'      // system = 系统备注（如「你确认了：…」），content 是人话
  content: string
  status: 'complete' | 'aborted' | 'error'
  provider?: string | null; model?: string | null; external?: boolean
  meta?: Record<string, unknown>            // system 备注：{kind:'review', claimId, action}
  createdAt: string
}
interface TurnReceipt {
  messageId: string; conversationId: string; provider: string; model: string; external: boolean
  confirmedClaimIds: string[]; workingClaimIds: string[]; materialChunkKeys: string[]
  retractedNoticeCount: number; promptChars: number; extractionProvider: string | null; createdAt: string
}
```

### 2.1 SSE 事件（`POST /{id}/messages`，`Content-Type: text/event-stream`）

前端用 `fetch` + `ReadableStream` 自解析（`EventSource` 带不了自定义头）。每帧 `event: <name>\ndata: <json>\n\n`。顺序固定：

```
meta          {messageId, userMessageId, conversationId, provider, model, external, mode, depth}
provenance    {confirmedClaims: ClaimBrief[], workingClaims: ClaimBrief[],
               materials: {materialId, title, chunkKey?, locator?}[],
               retractedNotices: number, charterVersion: number|null, promptChars: number}
token         {t: string}                      // 0..n 次
extraction    {state: 'queued'|'skipped', jobId?: string}
message_done  {messageId, status: 'complete'|'aborted'|'error', usage?: object, receiptId: string}
error         {code: string, message: string, retryable: boolean}   // 出错时替代 message_done
```
`ClaimBrief = {id, content, section, layer}`。

流开始前的失败用普通 HTTP 状态：`409 {code:"TURN_IN_FLIGHT"}` 同一会话已有生成中的轮次；
`429 {code:"PROVIDER_BUSY"}`；`503 {code:"PROVIDER_UNAVAILABLE"}`；`400` 内容为空或超 4000 字。
客户端中断（AbortController）→ 服务端把已生成文本以 `status=aborted` 落库。

抽取是异步的：`extraction.state=queued` 后，前端每 3 秒轮询 `GET /api/mindos/ontology/inbox` 最多 30 秒，
把新出现的候选以 chip 形式显示在该轮回复下方。

## 3. 本体 `/api/mindos/ontology`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/stats` | `OntologyStats` |
| GET | `/claims?section=&trust=&limit=200` | `{items: Claim[]}`；`trust` 可为 `working,confirmed` 逗号列表，默认 `confirmed` |
| GET | `/claims/{id}` | `Claim` |
| POST | `/claims` | body `{content, section, layer?: "self_declared"\|"aspirational", predicate?}` → `Claim`（用户手写，直接 confirmed，trust_origin=user_created） |
| POST | `/claims/{id}/review` | body `ReviewRequest` → `{claim: Claim, replacedBy?: Claim}`；非法状态转移 409 |
| GET | `/inbox?limit=20` | `{items: Claim[]}`：working 且未 deferred 未 challenged，最新在前 |
| GET | `/entities?type=` | `{items: Entity[]}` |
| GET | `/projection` | `{markdown: string, exportableMarkdown: string, generatedAt: string}` |

```ts
type Section = 'who' | 'people' | 'matters' | 'principles' | 'ways' | 'direction'
type Layer = 'observed' | 'self_declared' | 'aspirational' | 'hypothesis'
type TrustState = 'working' | 'confirmed' | 'retracted' | 'superseded'
type TrustOrigin = 'utterance' | 'user_confirm' | 'user_edit' | 'user_created' | 'material' | 'model'
type ReviewAction = 'confirm' | 'partial' | 'context_only' | 'reject' | 'defer' | 'retract' | 'reaffirm'

interface Claim {
  id: string; subjectEntityId: string; subjectName: string; predicate: string
  objectEntityId: string | null; objectName: string | null
  content: string; section: Section; layer: Layer
  trustState: TrustState; trustOrigin: TrustOrigin; confidence: number
  scope: 'long_term' | 'context_only'; contextRef: string | null
  privacyLevel: 'public' | 'private' | 'sensitive' | 'restricted'; exportAllowed: boolean
  firstSeen: string; lastReaffirmed: string
  supersedesId: string | null; supersededById: string | null
  retractedAt: string | null; retractionReason: string | null
  challenged: boolean; deferredUntil: string | null
  evidence: Evidence[]
}
interface Evidence {
  id: string; kind: 'conversation_turn' | 'material_span' | 'user_edit' | 'decision' | 'review'
  stance: 'supports' | 'contradicts' | 'background'
  conversationId: string | null; messageId: string | null
  materialId: string | null; chunkKey: string | null
  quote: string; createdAt: string
}
interface ReviewRequest {
  action: ReviewAction
  editedContent?: string          // partial 必填
  contextRef?: string             // context_only 可选，默认当前 conversationId
  note?: string
  surface: 'conversation' | 'ontology_page' | 'onboarding' | 'today'
  conversationId?: string; messageId?: string
}
interface Entity {
  id: string; type: 'me' | 'person' | 'organization' | 'project' | 'place' | 'topic' | 'event' | 'term'
  canonicalName: string; aliases: string[]; description: string; status: 'active' | 'merged' | 'retracted'
  claimCount: number
}
interface OntologyStats {
  hasOntology: boolean; entities: number
  claims: { working: number; confirmed: number; retracted: number; superseded: number }
  bySection: Record<Section, { confirmed: number; working: number }>
  inbox: number
}
```

### 3.1 状态机（服务端唯一入口 `OntologyStore.transition`）

```
working   -confirm->      confirmed
working   -partial->      新 claim(confirmed, supersedes=旧)；旧 -> superseded
working   -context_only-> confirmed(scope=context_only)
working   -reject->       retracted
working   -defer->        working(deferredUntil=+14d)
confirmed -retract->      retracted
confirmed -partial->      同 working 的 partial
confirmed -reaffirm->     lastReaffirmed=now
```
其它组合 → 409。每次 transition 同事务写 `review_events`；若带 `conversationId`，再追加一条 `role=system`
的备注消息（如「你确认了：我在做远川项目」），让下一轮模型可见。

## 4. 运行状态 `/api/mindos/zhijun`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/status` | `{provider: 'fake'\|'ollama'\|'openai'\|'anthropic', model: string, external: boolean, extraction: 'enabled'\|'beta'\|'disabled', workerRunning: boolean}` |

provider 由环境变量 `ZHIJUN_PROVIDER` 覆盖（`fake` 仅开发环境），否则沿用设置页的对话通道
（外部开启且 provider=openai → openai；否则 ollama）。`ZHIJUN_PROVIDER=anthropic` 需 `ANTHROPIC_API_KEY`
或 secret store 中的密钥；**本机禁止访问 anthropic.com，本地联调只用 fake / ollama / openai-compatible**。

## 5. 前端路由与入口（P1）

| 路由 | 页面 | 侧栏 |
|---|---|---|
| `/`、`/c/:conversationId` | 对话（默认） | 对话 |
| `/me`（`?section=`）、`/me/inbox` | 我的本体 | 我的本体 |
| `/judgments` | 判断（现 GrowthPage 换皮） | 判断 |
| `/data` | 资料与边界（导航枢纽：原材料 / 设置 / 回收站 / 知识档案 / 搜索） | 资料与边界 |
| 旧路由保留但不进侧栏：`/materials*`、`/knowledge*`、`/search`、`/graph`、`/recycle-bin`、`/settings`、`/growth`（重定向到 `/judgments`） | | |
| 删除：`/qa`、`/generate`、`/governance`、`/corrections` | | |

---

## P2 增补（商量 · 提醒 · 回访）— 版本 p2-2026-09-02

### 6. 商量模式与判断草稿

- `POST /api/mindos/conversations/{id}/messages` 的 body 增加 `mode?: "chat" | "deliberate"`（默认 chat）。
  `deliberate` 时知君按五步回复（还原上下文 → 摆选项 → 一个关键问题 → 【知君的看法】 → 更新草稿），并在 `extraction` 事件之前追加：
  ```
  decision_draft  {draftId, revision, status:'draft', fields: DecisionDraftFields, changedFields: string[]}
  ```
- 草稿按会话唯一（同一会话反复商量只更新同一份草稿，revision 递增）。
- **硬规则**：`choice / rationale / confidence` 只能来自用户原话（草稿里附 `userQuotes`），模型的看法只进 `zhijunView`；确认时用户可在面板里改这三项。

```ts
interface DecisionDraftFields {
  title: string; context: string; options: string[]
  leaning: string | null            // 用户倾向（来自原话），可空
  choice: string | null; rationale: string | null; confidence: number | null   // 0-100，仅来自用户
  expectedOutcome: string | null; reviewAt: string | null                     // ISO，默认 +14 天
  keyQuestion: string | null; zhijunView: string | null
  relatedEntityIds: string[]; evidenceRefs: string[]; userQuotes: string[]
}
interface DecisionDraft { id: string; conversationId: string; messageId: string | null; revision: number
  status: 'draft' | 'confirmed' | 'discarded'; decisionId: string | null; fields: DecisionDraftFields; createdAt: string; updatedAt: string }
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/mindos/conversations/{id}/decision-draft` | 当前草稿或 404 |
| POST | `/api/mindos/conversations/{id}/decision-draft/confirm` | body `{choice, rationale, confidence, expectedOutcome?, reviewAt?, title?, options?}` → `{draft, decision}`；写入 `growth_decisions`（绑定当前章程版本），草稿 → confirmed，追加系统备注「你记下了一个判断：…」 |
| POST | `/api/mindos/conversations/{id}/decision-draft/discard` | 草稿 → discarded |

### 7. 提醒 `/api/mindos/nudges`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/today` | `{items: Nudge[], policy: NudgePolicy}`：今日最多 3 条 pending/shown |
| POST | `/scan` | 立即扫描（否则后台每小时）；返回新增条数 |
| POST | `/{id}/dismiss` | 状态 → dismissed |
| POST | `/{id}/silence` | 该 triggerRef 永久静默 |
| GET / PUT | `/policy` | `{enabled, maxPerDay, silencedRefs: string[]}` |

```ts
interface Nudge { id: string; kind: 'review_due' | 'commitment_due' | 'checkin'; triggerRef: {decisionId?: string}
  whyNow: string; message: string; status: 'pending' | 'shown' | 'acted' | 'dismissed' | 'silenced'; scheduledFor: string; createdAt: string }
```
规则：`review_due` 来自 `growth_decisions` 到期 / 逾期且未记结果的判断（同一判断 3 天内不重复）；受章程 `quietDomains` 粗匹配（判断标题含静默词则不提醒）；`whyNow` 非空。

### 8. 回访会话

- `POST /api/mindos/conversations` body 增加 `decisionId?: string`；`mode: "review"` 时必填。创建后自动追加一条 `role=system` 的开场备注（「这是对「…」的回访：当时你选了…，预期…」），知君在此会话里只问结果与感受、不给新建议。
- `POST /api/mindos/conversations/{id}/outcome` body `{result, notes?}` → 调 `growth_decisions/{decisionId}/outcome`（状态非 open 时 409），追加系统备注，提醒状态 → acted。
- 结果记录后，知君下一轮按五段做复盘引导；复盘仍由现有 `POST /api/mindos/growth/reviews` 提交（判断页）。

---

## P3 增补（整合 · 裁决 · 资料理解 · 导出 / 全量删除）— 版本 p3-2026-09-02

### 9. 整合与裁决 `/api/mindos/ontology`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/proposals` | `{merges: MergeProposal[], conflicts: Conflict[], total}`：待用户裁决的实体合并候选与理解矛盾对 |
| POST | `/proposals/merges/{id}/resolve` | body `{accept: boolean}`；接受 → from 实体并入 into（别名迁移、理解改指） |
| POST | `/proposals/conflicts/{id}/resolve` | body `{keep: 'a'|'b'|'both'}`；留 a 则 b 撤回（反之亦然），both = 两条都对 |
| POST | `/consolidate` | 立即运行整合器（否则每日一次或每新增 20 条理解后由后台 worker 触发）→ 报告 `{mergeProposals, challenged, conflicts, merged, tensions, promoted, decayed, deferred, pairsJudged}` |
| GET | `/export?sections=who,people&includeWorking=false` | `{exportedAt, schemaVersion, entities, claims, reviewEvents}`（默认只导出已确认） |
| POST | `/purge` | body `{confirm: "删除全部记忆", includeConversations?: true}` → `{ontology:{purged,claims,entities}, conversations?:{conversations,messages}}`；确认词不符 400 `CONFIRM_MISMATCH` |

```ts
interface MergeProposal { id: string; fromEntityId: string; intoEntityId: string; fromName: string; intoName: string
  reason: string; score: number; status: 'pending'|'accepted'|'rejected'; createdAt: string }
interface Conflict { id: string; kind: 'contradiction'|'tension'; claimA: Claim; claimB: Claim; verdictBy: string
  note: string; status: 'pending'|'resolved'|'dismissed'; resolution: 'a'|'b'|'both'|null; createdAt: string }
```
`OntologyStats` 新增 `proposals: number`（待裁决总数）；`Claim` 新增 `promotionReady: boolean`（≥2 个独立来源，inbox 置顶，前端标「多处提到」）。
提醒 `Nudge.kind` 新增 `principle_tension`（措辞是问句：「…是原则变了，还是这次情况特殊？」），`triggerRef = {principleId, actionId}`；点击应打开 `/me?section=principles`。

### 10. 资料 → 理解

资料的实体 / 关系抽取完成后，后台把关系三元组写成 `observed` 工作理解（主语是资料实体，不是「我」；涉及人 → 我的人，否则 → 我的事），证据 `kind=material_span`。资料被永久删除时，只靠它支撑的工作理解自动撤回（`retractionReason=evidence_purged`）。前端不需要新页面：这些理解按分区出现在本体页与 inbox，ClaimCard 的证据链接指向 `/materials/:materialId`。

---

## P4 增补（Context Pack · 导出开关 · 语音 · 安装 · 薄壳）— 版本 p4-2026-09-02

### 11. 给其他 Agent 的只读上下文包

- 网关 scope 新增 `zhijun.profile`（本机管理接口 `POST /api/agent/clients` 签发令牌时勾选）。
- `POST /v1/agent/context-pack`（Bearer 令牌）body `{purpose: string(2–200), sections?: Section[], maxClaims?: 1–200}` →
  `{receiptId, purpose, consumer, generatedAt, sections, claims: [{id, section, sectionTitle, layer, layerTitle, content, about, lastReaffirmed}], counts: {included, excludedNotExportable}, notice}`。
  只含 `confirmed ∧ exportAllowed ∧ privacy ∈ {public, private} ∧ scope ≠ context_only`；不带证据原文、会话 ID、资料路径；每次生成写网关审计与本体回执计数。
- MCP 工具 `mindos_context_pack(purpose, sections?, max_claims?)` 同语义。
- 本机端：`POST /api/mindos/ontology/claims/{id}/export` body `{allowed: boolean}` 逐条开关；`GET /api/mindos/ontology/context-pack` → `{exportable, receipts: {count, last: {generatedAt, consumer, purpose, included} | null}, items: Claim[]}` 给「资料与边界」页显示「哪些认识会被带走」。

### 12. 前端

- Composer 语音输入：浏览器 `SpeechRecognition`（zh-CN，连续、临时结果写进输入框，用户确认后才发送）；不支持的浏览器隐藏按钮。不做录音上传。
- PWA：`manifest.webmanifest`（name 知君、`display: standalone`、`start_url: /mindos/`、主题色 `#A6452E`、SVG 图标），`index.html` 引用；不做 Service Worker / 离线。
- ClaimCard：已确认理解显示「可带走」开关（`exportAllowed`）；敏感 / 受限的开关禁用并说明。
- 资料与边界：「可以带走的认识」区块 = `GET /ontology/context-pack`（数量、最近一次被谁以什么用途取走、列表）。
- 桌面薄壳：`frontend/shell/`（Electron，只加载 `/mindos/`，无 preload / IPC）。

---

## 「良师」内核增补 — 版本 mentor-2026-09-02

- `POST /{id}/messages` 的 SSE `decision_draft` 事件新增 `state: 'ready' | 'queued'`：演示模型同步整理（ready，带 fields）；真实模型（推理型要几十秒）改为后台任务，先发 `queued`，前端每 3 秒轮询 `GET /{id}/decision-draft` 最多 90 秒，`revision` 变化即更新面板。`fields` 新增 `relatedDecisionIds: string[]`（用户自己记下的相似判断，可链接到 `/judgments`）。
- 建档第 8 轮（问完）后，后台生成一条「第一次观察」：`layer=hypothesis` 的工作理解，出现在 inbox / 建档地图上，等用户点头。
- 复盘 `POST /api/mindos/growth/reviews` 成功后，每条 `lessons[i]` 变成 `principles` 分区、`layer=aspirational` 的待确认理解（证据 `kind=review`）；≥2 次复盘写下相近经验 → `promotionReady`。
- 提醒 `Nudge.kind` 新增 `commitment_due`（`triggerRef={claimId, section}`；承诺到期前一天与当天，措辞带「不想聊也可以先划掉」）与 `weekly_review`（只在周日触发，`triggerRef={summary, weekStart}`；前端「一起看看」= 新建 chat 会话并自动发送「我们一起看看这周吧：{summary}」；「稍后 / 不再提醒」同其它类型）。
- `POST /api/mindos/ontology/claims` 的 `section` / `layer` 可省略：服务端按规则分类（用户可在结果卡上改）。
- `GET /api/mindos/zhijun/status` 新增 `pendingJobs`（后台任务数），前端可据此显示「知君还在整理 N 件事」。
- 外部模型：`ZHIJUN_OPENAI_TASK_MODEL` 指定后台 JSON 任务模型（默认同主模型）；JSON 任务预算至少 6000 token（推理型模型会先写推理）。
- 思考开关：`ZHIJUN_OPENAI_THINKING=deepseek`（base_url 含 deepseek 时自动）会按强度发送 `thinking`：effort=low（简短回复、抽取、摘要、草稿）关闭思考，effort=medium/high（深聊、商量、第一次观察）开启并把预算放宽到 ≥6000；其它 OpenAI 兼容服务不发送该参数（`ZHIJUN_OPENAI_THINKING=off` 可强制关闭）。

---

## 对话产出与出处显式化增补 · 版本 outcomes-2026-09-03

### 13. 对话产出 `GET /api/mindos/conversations/{id}/outcomes`

这段对话留下了什么。归属规则：理解按 `claim_evidence.conversation_id` 归到本会话（一条理解可同时归属多段对话）。

```ts
interface ConversationOutcomes {
  conversationId: string
  confirmedClaims: ClaimBrief[]        // 本会话归属、trustState=confirmed
  workingClaims: ClaimBrief[]          // 本会话归属、trustState=working
  decision: { id: string; title: string; choice: string; reviewAt: string | null; status: string } | null
  commitments: { claimId: string; content: string; validTo: string }[]   // predicate=committed_to 且 validTo 非空
  pendingJobs: number                  // 与本会话相关的后台待处理任务数（owner 是本会话或 payload 带本会话 id）；取不到时给全局数
  retracted: number                    // 本会话归属、已撤回 / 已替代的理解数
}
```

- `decision`：本会话确认入簿的判断。取 `conversation.decisionId`（回访会话），否则取本会话 `decision_drafts` 里最近一条 `status=confirmed` 的草稿绑定的判断；都没有为 `null`。
- 会话不存在 → `404 {code:"CONVERSATION_NOT_FOUND"}`。

### 14. 会话列表项 `GET /api/mindos/conversations`

每项保证带 `mode`，并新增：

```ts
outcomes: { confirmed: number; working: number; decision: boolean; commitments: number }
```

计数与 §13 同源（服务端一次 SQL 按 `conversation_id` 聚合 `claim_evidence`），`decision` 的判定规则同 §13。

### 15. `provenance` 事件新增字段

```
provenance    {..., pastDecisions: {id, title, choice, status}[], anchorClaimIds: string[]}
```

- `pastDecisions`：本轮带进上下文的「你过去类似的判断」（`history.similar_decisions`，纯词面匹配、无模型开销，最多 3 条）。商量与回访必带；普通 `chat` 模式在这句话过了抽取门（`should_extract` 通过：≥ 6 字且不是无第一人称的纯提问）时也带。
- `anchorClaimIds`：商量 / 回访 / 深入时无论词面是否命中都带上的、原则与做法分区的已确认理解 id（最多 6 条）；这些 id 同时出现在 `confirmedClaims` 里，前端可据此把它们标成「原则锚点」。普通聊天为空数组。
- 由回执还原的历史出处（`fromReceipt: true`）这两项为空数组：回执表未存过去判断与锚点。

### 16. 回访会话：复用与模板开场

- `POST /api/mindos/conversations` body `{mode:"review", decisionId}`：同一 `decisionId` 已有活跃的回访会话时直接返回它，响应多一项 `reused: true`；否则新建并返回 `reused: false`。
- 新建的回访会话第一条消息是知君的开场（`role=assistant`，`meta.kind=review_open`，`provider=model="template"`，模板生成、不调模型），文案：
  「「{title}」到了回访的时候。当时你选了「{choice}」，预期是「{expectedOutcome}」。先别急着说结果，这段时间你感觉怎么样？」
  取代原来的 `role=system` 备注；§8 中「自动追加一条 role=system 的开场备注」以本节为准。

### 17. 静默领域来自建档

提醒扫描（`nudges.scan`）的静默词除章程 `quietDomains` 外，还从本体 `principles` 分区的已确认 / 待确认理解里抽：`predicate=boundary`，或内容含「不希望 / 不要 / 不用 / 别 … 提 / 聊 / 谈 / 碰 / 问」句式。去掉句式词（我不希望、AI、知君、主动提起、这些话题、的话题 等）后按「和、或、以及、与、，」切分，保留 2 到 8 字的话题词。例：「我不希望AI主动提起健康和家里的矛盾这些话题」→ `健康`、`家里的矛盾`；标题或内容命中的到期判断与承诺不生成提醒。

---

## 关系首页增补 · 版本 relationship-home-v4-2026-09-03

### 18. 共同地图 `GET /api/mindos/zhijun/home`

旧 `/api/mindos/home` 保持不变。新接口同步返回真实数据模板，并在来源指纹变化时入队 `home_brief` 后台任务；前端仅在 `brief.status=refreshing` 时轮询，不等待模型。

```ts
interface ZhijunHomeOverview {
  state: 'first_meet' | 'building' | 'established'
  brief: { status: 'ready' | 'refreshing'; headline: string; message: string; generatedBy: string; sourceRefs: HomeSourceRef[] }
  map: { relationshipDays: number; nodes: HomeMapNode[] }
  nextAction: { kind: 'onboarding' | 'resume_onboarding' | 'review' | 'reflect' | 'commitment' | 'confirm' | 'nudge' | 'chat'; title: string; description: string; targetId: string | null; say: string | null }
  timeline: HomeTimelineEvent[]
  generatedAt: string
  sourceHash: string
}
```

- 节点 `ring` 为 `remembered | tracking | uncertain`，`sourceType` 为 `claim | decision | commitment`，并内嵌完整 `Claim` 或判断摘要供详情面板使用。
- 地图上限固定为 4 条已确认理解、3 条跟进中的判断/承诺、3 条未确认理解；轨迹只取最近 6 个“记住、判断、结果、复盘”事件。
- `sourceRefs.label` 只使用“你确认过 / 我的推测 / 判断簿”，每条同时带可点击的 `id`、`sourceType`、`title` 和 `trust`。
- 缓存存于 `ontology_meta.zhijun_home_snapshot_v1`。模型只可返回 `headline / message / focusIds`，非法来源 id 会使整份模型结果回退到模板。
- 外部模型不接收 `sensitive/restricted` 理解；判断仅发送标题、选择、状态和回访时间。
