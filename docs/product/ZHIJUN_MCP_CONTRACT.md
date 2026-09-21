# 知君 MCP 契约（PRD 附录 B）

配套：[产品愿景](ZHIJUN_VISION.md)、[产品需求](ZHIJUN_PRD_V2.md)（主文档）、[数据层](ZHIJUN_DATA_MODEL.md)（附录 A）。本文为附录 B。

> 本文是知君产品重做 PRD 的附录 B。读者是工程团队与集成方。
> 凡描述当前代码的句子都带 `文件:行号`；凡新提出的设计都标 `主张` 或 `目标态`，不带标记的即为已存在事实。

---

## 1. 文档定位与不变量

知君是**一个让用户直面内心的产品**，角色是知己与导师（见[产品愿景](ZHIJUN_VISION.md)）。

**MCP 在 2026-09-21 的定位修正中被降级。** 它不再是「主输出口」，更不是产品存在的理由，而是一项延伸能力：知君是真正认识用户的那个 AI，其他 AI 在用户同意下借用这份认识的**一部分**。它在排期上从 P1 退到 P2，并且从首次体验流程里移出（PRD 5.2）。

「一部分」是本次修正带给本文最重要的一条：用户袒露心声的产物——`burdens`（心里的事）与 `self_view`（我眼中的我）——**永不通过 MCP 外发，且没有开关**。别的 AI 没有理由知道什么压在用户心里。

以下六条是不可违反的约束。任何实现、任何排期压力都不能绕过。

| # | 不变量 | 含义 | 现状锚点 | 违反的后果 |
|---|---|---|---|---|
| I1 | 本地优先 | 无账号、无云控制面、无盒子。数据、授权、审计全在本机。传输只有 stdio 与 `127.0.0.1` HTTP 两种 | 今天的 A 线强制 https 公网资源标识（`backend/zhijun_mcp/server.py:16-17`），并依赖云侧 ticket 交换（`backend/zhijun_mcp/browser.py:54-80`） | 知君退化成一个需要联网才能读自己的服务 |
| I2 | 默认最小 | 按分区给默认值，敏感与受限永不外发；每个客户端仍需一次显式授权 | 今天授权页六个类别**默认全不勾**（`backend/zhijun_mcp/browser.py:98-100`） | 用户以为只给了一点，实际给了全部 |
| I3 | 只读为主 | 工具面以读为主，写通道单独设计、单独限流 | 工具白名单只有 5 个读（`backend/zhijun_mcp/models.py:84-90`），`service._call` 无写分支（`backend/zhijun_mcp/service.py:37-74`），agent 网关 `writeModes` 恒假（`backend/mindos/agent/service.py:47-54`） | 外部模型能直接改写用户的自我理解 |
| I4 | 提议可写 | 外部 agent 只能 `propose`，落入「待确认」，永不直接写入 | 今天外部完全不能写；最接近的写入口 `POST /api/mindos/ontology/claims` 直接建 `confirmed/user_created`（`backend/mindos/ontology.py:189-229`），但它是 loopback + CSRF 的第一方接口（`backend/server.py:1168-1173`） | 「知君认为」和「Cursor 认为」混成一团 |
| I5 | 全程留痕且不记正文 | 每次调用写审计；审计只记结构化事实，不记查询文本与返回正文 | 审计表八列（`backend/zhijun_mcp/store.py:44-46`），文件头与写入点均声明 never serialize arguments, exceptions or response bodies（`backend/zhijun_mcp/store.py:1,161-168`），失败也只写固定 `denied/none/[]`（`backend/zhijun_mcp/service.py:32-34`） | 审计日志本身变成第二个泄漏面 |
| **I6** | **内心不外流** | `section ∈ {burdens, self_view}` 的理解永不经由任何工具返回，**没有开关、没有授权组合可以打开**。画像、检索、分区读取、最近变化四个面都要过滤 | 这两个分区是 2026-09-21 新增的（[数据层](ZHIJUN_DATA_MODEL.md) 4.4、6.3），今天代码里尚不存在，必须在建表的同一个 PR 里加过滤 | 用户不再敢对知君说真话，产品前提消失 |

这六条同时是验收口径：任何一个 PR 若削弱其中一条，即便测试全绿也应当被拒。

**I6 是六条里唯一一条「漏了无法补救」的。** 其余五条被违反后，用户可以撤销授权、可以重新设置、可以要求我们修。而一句心里话被另一个 AI 读走之后，没有任何补救动作能把它收回来。因此它的实现要求也最严：两层过滤，且协议层根本不承认这两个分区存在（4.1、U9）。

---

## 2. 为什么是 MCP

### 2.1 知君需要一个输出口

**这一节的论证在定位修正后需要重写，因为旧版把因果搞反了。**

旧版说：理解如果只能在知君自己的界面里看，价值上限就是一个漂亮的日记本。这在新定位下是错的——**知君的价值主体恰恰就在它自己的界面里**：袒露、照见、商量、回访。那不是日记本，那是产品。

MCP 的真实价值是次一级的，但仍然成立，有三条：

1. **减少用户的重复劳动。** 用户在知君里已经说清楚的事，不必在 Cursor 里再说一遍。这是效率，不是核心价值，但它是真实的。
2. **证明数据真的是用户的。** 能把它接到任何地方，是「占有即拥有」最有说服力的演示。一个不能输出的本地库和一个云端筒仓，在用户体感上区别不大。
3. **产生一个有用的副信号**：哪些理解真的被别的 AI 读过，可反哺召回权重。

MCP 是当下唯一被这几个客户端共同支持的、可以声明工具与授权的本地协议。选它是因为它今天就能连上，不是因为它在架构上不可替代。

### 2.2 MCP 只是适配器

**主张**：知君的核心资产有三样，都不是 MCP。

