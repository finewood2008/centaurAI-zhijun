# 知君 · 通过对话认识你的良师益友

知君是一个通过对话逐渐认识你、并把这份「认识」交给你掌管的 AI。它记得你的人、事、原则和判断，在你要拿主意时陪你商量，在结果回来时陪你复盘，是你生活里一位有边界的良师益友。

- **对话优先**：本体（知君对你的理解）主要通过对话建立；导入资料只是加速器。
- **三层信任**：对话记录 → 工作理解（未确认，只能带保留语气用）→ 已确认本体（你亲口说的自动确认，其余一键确认）。
- **来源永远标清**：回复里的每一句都标 `【你告诉我的】` `【资料里看到的】` `【我推测的】` `【知君的看法】`；纠正过的理解永不回流。
- **本地优先**：数据都在设备上；调用外部模型时只发送必要片段，每轮有回执可查。

产品定义与路线：`docs/product/ZHIJUN_REDESIGN_V2.md`；接口契约：`docs/development/zhijun-api-contract.md`；原则与行为规范沿用 `docs/product/ZHIJUN_PRD_V1.md` §4、§7、§11、§12。

## 四个入口

| 入口 | 做什么 |
|---|---|
| 对话 | 一段持续的关系。首次使用先做一次 7 个问题的建档对话；之后每轮回复带出处条，知君新学到的理解以候选 chip 出现，一键 对 / 部分对 / 只适用于这件事 / 不对 / 先别存 |
| 我的本体 | 我是谁 · 我的人 · 我的事 · 我的原则 · 我的做法 · 我的方向，加「知君最近学到的」待确认列表；可改、可撤、可手写补一条 |
| 判断 | 人生章程、记录判断、结果与复盘（当下「商量」模式与回访在下一阶段） |
| 资料与边界 | 导入资料、模型与隐私设置、回收站、本体投影预览 |

## 快速开始（本机开发）

后端需要 Python 3.11；前端需要 Node 20+。

```bash
# 1) 后端依赖（首次）
python3.11 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt   # Intel macOS 装不上 torch 时，可先略过 torch/sentence-transformers：对话与本体不依赖它们

# 2) 前端构建（首次或改动后）
cd frontend/mindos-web && npm ci && npm run build && cd ../..

# 3) 启动后端（同时在 http://127.0.0.1:8618/mindos/ 提供前端）
./start-backend.sh
```

模型通道由环境变量 `ZHIJUN_PROVIDER` 选择：

| 值 | 说明 |
|---|---|
| （默认） | 设置页的对话通道：外部问答开启且 provider=openai 时走 OpenAI 兼容接口，否则走本地 Ollama（`ZHIJUN_LOCAL_NUM_CTX` 默认 8192） |
| `openai` | 强制 OpenAI 兼容通道（需在设置页配置 BaseURL / Key / Model） |
| `anthropic` | 官方 Anthropic SDK（`ANTHROPIC_API_KEY`，模型默认 `claude-opus-5`，`ZHIJUN_ANTHROPIC_MODEL` 可改） |
| `fake` | 演示模型：不调用任何模型服务，规则抽取，仅供联调与测试（生产环境拒绝启用） |

其它开关：`ZHIJUN_EXTRACTION=0` 关闭对话抽取；`ZHIJUN_MATERIAL_EVIDENCE=0` 关闭资料片段检索。

## 验证

```bash
# 后端单元测试（强制隔离数据根，不碰 data/）
backend/.venv/bin/python scripts/run_tests.py

# 知君竖切端到端：起真实后端（演示模型）→ 建会话 → 三轮 SSE → 抽取 → 确认 → 引用 → 撤回不回流 → 投影 → 前端可服务
backend/.venv/bin/python scripts/e2e_zhijun_phase1.py

# 前端类型检查、构建、node 测试
cd frontend/mindos-web && npm run typecheck && npm run build && npm run test:p14-frontend
```

## 数据在哪

所有可变数据都在 `CENTAURAI_DATABASE_DATA_ROOT`（默认 `./data`）下：`db/ontology.db`（实体 / 理解 / 证据 / 复核事件）、`db/conversations.db`（会话 / 消息 / 回执）、`db/growth.db`（章程 / 判断 / 复盘）、`memory/ZHIJUN_PROFILE.md`（已确认本体的可读投影）、`memory/USER.md`（允许导出的子集）。资料索引与向量在 `indexes/`、`chroma_data/`。删除数据目录即清空一切。

## 架构一页

```
frontend/mindos-web/            Vue 3 + TypeScript + Vite；SSE 客户端 src/services/sse.ts
backend/server.py               FastAPI 入口（仅绑定 127.0.0.1:8618，写路由要求 loopback + CSRF 头）
backend/mindos/zhijun/          对话 agent：provider · gate · persona · context · extract · jobs · projection · turn · confirm
backend/mindos/stores/          SQLite 存储：ontology_store · conversation_store · growth_store · …
backend/mindos/conversations.py /api/mindos/conversations（SSE）
backend/mindos/ontology.py      /api/mindos/ontology
backend/mindos/agent/           给第三方 Agent 的只读网关（REST + MCP），与对话 agent 无关
backend/{parser,embedder,watcher,vector_store}.py   资料摄取、解析、嵌入、索引（沿用）
```

## 现状与边界

- 本轮（P1）实现了「能聊、能记、能认」：多轮流式对话、从对话抽取理解、对话内一键确认、我的本体、建档对话、投影。**商量草稿面板、回访提醒、整合器、资料 → 理解、语音、移动端、硬件盒子尚未实现**，见 `docs/product/ZHIJUN_REDESIGN_V2.md` §10–§11。
- 真实模型（Ollama / OpenAI 兼容 / Anthropic）的通道代码有单元测试，但抽取质量需要在真实模型上评测后再放开默认。
- 旧的资料管理、知识卡片、搜索、图谱页面仍可通过 URL 访问（`/materials`、`/knowledge`、`/search`、`/graph`），不再出现在侧栏；`/api/mindos/qa` 单轮问答接口保留给 Agent 网关。
