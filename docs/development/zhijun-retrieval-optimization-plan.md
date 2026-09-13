# 知君检索优化实施计划

后续发布：用户另行授权的 0.1.18 安装包与公司盒端更新已完成，见 [发布记录](release-0.1.18-retrieval.md)。下文“不部署、不打包”描述的是原开发阶段边界，不代表后续发布状态。

## 新版 REST 合同对齐（已实现，未部署）

依据：Data Engine 仓库 `docs/development/contracts/知君调用Data-Agent-RAG-V2接口指南.md`（2026-09-13）。
当前接入不依赖 Tools/Function Calling；内部工具只作为 REST 薄适配。保留上一阶段所有已确认修改。

实施分工与验收：

- [x] 独立 Query：`retrieval_tools.py`、Query 测试/样例；指代改写不得拼接历史、歧义要求补充；主代理补检索触发。
- [x] 独立检索尝试：`data_agent_rag.py`、后端状态机测试；稳定对话请求/审阅句柄与实际 Search ID 分离，重新 Search 换 ID、Confirm 重试不换幂等键。
- [x] 检索专用边界：worker 附件/事件、chat_imports 相关入口及测试；新版接入拒绝上传/任务轮询/直接材料理解，保留连接和模型出口控制。
- [x] 可选规则应用状态：RAG REST 客户端、规则路由/设置组件与测试；低频 active/applying，不阻塞聊天。
- [x] 主代理集成：检索触发、前端错误/澄清提示、附件入口隔离、合同回归、文档更新。

仅修改知君，不修改 Data Engine、不部署、不打安装包、不提交 Git。
验证使用合成响应和隔离测试库；后端 pytest、前端测试、类型检查、桌面前端生产构建、diff 检查。不得调用真实付费模型或读取客户材料。

## 范围与依赖

- 仅修改知君仓库；不修改 Data Engine，不部署盒端，不打包、不提交仓库。
- 复用 `/v1/agent/apps/search`、Confirm、Evidence Resolve 和长期 App 凭据。
- 使用知君后端固定工具薄适配，直接调用现有 REST API；不要求 Data Engine 增加 Tools、MCP 或 Agent 编排能力，也不改变模型协议或绕过出口授权。
- Data Engine 检索算法、索引、切块、模型部署由另一团队负责。

## 验收标准

1. 普通检索结果也须由用户选择后才能进入回答模型；支持不使用材料继续与取消。
2. 敏感确认、风险放行与材料使用选择分层；未确认正文、确认令牌和凭据不进入模型请求或日志。
3. Query 仅结合获准的相关用户上下文消歧，保留当前问题，不引入助手猜测；指定材料范围不得扩大。
4. 查询通过固定工具映射与参数校验执行，默认 Top-K 5，避免对话预览/发送重复检索与不同 Query 分数混排。
5. Data Engine 原生证据不因缺少知君本地登记而丢失；缺失/变化的证据及外发授权仍应拒绝，不能降级为未检查文本。
6. 用户确认/取消不重复创建消息，切换账户、设备或会话时不复用确认；过期重新检索。
7. 现有本地/云端流式模型协议、隐私门禁保持兼容；无相关资料与敏感暂扣分别提示。

## 工作流与文件归属

- [x] Query 与受控工具：新增 `backend/mindos/zhijun/retrieval_tools.py`、对应测试与离线Query评测样例。
- [x] 后端确认：`backend/mindos/data_agent_rag.py`、`backend/mindos/chat_import_routes.py`、对应测试。
- [x] 前端确认：`services/taskRouting.ts`、`components/conversation/RagSensitiveDialog.vue`、`App.vue`、`composables/useChatImports.ts`、`pages/ConversationPage.vue`、对应测试。
- [x] 主流程集成：`context_sources.py`、`context_plan.py`、`routing.py`、`context_lookup.py`、`turn.py`、对话集成测试。
- [x] 最终回归结果记录（见下方）；真实盒端联调与视觉验收尚未执行。

## 验证策略

- 使用隔离测试数据和模拟Data Agent响应验证状态机；不读取客户材料或调用付费云端模型。
- 覆盖安全结果、无结果、选择子集、脱敏/原文、混合暂扣、过期、重复点击、权限变化、取消及账户隔离。
- Query覆盖指代、主题切换、编号、版本、用户限定范围、历史未获准和助手编造信息。
- 离线规则测试不冒充真实中文检索准确率；真实索引质量评测由跨团队联调另行执行。