| 层 | 内容 | 协议若变会怎样 |
|---|---|---|
| 存储与语义 | 本体条目（section / layer / trust_state / privacyLevel / exportAllowed）、证据锚点、来源闭包 | 不受影响 |
| 文件格式 | 本地可读的导出格式（今天是 `memory/PROFILE.md` 与 `memory/USER.md`，见 `backend/mindos/zhijun/projection.py:95-103`） | 不受影响 |
| 本机 HTTP API | 授权、审计、读取的单一事实来源 | 不受影响 |
| **MCP 适配器** | 工具声明、参数校验、stdio/HTTP 传输 | **只需换这一层** |

因此实现顺序是：先把本机 HTTP API 与授权模型做实，MCP 适配器薄薄一层包上去。反过来做会把协议细节漏进存储层。

### 2.3 今天唯一活着的通路，以及它为什么不算协议

今天真正能让外部 AI 读到知君的，只有一条文件通路：

```
本体条目 → projection 渲染 → memory/USER.md → mcp_tools.memory_get_user_profile → Claude Desktop
```

`backend/mindos/zhijun/projection.py:101` 写出 `USER.md`；`backend/mcp_tools.py:69` 以 `GET /api/memory/files/USER.md` 读回。这条路今天能用，但它不构成契约：

| 缺什么 | 证据 |
|---|---|
| 没有 grant | `backend/mcp_tools.py:14,36-65` 裸打 `http://127.0.0.1:8618`，不带任何 token |
| 没有鉴权 | `GET /api/memory/files/{path}` 无依赖（`backend/memory_api.py:80-88`），而同文件 `:91` 的 PUT 有 `Depends(require_local)` |
| 没有审计 | 该路径不写任何回执 |
| 没有逐条控制 | 唯一过滤发生在 projection 时的 `exportAllowed` |
| 没有 id 与版本 | `USER.md` 是人写的 markdown 行（`backend/mindos/zhijun/projection.py:29-33`），无 id、无版本、无证据锚点 |
| 配置还被主动派发 | `GET /api/config` 直接吐出 Claude Desktop 配置 JSON（`backend/server.py:4106-4171`） |

**结论（主张）**：这条通路必须整体下线，不做兼容期。它的存在使 I2、I5 在任何一台装了知君的机器上都不成立。

---

## 3. 传输与鉴权（目标态）

### 3.1 两种形态

| 形态 | 适用客户端 | 鉴权 | 主要风险 | 默认 |
|---|---|---|---|---|
| **stdio** | Claude Desktop、Cursor、Claude Code（本机进程可直接 spawn） | 进程边界即信任边界；`agentId` 由配置中的固定串声明，token 可选 | 任何能读到配置文件的进程都能冒充该客户端 | 开 |
| **本机 HTTP**（`127.0.0.1:<port>/mcp`） | 容器内 agent、自建脚本、不便 spawn 子进程的客户端 | 必需 `Authorization: Bearer <本地 token>` | 同机任意进程可尝试连接；需要防枚举与限流 | **关（主张）** |

**目标态约束**：

1. 监听地址硬编码 `127.0.0.1`，不接受 `0.0.0.0`、不接受 LAN IP、不接受隧道。今天 `backend/zhijun_mcp/server.py:16-17` 断言 `scheme == "https"` 且 `path == "/mcp"`，需放开为 loopback http，但 `path == "/mcp"`、无 query、无 fragment、无 userinfo 的检查保留。
2. `backend/zhijun_mcp/http.py:41-45` 的 `NoStore` 与 `TrustedHostMiddleware` 原样保留，`allowed_hosts` 改为 `127.0.0.1` 与 `localhost` 两项。DNS rebinding 保护保留。
3. 端口默认 `8644`（**主张**，避开现有 8618 后端与 8620 remote），冲突时失败退出，不自动换口。
4. 任何带 `Origin`、`X-Forwarded-For`、`Forwarded` 的请求直接 403。远程来源没有降级路径。

### 3.2 本地 token

**目标态**：token 的生成、散列存储、前缀展示、过期、防枚举**不需要新写**，`backend/mindos/agent/store.py:163-304` 已有完整实现（`create_client` / `rotate` / `disable` / `authenticate`，前缀 `agk_` 见 `:41`，库中只存散列与不可逆前缀，见 `:172-174` 的说明）。把它抄进 MCP 底座即可。

| 事项 | 目标态 |
|---|---|
| 生成 | 用户在「谁读过我」界面点「添加客户端」时生成，明文只展示一次 |
| 存储位置 | 只写知君数据目录下的 SQLite，不写进任何其他位置 |
| 系统钥匙串 | **未决**，见第 10 节 |
| 过期 | 跟随 grant 期限（1/7/30 天），grant 失效 token 一并失效 |
| 轮换 | 用户可手动轮换，旧 token 立即作废 |
| 失败响应 | 统一 401 + 固定文案，不区分「token 不存在」与「token 已过期」 |

### 3.3 客户端身份 `agentId`

| 来源 | 权威性 | 用途 |
|---|---|---|
| 配置文件中的固定串（如 `--agent-id claude-desktop`） | **权威**。grant、审计、限流全部以它为键 | 授权、留痕、限流 |
| MCP `initialize` 的 `clientInfo.name` / `.version` | **仅供展示**，永不用于授权判断 | 「谁读过我」界面上给用户看「Cursor 0.42」 |

这条与今天 A 线的做法一致：`agentId` 来自 token 的 `client_id` claim（`backend/zhijun_mcp/auth.py:32`），`agentName` 只在授权页渲染时用（`backend/zhijun_mcp/browser.py:99`）。目标态把「来自 OAuth claim」换成「来自本地配置 + 本地 token 绑定」，判断位置不变。

---

## 4. 工具面（目标态）

