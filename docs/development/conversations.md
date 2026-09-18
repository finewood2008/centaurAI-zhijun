# 对话、材料检索与回复恢复

## 会话

对话支持创建、加载、搜索、标题修改、置顶、归档和删除。列表元数据与消息流分开更新；迟到的读取或写入结果必须核对当前会话和请求代次，不能污染用户后来打开的会话。

输入草稿按会话保存。发送前会创建或确认目标会话、准备模型路由和来源授权；失败或取消时只恢复原提交所属会话的输入。用户在等待期间继续输入的文字优先保留，只有内容和来源都能安全合并时才自动合并。

## 当前桌面的材料检索

对话中的材料上下文使用 Data Agent RAG V2 的检索专用子集；这不等于知君产品只有检索入口。“资料与边界”保留原材料导入、文件夹、列表、处理状态、文件详情、知识卡片和回收站管理。桌面通过既有工作区鉴权与操作白名单调用 Data Engine 的材料管理能力，解析、索引和预识别仍由 Data Engine 完成，不在知君另建解析队列。

材料管理与对话使用是两个独立入口：上传、查看原件或确认知识卡片，不代表允许模型使用或外发该材料。对话输入框的历史自动附件上传/读取链路本次不恢复；需要讨论资料时，先在“资料与边界”导入，再在对话中检索、审阅和确认使用。材料管理接口不得作为 Search 或 Evidence Resolve 失败后的原文回退路径，敏感扫描启动与重试仍由 Data Engine 管理端负责。

- 需要资料时，知君组织独立 Query；仅使用获准的相关用户历史消解指代，不把完整 messages、身份或工具声明传给 Search。
- 普通闲聊不强制检索整个资料库；不限制材料时搜索当前 App 的授权就绪资料，指定范围只能缩小权限。
- 已允许交付的普通结果也须用户审阅，默认不选；可以选子集、不使用材料继续或取消。
- 在线模型只使用来源、用途、服务和实际请求均通过授权检查的必要内容。材料交付、材料选择和在线外发是三层不同决定。

检索和确认边界如下：

- 知君 RAG 数据面只使用 `/v1/agent/apps/*`；不以 `/api/mindos/rag-v2/*` 浏览器接口或 `/api/mindos/zhijun/*` 工作区控制协议回退读取原文。
- 检索工具是知君内部固定 REST 薄适配，不要求模型 Function Calling、MCP 或 `role=tool`。
- 只把允许交付且用户选中的 `items[].text` 作为材料上下文；最终使用前通过 `/evidence:resolve` 重新核对证据，过期或版本变化后重新检索，不自动读取原件。
- `sensitive_confirmation_required` 由用户选择脱敏交付、在具备能力时领取原文、不使用材料继续或取消；存在 `detectionNotice` 时，未完成检测的片段保持暂扣，风险放行还需要单独的明确确认。
- HTTP 200、空 items 和检测暂扣分别处理，不能把 `sensitive_check_unavailable` 当作 `no_results`。本地回答模型也不能绕过敏感交付确认。
- 每次实际 Search 使用新 interactionId；稳定轮次句柄不是 Search ID。普通 Confirm 的同一请求重试复用幂等键，重新 Search 或更改脱敏选择使用新键；旧界面确认不得作用于新结果。
- 风险领取一次性消费，响应丢失返回 `RAG_RISK_RESULT_UNKNOWN`，要求重新检索而非重放风险 Token。风险原文仍标记 unverified，外发须另行授权。
- 材料审阅状态仅在当前 worker 内短期保存，重启或过期后重新检索和选择；历史引用不自动获得永久可读权限。
- App ID/Secret 由盒端可信后端管理，普通确认 Token 和风险 Token 仅留在 worker 的短期状态中。默认长期凭据文件为 `$CENTAUR_SECRET_STORE_DIR/data-agent-rag-v2.json`，也可由 `ZHIJUN_DATA_AGENT_CREDENTIAL_FILE` 指向绝对路径；Renderer、日志和对话记录不得取得 Secret 或确认 Token。生产环境的 `CENTAUR_SECRET_STORE_DIR` 位于 Gateway 独立 secrets root 的 `rag-v2/<workspaceId>` 下，必须与工作区业务数据根互不包含。
- 凭据文件必须由 worker 运行账户持有且权限为 `0600`。Data Engine Gateway 在工作区 worker 激活边界按需创建一次长期 App；只有 active、未过期、owner/device/workspace 绑定和 Secret 全部精确匹配，且能力完整时才复用。已停用、已撤销或缺少能力的 App 一律失败关闭，不会自动恢复权限。
- 对话检索最小 App 能力为 `mindos.read`、`mindos.search`、`mindos.sensitive.confirm`；规则管理按需加 `mindos.sensitive.policy.write`，原文按部署策略加 `mindos.sensitive.original.read`。资料库导入管理使用现有 Gateway 的 `materials` 操作通道，不借此扩展 RAG App 权限，也不调用其历史 import/upload.status。当前 DE provisioning 仍含历史能力集合，不能据此宣称既有 App 已自动缩权。实际授权由 DE 团队受控配置；ownership epoch 变化不能扫描或批量停用其他应用。
- Data Agent 地址优先使用 `ZHIJUN_DATA_AGENT_BASE_URL`；未设置时取 `ZHIJUN_CAPABILITY_URL` 的同源地址，最后才使用回环默认地址。非回环 HTTP 会被拒绝。
- “偏好 → 敏感规则”通过知君可信后端管理 Data Agent 规则：可查看内置和自定义规则，创建、编辑、启停及删除自定义规则。内置规则只读；容量和检测提示词预算始终读取服务端返回值，不在前端写死。
- 更新/删除使用服务端 revision 对应的 `ETag`/`If-Match` 做并发控制，Renderer 只接触 revision。`CUSTOM_RULE_SIMILAR` 先展示相似规则，用户确认用途不同后以新幂等键和 `acknowledgeSimilarRuleId` 重提。
- 交付方式变更立即参与决策；识别语义变化可能进入 applying，后台重扫期间旧 active 规则继续服务。知君低频展示状态，不将规则状态查询作为 Search 前置条件；失效的旧确认 Token 或证据需要重新检索。

