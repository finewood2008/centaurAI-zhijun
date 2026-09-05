# 知君 · 有记忆边界的长期思考伙伴

知君是一个通过对话逐渐认识你、并把这份「认识」交给你掌管的 AI。它记得你的人、事、原则和判断，在你要拿主意时陪你商量，在结果回来时陪你复盘，是一位有记忆边界、可核对、不会替你决定的长期思考伙伴。

- **对话优先**：本体（知君对你的理解）主要通过对话建立；导入资料只是加速器。
- **三层信任**：对话记录 → 工作理解（未确认，只能带保留语气用）→ 由你核对、修订和确认的本体。
- **来源可核对**：区分用户表达、资料来源与知君推测，保留来源链和使用回执；展示清理不会解除来源限制。
- **本地优先**：数据都在设备上；调用外部模型时只发送必要片段，每轮有回执可查。
- **事情与成果**：把同一件事跨对话推进，将完整回复保存为可编辑文稿；准备提纲只填入输入框，保存文稿不自动调用模型或写入个人本体。

产品定义与路线：`docs/product/ZHIJUN_REDESIGN_V2.md`；接口契约：`docs/development/zhijun-api-contract.md`；原则与行为规范沿用 `docs/product/ZHIJUN_PRD_V1.md` §4、§7、§11、§12。

本工程维护于 `nexusaos-centuarai-zhijun`；源版本、迁移范围和验证记录见 [迁移说明](docs/development/MIGRATION.md)。

当前产品源已同步至 GitHub `22dc9a3`，本次新增功能、审核修复与验证结果见 [上游同步记录](docs/development/UPSTREAM-SYNC-0905.md)。

Electron SDK 与 data-engine 的后续集成调研见 [技术架构图](docs/development/ARCHITECTURE-0905.md)、[集成方案](docs/development/INTEGRATION-0905.md) 和 [二次审核记录](docs/development/REVIEW-0905.md)，其中区分了当前实现、接口缺口及建议实施步骤。

## 主要入口

| 入口 | 做什么 |
|---|---|
| 今日来信 | 首页展示来信、正在推进的事情与下一步，可回到相关对话继续推进 |
| 对话 | 一段持续的关系。首次使用先做一次 7 个问题的建档对话；之后每轮回复带出处条，知君新学到的理解以候选 chip 出现，一键 对 / 部分对 / 只适用于这件事 / 不对 / 先别存 |
| 事情与成果（对话内） | 显式新建/关联事项，记录目标、背景、下一步和结果；将完整回复留下为文稿，继续编辑、复制或下载 Markdown，保留原始来源限制 |
| 我的本体 | 新增按原文呈现的个人摘要，区分已确认与正在形成的理解；保留全景/列表及既有视图偏好，支持核对、修改、撤回、手写补充和处理冲突。事项进展与成果文稿不自动进入个人摘要 |
| 判断 | 人生章程、判断簿、结果与复盘；在对话里打开「我在考虑…」即进入商量模式，知君边聊边整理判断草稿，一键记进判断簿；到期后提醒你回访，回访会话里记下结果并引导复盘 |
| 资料与边界 | 导入资料（资料里的实体与关系会变成待确认的「资料里看到的」理解）、模型与隐私设置、回收站、本体投影预览、导出全部认识（JSON）、删除全部记忆、「可以带走的认识」（哪些理解会给其他 Agent、最近被谁以什么用途取走） |

## 快速开始（本机开发）

后端需要 Python 3.11；前端构建需要 Node 20+，运行前端测试需要 Node 22.6+（使用 `--experimental-strip-types`）。

```bash
# 1) 后端依赖（首次）
python3.11 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt   # Intel macOS 装不上 torch 时，可先略过 torch/sentence-transformers：对话与本体不依赖它们

# 2) 前端构建（首次或改动后）
cd frontend/mindos-web && npm ci && npm run build && cd ../..

# 3) 启动后端（同时在 http://127.0.0.1:8618/mindos/ 提供前端）
./start-backend.sh
```

本机 Web 开发也可使用 `bash zhijun.sh start` 同时管理后端与 Vite，配合 `status` / `stop`；入口使用本机开发数据，不是测试沙箱。当前依赖 macOS/POSIX 工具，平台范围、日志和已有进程处理见 [本机开发入口](docs/development/local-runtime.md)。

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
# 后端单元测试（按模块隔离；额外密钥/路径隔离见下方文档）
backend/.venv/bin/pip install pytest
backend/.venv/bin/python scripts/run_tests.py --isolated-modules -- -q

# 知君竖切端到端：起真实后端（演示模型）→ 对话/抽取/确认/撤回/投影 → 商量→草稿→判断簿 → 提醒 → 回访→结果 → 整合→裁决→张力提醒→导出 → 前端可服务
backend/.venv/bin/python scripts/e2e_zhijun_phase1.py