七个工具。名称不带前缀，MCP 服务名 `知君` 已提供命名空间。所有读工具标注 `readOnlyHint=True, destructiveHint=False, openWorldHint=False`，与今天 5 个工具的标注一致（`backend/zhijun_mcp/server.py:26`）。参数用 Strict 模型、`extra=forbid`、frozen，沿用 `backend/zhijun_mcp/models.py:84-90` 的白名单式派发。

### 4.1 工具总表

| 工具 | 用途 | 关键参数（类型 / 上限） | 授权要求 | 限流（每客户端） | 主要失败码 |
|---|---|---|---|---|---|
| `whoami` | 一页画像 + 指纹，供客户端缓存 | 无 | 至少一个分区已授权 | 60 次/小时 | `GRANT_INACTIVE` |
| `search_memory` | 在已授权分区内检索理解 | `query` str 1..1000；`limit` int 1..20（默认 5） | 分区授权 | 120 次/小时 | `INVALID_ARGUMENTS` |
| `get_section` | 取某个分区的全部可读条目 | `section` **enum(6)，不含 `burdens` / `self_view`**；`limit` int 1..100（默认 30） | 该分区已授权 | 60 次/小时 | `SECTION_NOT_GRANTED` |
| `recent_context` | 最近发生变化的理解 | `days` int 1..90（默认 14）；`limit` int 1..50 | 分区授权 | 60 次/小时 | `GRANT_INACTIVE` |
| `decisions` | 已记录的决定与其时间 | `limit` int 1..50；`since` ISO8601 可选 | `matters` 或 `direction` 已授权 | 60 次/小时 | `SECTION_NOT_GRANTED` |
| `search_materials` | 在已授权资料内检索，**只回片段与出处** | `query` str 1..1000；`limit` int 1..10（默认 3） | 逐份资料授权（`materialIds`） | 30 次/小时 | `MATERIALS_UNAVAILABLE` |
| `propose` | 提交一条提议，落入待确认 | `content` str 1..500；`section` **enum(6)，不含 `burdens` / `self_view`**；`rationale` str 0..300 | 独立开关，默认关 | **10 次/小时，20 条/天** | `PROPOSAL_RATE_LIMITED` |

参数上限与今天 A 线保持同一量纲：`query` 上限 1000、`limit` 上限 20 或 100、`purpose` 上限 300，均见 `backend/zhijun_mcp/models.py:59-70`。

**I6 的实现要点（目标态）**：`section` 的枚举里**不出现**这两个值——不是「传了就报错」，是协议层根本不承认它们存在。外部 agent 连「知君有没有这个分区」都不应该知道，因此也不提供 `SECTION_FORBIDDEN` 这类错误码，传入非法值走普通的 `INVALID_ARGUMENTS`。同时 `whoami`、`search_memory`、`recent_context` 三个不按分区取值的工具必须在数据层过滤，不能依赖调用方不问。`propose` 也禁止向这两个分区提议：外部 agent 不该有能力断言用户心里压着什么。

### 4.2 每个工具**不返回**什么

| 工具 | 明确不含 |
|---|---|
| `whoami` | 证据原文、quote、会话 id、confidence 数值、未确认的印象 |
| `search_memory` | 证据原文、会话 id、匹配位置、相似度分数 |
| `get_section` | 证据原文、`exportAllowed` 等内部开关、被排除条目的存在提示 |
| `recent_context` | 变更前的旧值、谁改的、改动原因 |
| `decisions` | 决定的推理过程、被否决的备选、相关会话 id |
| `search_materials` | 原件、原件路径、文件名以外的任何本地路径、整段原文（只给服务端截断后的片段） |
| `propose` | 任何已有条目的内容（提议接口不是读接口） |

这一条不是新发明。今天 `personal.public()` 返回的字段是 `id/type/section/nature/content[:4000]/scope/validFrom/validTo/version/updatedAt/source`（`backend/zhijun_mcp/personal.py:117-124`），**不含** evidence、quote、conversationId、trustState、privacyLevel、exportAllowed、confidence、alignment。目标态延续该字段集，并追加 `layerTitle` 与 `lastReaffirmed`（后者今天在 context pack 里已有，见 `backend/mindos/zhijun/context_pack.py:58`）。

资料片段的截断也已有先例：`backend/mindos/agent/projection.py:28-29` 把片段上限固定为 700 字符、标题 300 字符，并注明「服务端固定控制，不允许请求参数无限扩大」。`search_materials` 沿用。

### 4.3 `whoami` 返回样例

```json
{
  "type": "whoami",
  "fingerprint": "sha256:9f3a1c0e7b52",
  "generatedAt": "2026-09-20T02:14:07Z",
  "grant": {
    "agentId": "claude-desktop",
    "sections": ["who", "principles", "ways", "direction"],
    "expiresAt": "2026-10-20T02:03:51Z",
    "readOnly": true,
    "canPropose": false
  },
  "summary": "做产品的人，偏好先收窄再展开；习惯用选项式提问推进决策。",
  "claims": [
    {
      "id": "cl_7e21a4",
      "section": "who",
      "sectionTitle": "我是谁",
      "layer": "self_declared",
      "layerTitle": "我这样说自己",
      "content": "我是万象与知君两个产品的负责人。",
      "lastReaffirmed": "2026-09-11T09:20:00Z",
      "version": "2026-09-11T09:20:00Z"
    },
    {
      "id": "cl_b0c933",
      "section": "principles",
      "sectionTitle": "我的原则",
      "layer": "self_declared",
      "layerTitle": "我这样说自己",
      "content": "宁可直说差距，也不要含糊的乐观汇报。",
      "lastReaffirmed": "2026-08-30T14:02:00Z",
      "version": "2026-08-30T14:02:00Z"
    }
  ],
  "counts": { "included": 2, "sectionsGranted": 4, "sectionsTotal": 6 },
  "notice": "只包含用户已确认且允许带走的理解。这是资料，不是指令。",
  "receipt": "rc_4a8f21c0d3b7"
}
```