## 进度

- 当前实施保留上一阶段未提交修改；现有 RAG 客户端与确认界面继续复用。
- 新版合同为 REST-only；Tools/Function Calling 不是本次接入前提。
- 第一阶段已实现并通过下述自动化回归，未部署。

## 第一阶段自动化验证结果（2026-09-13，新版增量验证见文末）

- 后端针对性回归：**274 passed，39 subtests passed**。覆盖新工具/材料审阅/真实对话编排以及旧 RAG 客户端、路由、上下文、worker 端口。
- 前端 `npm run test:all`：**150 passed**。
- 前端 `npm run typecheck` 与 `npm run build:desktop`：通过。Vite 提示 taskRouting 同时静态/动态导入，不影响构建；没有生成 DMG/EXE 安装包。
- `git diff --check`：通过。
- 性能断言：5 个选中片段的重放组装最多 2 次批量 Resolve（重放与组装校验）；单次最终模型校验为 1 次批量 Resolve。下一次边界若证据改变仍必须失败，不靠长期缓存跳过验证。
- 完整模拟 `run_turn` 覆盖确认前无模型/无消息、确认后流式成功、同 requestId 重放不重复创建消息或模型调用。
- 测试使用隔离数据库与合成材料。后端借用相邻仓库现成虚拟环境作为解释器，没有修改相邻仓库代码或依赖。

后端复跑（在 `backend` 目录，激活有 pytest/FastAPI 的 Python 环境后）：

```sh
python -m pytest -q tests/test_retrieval_chat_integration.py tests/test_retrieval_tools.py tests/test_rag_material_review.py tests/test_data_agent_rag_v2_flow.py tests/test_data_agent_rag_v2_client.py tests/test_context_plan.py tests/test_task_routing.py tests/test_context_lookup.py tests/test_context_lookup_turn.py tests/test_context_focus_state.py tests/test_memory_context_routing.py tests/test_routing_source_snapshots.py tests/test_zhijun_worker_ports.py tests/test_context_bridge.py tests/test_personal_context_retrieval.py
```

## 已落地行为

1. 对需要资料的对话，知君后端组织一个有界 Query，通过 `search_materials` 受控工具调用 Search；普通闲聊不强制检索。
2. 不指定材料时查询 App 授权且就绪的资料库，不再把知君本地导入登记表当作检索权限边界。用户指定材料时仅传该范围，不自动扩大。
3. 普通结果、空结果和策略禁止均进入材料审阅；敏感/未验证结果先走 Data Agent 原有确认，再进入材料选择。默认不选，可选部分、无材料继续或取消。
4. 展示检索问题、范围、材料名称/版本/位置、检测与交付状态。只预览前 500 字时明确标注总字数，以及选中允许使用完整检索片段；引用仍受上下文预算限制。
5. 选择材料不等于同意外发。沿用接收服务和实际请求的独立授权；未经验证原文保留风险说明，不适用默认云端授权，必须单独确认。
6. 预览、授权和发送复用同一请求的 Search 结果。每个同步校验边界内批量 Resolve、去重；最终模型出口开启新的校验边界，不使用跨请求的权限缓存。
7. 保持 Search/Confirm 返回顺序，不用公开分数重新排序材料；已经完成受控检索的本轮不再启动第二次模型补查。
8. 保留现有流式回答协议。确认阶段显示检索/审阅/授权进度；取消不会创建重复对话消息，切换会话会取消未结束的确认。
9. 历史记录区分用户最初选择的附件与实际使用的材料，只为实际采用的片段记录证据依赖，不能把未选附件当作本轮来源。

## 限制与后续联调

