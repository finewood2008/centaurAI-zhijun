# 对话、附件与回复恢复

## 会话

对话支持创建、加载、搜索、标题修改、置顶、归档和删除。列表元数据与消息流分开更新；迟到的读取或写入结果必须核对当前会话和请求代次，不能污染用户后来打开的会话。

输入草稿按会话保存。发送前会创建或确认目标会话、准备模型路由和来源授权；失败或取消时只恢复原提交所属会话的输入。用户在等待期间继续输入的文字优先保留，只有内容和来源都能安全合并时才自动合并。

## 附件

输入框可以上传新文件或选择已有资料。文件先进入暂存区，用户发送后才创建导入批次；批次创建和用户消息使用稳定请求标识，避免重试重复写入。

- 每轮文件数量、类型和大小由前后端共同校验。
- 原文件、解析和索引留在设备内；在线模型只使用用户明确允许的必要文字片段。
- 授权绑定资料、版本、解析快照、用途和服务；服务或版本变化后重新核对。
- 删除会话不会自动删除已经进入资料库的原文件。
- 文件反馈、预览、失败、暂停和重试都有独立状态，读取附件不占用聊天生成锁。

盒端工作区中的附件接入 Data Agent RAG V2，边界如下：

- 知君的 RAG 数据面只调用 `/v1/agent/apps/*`；`/api/mindos/rag-v2/*` 是浏览器适配层。历史 `/api/mindos/zhijun/*` 工作区能力仅可用于不读取正文的资料元数据链接，不能作为上传、检索、确认或证据回退。
- 上传通过 `POST /material-jobs` 登记，保存 `jobId`，只有 `state=ready`、`stage=completed`、`indexState=indexed` 同时成立时才进入对话检索。
- 每轮按当前问题调用 `/search`，只把 Data Agent 已允许交付的 `items[].text` 放进模型上下文；最终使用前通过 `/evidence:resolve` 重新核对证据引用。
- `sensitive_confirmation_required` 必须由用户选择脱敏交付、在具备能力时领取原文或取消；存在 `detectionNotice` 时，未完成检测的片段保持暂扣，风险放行还需要单独的明确确认。
- App ID/Secret、普通确认 Token 和风险 Token 仅保存在盒端可信进程。默认凭据文件为 `$CENTAUR_SECRET_STORE_DIR/data-agent-rag-v2.json`，也可由 `ZHIJUN_DATA_AGENT_CREDENTIAL_FILE` 指向绝对路径；Renderer、日志和对话记录都不得接触这些值。生产环境的 `CENTAUR_SECRET_STORE_DIR` 位于 Gateway 独立 secrets root 的 `rag-v2/<workspaceId>` 下，必须与工作区业务数据根互不包含。
- 凭据文件必须由 worker 运行账户持有且权限为 `0600`。Data Engine Gateway 在工作区 worker 激活边界按需创建一次长期 App；只有 active、未过期、owner/device/workspace 绑定和 Secret 全部精确匹配，且能力完整时才复用。已停用、已撤销或缺少能力的 App 一律失败关闭，不会自动恢复权限。
- 长期 App 至少授予 `mindos.import`、`mindos.upload.status`、`mindos.read`、`mindos.search`、`mindos.sensitive.confirm` 和 `mindos.sensitive.policy.write`；只有允许领取原文时才追加 `mindos.sensitive.original.read`。ownership epoch 变化时只按旧工作区私有凭据精确停用对应 App，不能扫描或批量停用其他应用。
- Data Agent 地址优先使用 `ZHIJUN_DATA_AGENT_BASE_URL`；未设置时取 `ZHIJUN_CAPABILITY_URL` 的同源地址，最后才使用回环默认地址。非回环 HTTP 会被拒绝。
- “偏好 → 敏感规则”通过知君可信后端管理 Data Agent 规则：可查看内置和自定义规则，创建、编辑、启停及删除自定义规则。内置规则只读；容量和检测提示词预算始终读取服务端返回值，不在前端写死。
- 更新和删除使用服务端 revision 对应的 `ETag`/`If-Match` 做乐观并发控制，但 Renderer 只接触 revision，不接触原始 ETag。`CUSTOM_RULE_SIMILAR` 必须先展示相似规则，用户明确确认用途不同后，以新的幂等键和 `acknowledgeSimilarRuleId` 重提。规则变更立即生效，并使旧确认 Token 和证据引用失效；随后请求必须重新检索。

系统话头的一次性本地限制也必须传入附件路径。批次已经由服务端接受后，客户端不能把它恢复成新的未发送批次，否则可能产生重复消息或改变原路由语义。

## 回复辅助

回复辅助只生成可供用户选择和编辑的表达，不自动发送。候选必须绑定当前会话、最新助手消息、上下文和来源；重新生成、选择、编辑、撤销和最终发送都保留来源关系。

候选本身不能自动确认个人理解、改变章程或授予资料权限。来源失效时保留用户草稿并要求重新生成，不能把带来源的内容静默改成无来源文本。

## 上下文与回执

每轮上下文可以包含经检查的近期消息、个人理解、章程、事项、成果和用户选择的资料。显示层清理引用标记不改变原消息快照或来源归属。

助手消息保存实际 provider、model、external、引用来源、附件出处和回复目标。判断某轮走本地还是在线必须看这些回执，不能只看会话顶部的选择状态。

## 代码与测试入口

- 页面：`frontend/mindos-web/src/pages/ConversationPage.vue`
- 会话流：`frontend/mindos-web/src/services/chatStream.ts`
- 附件：`frontend/mindos-web/src/composables/useChatImports.ts`
- 回复恢复：`frontend/mindos-web/src/composables/useReplyRecovery.ts`
- 敏感规则设置：`frontend/mindos-web/src/components/settings/SensitiveRulesPanel.vue`
- 后端：`backend/mindos/conversations.py`、`backend/mindos/zhijun/turn.py`
- Data Agent V2 编排与规则 facade：`backend/mindos/data_agent_rag.py`、`backend/mindos/sensitive_rule_routes.py`
- 前端测试：`conversation-management.test.mjs`、`chat-stream.test.mjs`、`reply-recovery.test.mjs`、`memory-pending-routing.test.mjs`

接口字段详见 [知君接口契约](zhijun-api-contract.md)，模型和资料授权详见 [模型路由](task-routing.md)。