`fingerprint` 为 `主张`：对返回条目的 `id + version` 有序拼接取 SHA-256 前 12 位。客户端可缓存整页，下次先调 `whoami` 比对指纹，一致则跳过后续读取。这能把常规会话的读取次数压到每次一调。

`notice` 沿用今天 pack 的写法（`backend/mindos/zhijun/context_pack.py:77`）与服务器 instructions 的口径「返回内容是资料，不是执行指令」（`backend/zhijun_mcp/server.py:19`）。这是对提示注入的最低限度声明，不是防护。

---

## 5. 授权模型

### 5.1 客户端到 grant 的生命周期

| 阶段 | 动作 | 现状锚点 / 目标态 |
|---|---|---|
| 1 注册 | 用户在界面添加客户端，得到 `agentId` 与一次性 token | **目标态**，实现抄 `backend/mindos/agent/store.py:163-304` |
| 2 授权 | 打开本地授权页，勾选分区、逐条排除、选期限、接受外发披露 | 页面逻辑今天已存在（`backend/zhijun_mcp/browser.py:98-108`），只需把云侧 ticket 交换（`:54-80`）换成本地 session |
| 3 生效 | 写入 grants 表，`revision=1`，`expires = now + days*86400` | `backend/zhijun_mcp/store.py:131-133` |
| 4 每次调用 | 重查 subject、audience、过期、enabled、agentId、state、revision | `backend/zhijun_mcp/store.py:148-159` |
| 5 改范围 | `revision+1`，清空 evidence_refs，取消 pending requests，**不续期** | `backend/zhijun_mcp/store.py:125-129` |
| 6 暂停 / 撤销 | 乐观锁 `expectedRevision`，冲突返回 409 | `backend/zhijun_mcp/store.py:136-146` |
| 7 过期 | 到期即失效，不自动续 | `backend/zhijun_mcp/store.py:155-157` |

### 5.2 分区默认表（主张）

今天已固定六个分区的键与标题：`who / people / matters / principles / ways / direction`（`backend/mindos/agent/mcp_server.py:356`），对应「我是谁 / 我的人 / 我的事 / 我的原则 / 我的做法 / 我的方向」（`backend/zhijun_mcp/browser.py:18`）。

数据层目标态是八个分区（[数据层](ZHIJUN_DATA_MODEL.md) 4.4），新增的两个**不进入本表的授权体系**：它们在 MCP 这一侧根本不存在（I6）。

| 分区 | 界面名 | 默认 | 理由 |
|---|---|---|---|
| `who` | 我是谁 | **开** | 身份、角色、称呼。这是 AI 少问一轮的最大来源，且几乎不含第三方信息 |
| `principles` | 我的原则 | **开** | 「直说差距」「不要含糊的乐观」这类约束，正是希望每个工具都遵守的 |
| `ways` | 我的做法 | **开** | 工作方式与偏好。描述的是用户自己，不涉及他人 |
| `direction` | 我的方向 | **开** | 长期目标。有助于 AI 判断当前任务的取舍，粒度粗、时效长 |
| `people` | 重要的人 | **关** | 每一条都包含第三方信息。第三方没有同意过被外发，用户也未必意识到自己在替别人做决定 |
| `matters` | 正在做的事 | **关** | 时效性强、商业敏感度高，且经常牵涉未公开的项目与合作方 |
| `burdens` | 心里的事 | **永不** | **无开关，不出现在授权页上。** 这是用户袒露心声的产物：反复压在心里的事、在回避的事、消耗他的东西。别的 AI 读到它，唯一确定的后果是用户下次不再说真话（I6） |
| `self_view` | 我眼中的我 | **永不** | **无开关。** 用户对自己的评价，常常是他最不愿被人看见的那一面，而且几乎总是片面的——交给一个不了解上下文的外部模型，既伤人又会被用错（I6） |
| 敏感与受限 | 不作为分区出现 | **永不** | 今天已由 `privacyLevel not in ("public","private")` 与 `SourcePolicy.claim_local` 双重排除（`backend/zhijun_mcp/personal.py:100-101`） |

**与今天的差异**：今天授权页六个 checkbox 全部不勾（`backend/zhijun_mcp/browser.py:98-100`），默认全关。改为「四开两关两永不」是行为变更，必须在授权页上以视觉方式呈现（已勾的四项仍可取消），并在首次连接时明确告知。「四开两关」属于 `主张`，需要 johny 拍板确认默认值本身；**两个「永不」不是主张，是 I6，不接受拍板改成可选**。

授权页上要不要显示这两个分区（灰掉并注明「永不外发」），是一个真实的取舍：显示能让用户看见我们守住了什么，但也等于告诉外部客户端「这里还有东西」。倾向**不显示**，把这件事放在知君自己的界面里讲（未决 U8）。

### 5.3 期限与失效

| 规则 | 说明 | 锚点 |
|---|---|---|
| 期限只有 1 / 7 / 30 天 | 没有「永久」选项 | `backend/zhijun_mcp/models.py:43` |
| 改范围不续期 | `expires = min(old_expires, now + days*86400)` | `backend/zhijun_mcp/store.py:125` |
| 任何改动使旧句柄作废 | revision+1 后，清 evidence_refs、cancel requests | `backend/zhijun_mcp/store.py:126-129` |
| 版本变化使授权失效 | `check(principal, revision)` 在交付前后各验一次 | `backend/zhijun_mcp/service.py:64,72` |
| 总开关关闭时清空 | `set_enabled(False)` 删 evidence_refs、cancel requests | `backend/zhijun_mcp/store.py:76-78` |