# 前端类型检查、构建、全部 node 测试（含新事项/发送恢复）
cd frontend/mindos-web && npm run typecheck && npm run build
node --experimental-strip-types --test tests/*.test.mjs
```

测试前按 [路径隔离说明](docs/development/local-runtime.md) 排除继承的外部数据路径并使用临时密钥目录。原 `e2e_zhijun_phase1.py` 有已记录的旧自动确认断言失败；本次执行的事项/发送恢复等隔离检查及范围见 [同步验证记录](docs/development/UPSTREAM-SYNC-0905.md)，不等同 SDK 真机联调。

## 给其他 Agent 的上下文包

其他 Agent（例如万象）通过只读网关拿「知君对你的认识」：只包含你已确认、并逐条打开了「可带走」的理解，敏感或受限内容永远不出去；调用方必须说明用途，每次都有回执。

```bash
export MINDOS_AGENT_GATEWAY_ENABLED=true          # 默认关闭
# 本机签发带 zhijun.profile scope 的令牌
curl -s -X POST http://127.0.0.1:8618/api/agent/clients -H 'X-Requested-By: centaur-vdb' -H 'Content-Type: application/json' -d '{"name":"wanx","scopes":["zhijun.profile"]}'
# 取上下文包（REST）；MCP 工具名 mindos_context_pack
curl -s -X POST http://127.0.0.1:8618/v1/agent/context-pack -H "Authorization: Bearer agk_…" -H 'Content-Type: application/json' -d '{"purpose":"帮用户整理本周计划"}'
```

## 桌面薄壳、安装到主屏、盒子部署

- 桌面：`cd frontend/shell && npm install && npm start`（只加载本机 `/mindos/`，没有 preload 与 IPC 桥）。
- 手机 / 平板：浏览器打开 `/mindos/` 可「添加到主屏幕」（PWA 清单），语音输入在 Chromium 系浏览器可用。
- 盒子：`deploy/box.env.example` 是环境变量样例（数据根、生产模式、本地模型、网关开关）。

## 数据在哪

主要业务数据位于 `CENTAURAI_DATABASE_DATA_ROOT`（默认 `./data`）：`db/ontology.db`（实体/理解/证据，以及新增 work_* 事项、绑定、成果和历史表）、`db/conversations.db`（会话/消息/回执）、`db/growth.db`（章程/判断/复盘）。资料索引与向量在 `indexes/`、`chroma_data/`；`memory/ZHIJUN_PROFILE.md` 与 `USER.md` 是旧 global 范围的文件投影，设备视图另由 API 渲染。

密钥库默认在独立的 `./secrets`；metadata、gbrain、MCP 路径可被环境变量单独覆盖。开发 supervisor 的日志/进程记录在项目 `data/run/dev`；浏览器还有临时草稿，用户可显式复制/导出文稿。不能用“清空数据根”概括所有这些位置。旧本体清除和删除对话都不清新增成果副本及历史，新增数据的独立清除能力尚待设计，见同步审核记录。

## 架构一页

```
frontend/mindos-web/            Vue 3 + TypeScript + Vite；SSE 客户端 src/services/sse.ts
frontend/mindos-web/src/services/{chatStream,matters}.ts  发送恢复编排与事项/成果API
backend/server.py               FastAPI 入口（仅绑定 127.0.0.1:8618，写路由要求 loopback + CSRF 头）
backend/mindos/zhijun/          对话 agent：provider · gate · persona · context · extract · jobs · projection · turn · confirm
backend/mindos/stores/          SQLite 存储：ontology_store · conversation_store · growth_store · …
backend/mindos/conversations.py /api/mindos/conversations（SSE）
backend/mindos/ontology.py      /api/mindos/ontology
backend/mindos/matters_routes.py /api/mindos/matters、/artifacts、会话/matter绑定
backend/mindos/agent/           给第三方 Agent 的只读网关（REST + MCP），与对话 agent 无关
backend/{parser,embedder,watcher,vector_store}.py   资料摄取、解析、嵌入、索引（沿用）
```

## 现状与边界

- P1「能聊、能记、能认」、P2「能商量、会回访」、P3「像良师」、P4「可带走、可安装」已实现：多轮流式对话、从对话抽取理解、对话内一键确认、我的本体、建档对话、投影、商量模式与判断草稿、到期提醒、回访记结果与复盘引导、整合器与裁决、张力提醒、资料 → 理解、导出与全量删除、给其他 Agent 的上下文包、可带走开关、语音输入、PWA 清单、桌面薄壳、盒子 profile。**承诺提醒、议题线程、移动端离线采集、录音转写、盒子硬件通讯、旧面退役尚未实现**，见 `docs/product/ZHIJUN_REDESIGN_V2.md` §10–§11。
- 真实模型（Ollama / OpenAI 兼容 / Anthropic）的通道代码有单元测试，但抽取质量需要在真实模型上评测后再放开默认。
- 持续事项、可编辑成果和有限发送恢复已同步；它们不等于完整议题线程/承诺任务系统，也尚未适配 Electron SDK。SDK 鉴权桥、流式通道、目录隔离等前置问题继续按集成方案推进。
- 旧的资料管理、知识卡片、搜索、图谱页面仍可通过 URL 访问（`/materials`、`/knowledge`、`/search`、`/graph`），不再出现在侧栏；`/api/mindos/qa` 单轮问答接口保留给 Agent 网关。
