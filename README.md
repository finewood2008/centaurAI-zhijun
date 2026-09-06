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

Electron / 盒端集成当前采用完整产品 v2：原 15 个页面、20 条页面路由和 2 条重定向已装配到独立桌面，170 项受控操作通过主进程、Connectivity SDK 1.2.0 Direct 通道和盒端 Gateway 分发到独立知君领域 worker 或 DE 基础能力。原 Web 开发入口继续可用。架构和开发入口见 [架构图](docs/development/ARCHITECTURE-0905.md)、[集成与部署方案](docs/development/INTEGRATION-0905.md)、[桌面接口](docs/development/DESKTOP-CONTRACT-0905.md)、[领域规格](docs/development/DOMAIN-INTEGRATION-0905.md)。

**v2 正式盒子/UI 验收仍待完成，Admin 新应用生产发布路径尚未提供。** 新应用为 `zhijun-desktop / zhijun.workspace`，不能复用旧只读应用扩权。SDK 仍是整响应 request/close；聊天流、上传和导出使用盒端真实短任务、游标事件与分片，并非 SDK 原生流式接口。范围与进度见 [完整产品计划](docs/development/FULL-PRODUCT-INTEGRATION-0906.md)、[真实验收记录](docs/development/REAL-ACCEPTANCE-0906.md)。

早期 v1 只读桥保留兼容：真实登录、授权空资料页、刷新和一次重连已验证；这不表示非空历史资料、完整跨主体矩阵或 v2 已验收。[盒端历史部署](docs/development/BOX-DEPLOYMENT-0906.md)保留原版本和证据。2026-09-06 现有 DE 服务连接 FD 泄漏已单独热修并恢复 HTTP 200，见 [故障记录](docs/reports/CONNECTIVITY-FD-HOTFIX-0906.md)；该修复与 v2 产品发布分开。

安装 `frontend/mindos-web` 和 `frontend/shell` 依赖后，可用 `rtk proxy bash start-desktop.sh --simulation` 体验合成入口；真实启动使用 `rtk proxy bash start-desktop.sh --real`，仍受新应用登记、服务配置与实际 capability 控制。密码仅在应用内输入，配置与sidecar位于已忽略的 `data/desktop/`，不进入Git。启动和安装说明见 [桌面说明](frontend/shell/README.md)。

分配开发工作使用 [详细任务清单](docs/development/DEVELOPMENT-TASKS-0905.md)；测试、菜单覆盖和健康检查不能代替逐功能业务验收。

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

## 独立桌面、安装到主屏、盒子部署

- 桌面：仓库根执行 `rtk proxy bash start-desktop.sh --simulation` 体验合成流程；完整产品页面已接入受控 IPC。正式 v2 需要新应用与盒端 Gateway 配置，不能把 simulation 当成业务验收。
- 手机 / 平板：浏览器打开 `/mindos/` 可「添加到主屏幕」（PWA 清单），语音输入在 Chromium 系浏览器可用。
- 盒子：`deploy/box.env.example` 是环境变量样例（数据根、生产模式、本地模型、网关开关）。

## 数据在哪

以下为原本机 Web 数据路径；v2 正式领域改用 Gateway 配置的独立 domain root，每个 workspace 独立子目录，详见[领域规格](docs/development/DOMAIN-INTEGRATION-0905.md)。本机主要业务数据位于 `CENTAURAI_DATABASE_DATA_ROOT`（默认 `./data`）：`db/ontology.db`（实体/理解/证据，以及新增 work_* 事项、绑定、成果和历史表）、`db/conversations.db`（会话/消息/回执）、`db/growth.db`（章程/判断/复盘）。资料索引与向量在 `indexes/`、`chroma_data/`；`memory/ZHIJUN_PROFILE.md` 与 `USER.md` 是旧 global 范围的文件投影，设备视图另由 API 渲染。

密钥库默认在独立的 `./secrets`；metadata、gbrain、MCP 路径可被环境变量单独覆盖。开发 supervisor 的日志/进程记录在项目 `data/run/dev`；浏览器还有临时草稿，用户可显式复制/导出文稿。不能用“清空数据根”概括所有这些位置。旧本体清除和删除对话都不清新增成果副本及历史，新增数据的独立清除能力尚待设计，见同步审核记录。

## 架构一页

```
frontend/mindos-web/            Vue 3 + TypeScript + Vite；SSE 客户端 src/services/sse.ts
frontend/mindos-web/src/desktop/ 独立 hash router、连接 provider 与完整产品 transport
frontend/shell/                 Electron 37.10.3 宿主、preload、安全协议与 runtime
frontend/shared/{desktop-contract,product-contract}.ts  凭据隔离的公开桌面类型
frontend/shared/product-operations.json  170项受控操作清单
backend/zhijun_worker/           盒端独立workspace领域进程，经UDS/HMAC与DE协作
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

- 原 Web 产品的会话、本体、判断、章程、学习、事项/成果、资料/边界、搜索/图谱及偏好页面已装配进桌面；当前正在完成跨仓部署和真实逐功能验收，不能宣称已全量交付。
- 录音端口已实现明确按钮授权、最长120秒、16kHz PCM16单声道WAV、盒端本地转写并填入草稿；不自动发送消息。盒端voice API已用合成WAV实测通过；真实麦克风、平台权限和正式SDK/UI仍待实测。
- 隔离真盒已通过hardware-candidate5全部5项与gateway-candidate6全部10项（60请求、21个completed操作，含知识CRUD/confirm/search/purge）；均为合成主体/输入。首次失败、修复及189项资料相关回归见[审核记录](docs/reports/FULL-PRODUCT-GATEWAY-REVIEW-0906.md)，正式Consumer/SDK/UI验收仍待Admin生产发布入口与匹配部署。
- 外部模型由DE统一管理，外发必须绑定来源预览、配置和真实同意；模型质量、真实调用、取消及来源撤销仍需验收。
- 知君新领域目录按账号/设备/所有权代次隔离，DE能力负责canonical资料和模型。旧global不自动迁移，新领域目录不进入DE个人记忆退役清理范围。
- 原事项/成果新cursor分页、增强业务幂等和清除流程仍是独立规划增量，不能把原列表/history或旧ontology purge描述为已具备这些能力。承诺提醒、完整议题线程、BLE配网等未实现产品规划不因本次传输接入自动完成。