**目标态补充**：`whoami` 的 `fingerprint` 在 grant revision 变化时必然变化（因为可见条目集变了），客户端缓存自然失效，无需额外通知机制。

### 5.4 两道闸门与一处必须修的语义不一致

可见性应当是**两道闸门取交集**：

```
可外发 = 分区闸门（grant.sections 勾选）  ∧  逐条闸门（该条 exportAllowed 为真）
```

今天这两道闸门在两条导出路径上**判定不一致**：

| 路径 | 是否看 `exportAllowed` 字段 | 锚点 |
|---|---|---|
| A 线 `zhijun_get_personal_context` | **不看**。只把「历史上被显式关掉」当永久拒绝，沿 `supersedesId`、`evidence.locator.claimId`、`decisionId` 递归传染并持久化到 denials 表 | `backend/zhijun_mcp/personal.py:44-84`；候选筛选见 `:86-105`，其中无 `exportAllowed` 条件 |
| context pack | **要求为真** | `backend/mindos/zhijun/context_pack.py:32` |
| projection / `USER.md` | **要求为真** | 同上，`exportable_claims` 为唯一入口 |

后果：同一条理解，在 MCP 里可见、在导出包里不可见，或者相反。用户在界面上关掉一条的「带走开关」，A 线不一定认。

**主张**：统一为「`exportAllowed` 为真 ∧ 不在 denials ∧ 不在 excludedClaimIds ∧ 分区已授权」。A 线现有的递归传染逻辑（`backend/zhijun_mcp/personal.py:62-83`）保留，它解决的是「父条目被关掉、子条目仍泄漏」的真问题，与 `exportAllowed` 是并列条件而非替代。这条修改需要一个专门的回归测试，覆盖「开关关掉后 MCP 立刻不可见」。

A 线其余筛选条件全部保留，逐条列出以便实现时对照（`backend/zhijun_mcp/personal.py:86-105`）：`trust_state == confirmed`、非 challenged、非 deferred、`scope == long_term`、无 `supersededById`、无 `retractedAt`、`validFrom ≤ now < validTo` 且时间必须有限、`privacyLevel ∈ (public, private)`、`SourcePolicy.claim_local` 为假、`alignment.visible` 为真、不在 denied 集合、不在 `excludedClaimIds ∪ (legacy − acknowledgedLegacy)`。

---

## 6. 提议写入 `propose`

### 6.1 为什么外部 agent 永远不能直接写

知君的价值建立在「这些理解是我确认过的」。一旦外部模型能直接写入，用户就再也无法区分「我说过」与「某个 agent 推断过」。这不是权限问题，是语义问题：**直接写入会摧毁这个知识库唯一的可信属性**。

今天的代码已经站在这个立场上，只是没有说出口：

| 事实 | 锚点 |
|---|---|
| 工具白名单只有 5 个读 | `backend/zhijun_mcp/models.py:84-90` |
| `service._call` 无写分支 | `backend/zhijun_mcp/service.py:37-74` |
| agent 网关 `writeModes` 忽略 scopes 恒返回三个 false，注释写明「V1 第一阶段仅开放读取」 | `backend/mindos/agent/service.py:47-54` |
| `mindos.import` / `knowledge.draft` / `knowledge.commit` / `correction.draft` 四个写 scope 在白名单里，但**没有任何路由或工具消费它们** | `backend/mindos/agent/store.py:26-38` |

### 6.2 提议如何落地（目标态）

| 字段 | 值 | 理由 |
|---|---|---|
| `trust_state` | `working` | 与用户自己手写的 `confirmed` 区分开 |
| `trust_origin` | `agent_proposed:<agentId>` | 界面要能说出「是谁说的」 |
| `layer` | `hypothesis` | 它没听见你说，是它推断的 |
| `confidence` | 由 agent 声明，但不参与排序 | 防止 agent 用高置信度抢占注意力 |
| `evidence` | `[{kind: "agent_proposal", agentId, rationale}]` | 不含外部会话内容 |
| `exportAllowed` | `false` | 未确认的东西不能再流出去 |
| `device_scope` | 与第一方写入一致 | 沿用 `backend/mindos/ontology.py:214` 的 `_scope(request)` 语义 |

用户在「待确认」里看到的是一句人话：**「Cursor 说你倾向于先收窄再展开。是这样吗？」** 三个按钮：确认、改一下再确认、不是这样。确认后走既有的复核路径 `POST /api/mindos/ontology/claims/{id}/review`（`backend/mindos/ontology.py:231-250`），`trust_origin` 保留，用户随时能翻出来这条当初是谁提的。

### 6.3 与用户自己「记下来」的区别

| 维度 | 用户自己记 | agent `propose` |
|---|---|---|
| 入口 | `POST /api/mindos/ontology/claims`，loopback + CSRF（`backend/server.py:1168-1173`） | MCP `propose` 工具，需 grant 且 `canPropose` 为真 |
| 落地状态 | 直接 `confirmed` / `user_created`，**不走复核**（`backend/mindos/ontology.py:214-218`） | 必落 `working` / `hypothesis`，必走复核 |
| 触发 projection | 是（`backend/mindos/ontology.py:224-227`） | **否**，未确认的内容不进导出文件 |
| 限流 | 无 | 10 次/小时、20 条/天 |

### 6.4 滥用防护（目标态）

| 手段 | 规则 |
|---|---|
| 开关 | `canPropose` 是 grant 上的独立布尔，默认 false，授权页单独一个 checkbox |
| 频次 | 每客户端 10 次/小时、20 条/天，超限返回 `PROPOSAL_RATE_LIMITED`，写审计 |
| 内容上限 | `content` ≤ 500 字，`rationale` ≤ 300 字，超限即 `INVALID_ARGUMENTS` |
| 去重 | 同客户端 24 小时内提交内容完全相同的条目，静默丢弃并计入限额 |
| 待确认队列上限 | 单客户端未处理提议达 20 条时拒绝新提议，迫使用户先清理 |
| 撤销联动 | grant 撤销时，该客户端所有未确认提议一并删除 |