系统话头的一次性本地限制必须在检索、预览和实际提交中保持一致；用户显式改选在线后遵循权威路由。确认取消不重复创建消息，结果未知的提交不得自动补发。

## 回复辅助

回复辅助只生成可供用户选择和编辑的表达，不自动发送。候选必须绑定当前会话、最新助手消息、上下文和来源；重新生成、选择、编辑、撤销和最终发送都保留来源关系。

候选本身不能自动确认个人理解、改变章程或授予资料权限。来源失效时保留用户草稿并要求重新生成，不能把带来源的内容静默改成无来源文本。

## 上下文与回执

每轮上下文可以包含经检查的近期消息、个人理解、章程、事项、成果和用户选中的检索片段。显示层清理引用标记不改变原消息快照或来源归属；上下文预算仍可能减少最终采用的片段，勾选数量不等于实际输入数量。

助手消息保存实际 provider、model、external、引用来源、历史附件出处和回复目标。判断某轮走本地还是在线必须看这些回执，不能只看会话顶部的选择状态。RAG 使用 JSON REST，最终回答继续使用既有流式协议；检索/人工确认等待与模型生成耗时应分开定位。

## 代码与测试入口

- 页面：`frontend/mindos-web/src/pages/ConversationPage.vue`
- 会话流：`frontend/mindos-web/src/services/chatStream.ts`
- 材料确认：`frontend/mindos-web/src/components/conversation/RagSensitiveDialog.vue`、`src/services/taskRouting.ts`（后者相对 mindos-web）
- 检索专用兼容边界：`frontend/mindos-web/src/composables/useChatImports.ts`、`backend/mindos/chat_imports.py`
- 回复恢复：`frontend/mindos-web/src/composables/useReplyRecovery.ts`
- 敏感规则设置：`frontend/mindos-web/src/components/settings/SensitiveRulesPanel.vue`
- 后端：`backend/mindos/conversations.py`、`backend/mindos/zhijun/turn.py`
- Data Agent V2 编排与规则 facade：`backend/mindos/data_agent_rag.py`、`backend/mindos/sensitive_rule_routes.py`
- 前端测试：`conversation-management.test.mjs`、`chat-stream.test.mjs`、`reply-recovery.test.mjs`、`memory-pending-routing.test.mjs`

接口字段详见 [知君接口契约](zhijun-api-contract.md)，模型和资料授权详见 [模型路由](task-routing.md)。

## 沉浸式壳（记忆系统 V3 之后，2026-09-19）

设计见 [沉浸式界面](../product/ZHIJUN_IMMERSIVE_UI.md)。当前通过 `?shell=immersive|classic`（写入 `localStorage['zhijun.shell']`）或偏好里的开关切换；壳的选择在 `src/App.vue`，路由表不变。

- `src/immersive/ImmersiveShell.vue`：舞台（在场行 + 印坞 + 日流 + 输入区宿主）与四个抽屉（我 / 昔 / 案头 / 偏好）；非流路由在 `RoutePageDrawer` 中打开，`/settings` 在偏好抽屉中打开。
- `src/immersive/composables/useDayStream.ts` + `dayStream.ts`：按天分节的长流（会话列表分页、按创建日分组、上滑加载更早、今日来信、当前会话规则、回到某天）。
- `src/pages/ConversationPage.vue` 嵌入模式：注入 `immersiveKey` 后成为流里唯一可发送的当前块，发送 / 流式 / 授权 / 记忆 / 产出 / 草稿逻辑原样；工具与输入区经 `Teleport` 进入案头、偏好与输入区宿主；`activeTurn.ts` 桥接把状态暴露给壳。
- `src/immersive/StreamTurn.vue` + `composables/usePacedReply.ts`：单条消息的知印、状态行（`statusLine.ts`）、记印 / 留印、就地展开的出处与产出；回复按段落浮现（偏好可关，减少动效偏好下直通）。
- 测试：`tests/immersive-*.test.mjs`、`tests/paced-reply.test.mjs`、`tests/status-line.test.mjs`；端到端 `tests/immersive-turn.e2e.mjs`（夹具 `backend/tests/immersive_fixture.py`，8778 端口）。