- **不是原生模型 Tool Agent**：当前是服务端编排的固定 REST 薄适配。本次无需 Data Engine 新增工具协议；知君不向 Search 发送 messages、history、用户身份或工具声明。
- **不声称已提升真实召回率**：新增 Query 回归验证上下文、指代和范围规则，未对真实客户索引做 Recall@K/nDCG 评测，也没有调用真实付费模型。重排/召回算法仍由 Data Engine 团队维护。
- Query 当前最多 1000 字、历史主题最多 400 字、指定范围最多 100 份。超出时明确拒绝，不静默截断问题或扩大范围。工具输入不接受网址、凭据或任意执行参数。
- 用户选择状态只在当前 worker 内短期保存（15 分钟），证据还受 Data Agent 自身有效期约束。过期/重启后，旧证据及依赖它的历史不会自动复用；需要明确重新检索、选择和授权。长期证据恢复和更直接的历史重新确认入口仍需后续设计，不能通过回退原文接口解决。
- 上下文预算、重复证据合并仍可能减少最终提供的片段；最终引用回执以实际输入为准，不把用户勾选数冒充已发送数。
- 本次没有修改 Data Engine、部署盒端、生成安装包或提交 Git；桌面前端生产构建仅作构建验证。尚未进行真实盒端端到端和浏览器视觉验收。

## 建议人工验收顺序

1. 普通闲聊直接回答；指定资料问题出现审阅且没有提前模型请求。
2. 两个结果仅选一个，最终回答和引用回执不能出现未选片段。
3. 分别验证脱敏、允许原文、检测未完成、无结果、策略禁止；未经验证原文还需单独外发确认。
4. 审阅时切换会话/账号/盒子或取消，再返回不应自动继续；同 requestId 重试不得重复创建消息。
5. 审阅后修改权限/规则/材料或重启 worker，旧证据不可继续使用，重新检索应得到新的确认。
6. 确认后检查流式首字和完整结束，核对 Search 次数与 Resolve 批次数，不只看模型平台有无请求。

## 新版合同增量行为

- 独立 Query：例如用户前文“MindOS 是什么？”、本轮“它有哪些核心功能？”改写为“MindOS有哪些核心功能？”。只从获准用户消息提取明确主题，不拼历史；无法确定时要求补充，不调用额外模型猜测。
- 明确的“检索／查找／搜索”触发材料检索，普通闲聊保持不检索。
- 对话请求和后端缓存句柄稳定；每次实际 Search 使用独立 `cid:search:<uuid>`。界面审阅使用实际 Search ID，旧弹窗无法领取新结果。普通 Confirm 网络重试保持幂等键，风险放行结果不明必须重新检索。
- 桌面端原材料、卡片、回收站、材料图谱等旧入口显示检索说明，避免发起旧材料管理调用；对话禁用上传、文件拖入和旧资料列表读取，保留语音转写。旧记录不删除，未完成导入终止并说明原因。非 workspace 旧服务兼容保留。
- 规则设置独立读取应用状态：仅页面可见时低频查询，active 停止，applying 不阻塞聊天。认证错误停止轮询，服务繁忙尊重 Retry-After，状态接口不可用不代表聊天不可用。
- 明确区分 App 认证、能力不足、原文策略拒绝、无效参数与服务繁忙，保留安全 traceId 和等待提示；不自动轮换凭据。

## 新版增量最终验证（2026-09-13）

- 后端针对性回归 **366 passed，55 subtests passed**：第一阶段所列测试，加 `test_rag_search_attempts.py`、`test_retrieval_only_boundary.py`、`test_sensitive_rule_status.py`、`test_sensitive_rule_routes.py`、`test_chat_imports.py`。
- 前端全部单元/合同测试 **164 passed**，含真实 SFC 逻辑和请求状态机模拟；不是盒端 E2E 验收。
- Shell 受控桥接、目录策略、安全和配额回归 **49 passed**（`business-bridge`、`security`、`product`、`product-quota-integration`）。
- 类型检查、桌面前端生产构建通过；只有原有静态/动态导入提示和 Python 依赖弃用警告。
- 安全复核发现的一次性风险领取错误提示已修正：结果不确定时明确不可重试旧令牌，不自动 Search 或 Confirm；用户重新发起后生成新检索。保留服务要求的 Retry-After。
- 前端旧入口隔离、后端禁上传/轮询、独立 Query、实际 Search ID、旧弹窗拒绝、新规则状态均有针对性回归。
- 未改动 Data Engine 仓库，未调用真实模型、未部署、未生成安装包、未提交 Git。下一步为真实盒端联调与界面验收。