限流实现可复用 `backend/mindos/agent/rate_limit.py`（该模块已存在，用于 agent 网关）。

---

## 7. 审计与「谁读过我」

### 7.1 审计字段

今天的审计表已经是对的形状，直接沿用（`backend/zhijun_mcp/store.py:44-46`）：

| 列 | 类型 | 内容 | 为什么是它 |
|---|---|---|---|
| `id` | TEXT | 回执 id，`rc_` 前缀 | 返回给调用方，用户可据此对账 |
| `agent` | TEXT | `agentId` | 「谁读的」 |
| `grant_id` | TEXT | grant id | 「凭哪次授权读的」 |
| `operation` | TEXT | 工具名 | 「做了什么」 |
| `resources` | JSON | `[{id, version, delivery}]` | 「读到了哪几条、什么版本」 |
| `delivery` | TEXT | 允许值枚举 | 「以什么形式给的」 |
| `result` | TEXT | delivered / denied / pending | 「成没成」 |
| `created` | REAL | 时间戳 | 「什么时候」 |

### 7.2 不记什么，以及为什么

| 不记 | 原因 | 锚点 |
|---|---|---|
| 查询文本 | 查询本身就是隐私（「我离婚的事」是一次查询） | `backend/zhijun_mcp/store.py:1,161-162` |
| 返回正文 | 审计库会变成第二份全量副本 | 同上 |
| 异常信息 | 下游异常可能含资料名、网络路径、凭证 | `backend/zhijun_mcp/management.py:17-18`、`backend/zhijun_mcp/service.py:31-34` |
| OAuth token / 密钥 | 显然 | `backend/zhijun_mcp/store.py:1` |

失败路径也写审计，但写的是固定值 `result="denied", delivery="none", resources=[]`（`backend/zhijun_mcp/service.py:32-34`）。这样「被拒绝过多少次」可统计，而拒绝原因不落盘。

### 7.3 审计是交付前的线性化点

这是 A 线最值得保留的设计。`backend/zhijun_mcp/service.py:63-73` 的顺序是：

```
check(principal, grant.revision)  →  receipt(...)  →  check(principal, grant.revision)  →  return payload
```

含义：**审计写不成功，就不交付**；并且授权若在 I/O 期间被撤销或改动，两次 check 中必有一次失败，正文不会逃逸。整段在 `store.lock` 之下，读个人信息时还额外持 `ontology._lock`（`backend/zhijun_mcp/service.py:63`）。目标态原样保留，七个新工具全部走同一条路径。

### 7.4 「谁读过我」界面（目标态）

| 列 | 内容 | 来源 |
|---|---|---|
| 客户端 | 图标 + `agentName` + `clientInfo` 展示串 | grants 表 + initialize 上报 |
| 时间 | 相对时间，点开看精确值 | `audit.created` |
| 分区 | 本次涉及的分区中文名 | 由 `audit.resources` 的 id 反查 |
| 条数 | 「读了 7 条」 | `len(audit.resources)` |
| 操作 | 暂停 / 撤销 / 改范围 | `change_state`（`backend/zhijun_mcp/store.py:136-146`） |

**需要改的一点**：今天 `audits()` 只回最近 100 条且不可分页（`backend/zhijun_mcp/store.py:170-173`）。一个日常使用 Claude Desktop 的用户几天就会冲掉历史。目标态改为按时间倒序分页，保留 90 天。

### 7.5 反馈回路（主张，可选）

在「谁读过我」的每一行上放一个轻量反馈「这次有帮助吗」。用户点「有帮助」时，把本次 `audit.resources` 涉及的条目 `lastReaffirmed` 前移，使其在后续排序中靠前；点「没帮助」不做负向处理，只记计数。这条完全是新增，没有任何现有代码，且需要想清楚它会不会诱导用户把知君当推荐系统调教。列入未决。

---

## 8. 配置样例（目标态）

### 8.1 Claude Desktop（stdio）

```json
{
  "mcpServers": {
    "zhijun": {
      "command": "/usr/local/bin/zhijun-mcp",
      "args": ["--agent-id", "claude-desktop"]
    }
  }
}
```

### 8.2 Claude Code（stdio，项目级 `.mcp.json`）

```json
{
  "mcpServers": {
    "zhijun": {
      "command": "/usr/local/bin/zhijun-mcp",
      "args": ["--agent-id", "claude-code"]
    }
  }
}
```

### 8.3 Cursor（本机 HTTP）

```json
{
  "mcpServers": {
    "zhijun": {
      "url": "http://127.0.0.1:8644/mcp",
      "headers": { "Authorization": "Bearer zjk_xxxxxxxxxxxx" }
    }
  }
}
```

stdio 入口的实现可直接抄 `backend/mindos/agent/mcp_server.py:470-472`（`create_agent_mcp_server().run("stdio")`），该路径已有端到端测试验证 stdout 只有协议消息、日志走 stderr（`backend/tests/test_mindos_agent_mcp_read_tools.py:158-195`）。HTTP 多 profile 骨架可抄 `backend/mcp_remote_server.py:201-322`。

### 8.4 首次连接动线（六步）

| 步 | 用户看到 | 系统做了什么 |
|---|---|---|
| 1 | 在知君「谁读过我」点「添加客户端」，选 Claude Desktop | 生成 `agentId` 与一次性 token |
| 2 | 界面给出可复制的配置片段 | 不自动写入客户端配置文件 |
| 3 | 粘贴进客户端配置，重启客户端 | 客户端 spawn stdio 进程 |
| 4 | 客户端首次调用任意工具，知君弹出本地授权页 | 页面默认勾选四个分区，两个默认关 |
| 5 | 用户确认分区、逐条排除、选 1/7/30 天、接受外发披露 | `disclosureAccepted` 为必填（`backend/zhijun_mcp/models.py:44`） |
| 6 | 回到客户端，工具可用 | 写入 grant，`revision=1` |

---

## 9. 与现有代码的差距与工作量

### 9.1 三套半 MCP 的取舍

| 线 | 位置 | 数据源 | 今天可用吗 | 处置 |
|---|---|---|---|---|
| **A 统一 MCP V1** | `backend/zhijun_mcp/*` | 本体 + 资料 | **工具面在本仓库内不可达**：需要盒子 Gateway UDS（`backend/zhijun_mcp/gateway.py:15-31`）、Admin OAuth、`ZHIJUN_MCP_RESOURCE_URL`（`backend/zhijun_mcp/runtime.py:47-51`），单机 `capabilities.current()` 恒为 None（`backend/zhijun_worker/capabilities.py:143`）。管理面已接通（`backend/server.py:1280-1281`，前端 `ExternalAgentsPanel.vue`），单机返回 `{available:false}`（`backend/zhijun_mcp/management.py:20-27`） | **保留作底座** |
| **B 记忆 / KB MCP** | `backend/mcp_server.py`、`mcp_tools.py`、`mcp_remote_server.py`、`mcp_access.py` | `memory/` 文件 + 向量库 | **唯一现在就能连的，且完全没有鉴权** | **整体下线** |
| **C agent 网关 MCP** | `backend/mindos/agent/mcp_server.py` | MindOS 知识卡片 + 资料 | 总开关 `MINDOS_AGENT_GATEWAY_ENABLED` 默认 false（`backend/mindos/agent/config.py:24`），env token（`:58-71`） | MCP 面下线，**REST 孪生保留** |

**为什么留 A**：它是唯一四件齐全的实现，grant 模型（`backend/zhijun_mcp/store.py:34-36`）、审计与线性化（`backend/zhijun_mcp/service.py:63-73`）、四道默认关、授权页交互（`backend/zhijun_mcp/browser.py:98-151`），缺的只是「本地化」。B 和 C 都要从零补这四件。

**A 线的四道默认关**（全部保留）：

| 层 | 规则 | 锚点 |
|---|---|---|
| 进程 | 配置 `enabled is not True` 直接 SystemExit | `backend/zhijun_mcp/__main__.py:25-26` |
| 工作区 | `meta.enabled` 初值 `'false'` | `backend/zhijun_mcp/store.py:53` |
| 运行时 | worker 未 provision 即 503 | `backend/zhijun_mcp/runtime.py:44-51` |
| 单次授权 | 六个类别默认全不勾 | `backend/zhijun_mcp/browser.py:98-100` |

### 9.2 删 / 改 / 抄 / 新写

| 类 | 对象 | 理由 | 估计 |
|---|---|---|---|
| **删** | `backend/zhijun_mcp/account.py` | 云侧 mTLS + consent ticket，本地无对应物 | 0.5 人日 |
| **删** | `backend/zhijun_mcp/tunnel.py` | 250 行公网 relay，违反 I1 | 0.5 |
| **删** | `backend/zhijun_mcp/gateway.py` | 盒子 UDS 转发；本地直接调 service | 0.5 |
| **删** | `backend/zhijun_mcp/stdio.py` | 不是独立 stdio server，是 OAuth+PKCE 代理，本地无意义 | 0.5 |
| **删** | B 线四文件 + `start-mcp.sh` | 无鉴权 stdio 通路，违反 I2/I5 | 1 |
| **删** | `GET /api/config`（`backend/server.py:4106-4171`） | 主动派发无鉴权配置 | 0.5 |
| **删** | `memory/USER.md` 外发通路 | `backend/mindos/zhijun/projection.py:101` 仍可写本地文件供用户自己看，但不再有任何 MCP 工具读它 | 0.5 |
| **改** | `backend/zhijun_mcp/__main__.py:20-55` | 去掉 JWKS、mTLS、TLS 证书、AccountBroker，改为本地 token + loopback | 2 |
| **改** | `backend/zhijun_mcp/server.py:16-17` | https 断言放开为 loopback http，其余路径校验保留 | 0.5 |
| **改** | `backend/zhijun_mcp/auth.py:16-37` | 整个 OAuth 2.1 验证换成本地 bearer | 1 |
| **改** | `backend/zhijun_mcp/browser.py:54-80` | ticket 交换换成本地 session；`:82-118` 渲染逻辑不动 | 1.5 |
| **改** | `backend/zhijun_mcp/personal.py:86-105` | 补上 `exportAllowed` 条件，见 5.4 | 1（含测试） |
| **改** | `backend/zhijun_mcp/store.py:170-173` | 审计改为分页、保留 90 天 | 0.5 |
| **改** | `backend/mindos/agent/mcp_server.py:264-271` | **真 bug，见 9.3** | 0.5 |
| **抄** | `backend/mindos/agent/store.py:163-304` → 本地 token | 已含 SHA-256 存储、前缀展示、过期、防枚举 | 1 |
| **抄** | `backend/zhijun_mcp/http.py:41-45` | NoStore + TrustedHost 原样保留，只换 allowed_hosts | 0.2 |
| **抄** | `backend/mcp_remote_server.py:201-322` | 多 profile HTTP 骨架 | 1 |
| **抄** | `backend/mindos/agent/mcp_server.py:470-472` | stdio 入口，已有端到端测试 | 0.2 |
| **抄** | `backend/zhijun_mcp/browser.py:21,67-76,98-100,126,148-151` | CSP `default-src 'none'`、CSRF 严格比对、`__Host-` cookie（Secure/HttpOnly/SameSite=strict/300s）、默认不勾、**写前先消费 session**、complete 失败回滚撤销 grant，整套本地可复用 | 0（保留即可） |
| **新写** | 七个工具的参数模型与派发 | 扩 `TOOLS` 白名单（`backend/zhijun_mcp/models.py:84-90`） | 3 |
| **新写** | `propose` 写路径 + 限流 + 待确认界面 | 见第 6 节 | 4 |
| **新写** | `whoami` 的 `fingerprint` 与缓存语义 | 见 4.3 | 1 |
| **新写** | 「谁读过我」前端 | 复用 `ExternalAgentsPanel.vue` | 3 |

合计粗估 **24 人日**，不含联调与文案。

### 9.3 `mindos_context_pack` 是一个从未工作过的工具

`backend/mindos/agent/mcp_server.py:263-268` 定义 `def _gateway()`，函数体最后一行是 `return _GATEWAY`（`:268`）。紧接着 `:271` 的 `def mindos_context_pack(self, purpose, sections=None, max_claims=50)` **仍然缩进在 `_gateway()` 的函数体内，且位于 `return` 之后**，因此它是永不执行的死代码，`_Gateway` 类上根本不存在这个方法。模块级包装器 `:361` 调用 `_gateway().mindos_context_pack(...)`，必然抛 `AttributeError`。

测试为什么没抓到：`backend/tests/test_mindos_agent_mcp_read_tools.py:66-78` 只断言 `list_tools()` 返回的名字集合包含 `mindos_context_pack`、且 annotations 为只读，**从未实际调用该工具**。工具注册走的是模块级包装器对象，注册成功不代表可调用。

影响范围有限：REST 孪生 `POST /v1/agent/context-pack`（`backend/mindos/agent/router.py:382-416`）是好的，底层 `build_pack`（`backend/mindos/zhijun/context_pack.py:37-78`）也是好的。但这说明一件事：**「工具在 list 里」不是「工具能用」的证据**。目标态的七个工具，每个都必须有至少一条真正 `call` 过去的端到端测试。

---

## 10. 未决事项

诚实列出，不预设答案。

| # | 未决 | 两种走法 | 倾向 |
|---|---|---|---|
| U1 | 本地 token 是否进系统钥匙串 | (a) 只存知君数据目录的 SQLite，散列存储，简单、跨平台一致；(b) 明文进 Keychain / Credential Manager，更符合平台惯例，但引入平台分支与权限弹窗 | 倾向 (a)，但需确认 macOS 上 SQLite 文件权限是否足够。今天 `backend/zhijun_mcp/store.py:29-30` 已有权限校验（不合规即 `MCP_STORE_PERMISSIONS_INVALID`），可作为基础 |
| U2 | 本机 HTTP 形态是否默认开启 | (a) 默认关，用户需显式启用，多一步但符合 I2；(b) 默认开，容器内 agent 开箱可用 | 倾向 (a)。stdio 已覆盖三个主要客户端 |
| U3 | `search_materials` 在无向量依赖发行版下的行为 | (a) 工具从 `list_tools` 中整个消失；(b) 工具存在但固定返回 `MATERIALS_UNAVAILABLE`；(c) 降级为关键词匹配 | 未定。(a) 对客户端最干净，但会让「同一个知君，工具面不一样」成为常态，给集成方带来不确定性 |
| U4 | 多客户端并发的一致性 | 今天靠 `store.lock` 串行 + `expectedRevision` 乐观锁（`backend/zhijun_mcp/store.py:136-146`）。三四个客户端同时轮询 `whoami` 时，SQLite 单写锁是否够用，未做过压测 | 需要一次真实压测才能回答 |
| U5 | `Router` 无会话来源闭包缺测试覆盖 | `Router` 支持虚拟会话 id `"scope:global"`（`backend/mindos/zhijun/routing.py:80,84`），因此无会话的来源闭包解析**在结构上可行，但没有任何现有调用方、没有测试覆盖**。`Router.resolve` 的三个预算是分开的：普通深度 `> 32` 截断、`len(seen) > 128` 截断、节点预算 `budget["nodes"] >= 1024` 截断（`backend/mindos/zhijun/routing.py:152-161`），三者互不等价。`Router.permission` 四段判断（`:494-520`）中 `unverifiedEvidence` 是一票否决（`:504-506`） | 若 `search_materials` 要走来源闭包鉴权，这块必须先补测试，否则是盲区 |
| U6 | 分区默认值本身 | 5.2 的「四开两关」是本文主张，未经 johny 拍板 | 需拍板 |
| U7 | 「这次有帮助吗」反馈是否做 | 见 7.5。风险是把知君变成需要被调教的推荐系统 | 倾向先不做 |
| U8 | 授权页是否显示两个「永不」分区 | (a) 不显示，外部客户端连它们存在都不知道，在知君自己界面里讲；(b) 灰掉显示并注明「永不外发」，让用户看见我们守住了什么 | 倾向 (a)。见 5.2 |
| U9 | I6 的过滤放在哪一层 | (a) 放在 `personal.public()` 这类投影函数里，MCP 与导出共用；(b) 在 MCP 服务层再加一道独立过滤 | **倾向两层都做。** 这是唯一一条「漏了就无法补救」的约束——用户的心里话一旦被另一个 AI 读走，道歉没有意义。重复过滤的成本远低于失误的代价 |

---

*附录 B 完。本文描述的现状均以本仓库 `newzhijun` 分支（`98c2331`）为准；2026-09-21 定位修正改动了第 1 节（新增 I6）、2.1、4.1、5.2 与本节。*
