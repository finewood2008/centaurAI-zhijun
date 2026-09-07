# 对话整理：重命名、归档、搜索与置顶

> 2026-09-05 已按上游 `22dc9a3112058f06a1e4a385c1b2dc3175e39476` 复核；本轮变化见文末同步补充，原测试与部署内容保留为历史记录。

## 用户行为与边界

- 列表每项及当前标题的 `⋯` 都提供管理入口。改名去除首尾空白，接受 1～80 个 Unicode 字符；Enter 保存、Esc 取消，冲突时保留用户输入供核对。手动标题不会被后续消息覆盖。
- 最近／已归档切换只影响列表。归档当前对话不离开正文、不清空输入；归档和恢复后均提供撤销。查看、刷新、后台回复完成、旧请求重试不自动恢复。
- 一条新的完整用户消息与恢复归档在同一事务写入；新文件批次创建的用户消息同样适用。未授权、校验失败、重复批次或插入失败不恢复。归档不取消正在生成的任务。
- 最近对话按置顶时间倒序在前，其余按最后消息时间倒序；时间相同用 ID 稳定排序。归档保留置顶时间，恢复后重新进入置顶区域。
- 搜索默认查全部对话，可切换全部／最近／已归档。查询本地完整数据库中的标题、用户与助手正文，不调用模型；不匹配系统消息。结果展示纯文本片段，正文命中携带消息位置并高亮。清除搜索恢复此前的列表分组。
- 首次认识也可改名、置顶或归档，不重置初始化；原有引导期间删除限制保留。回访查找包括已归档会话，按设备复用，不因归档重复创建。
- 归档不是删除或遗忘，不改变本体、资料、来源限制、在线授权和既有跟进任务。不增加临时对话、导出、标签或批量管理。

## 存储与 API

沿用 `conversations.status`，增量添加 `pinned_at`（默认 NULL）与 `metadata_revision`（默认 0）。旧数据不改名、不置顶、不归档。

`PATCH /api/mindos/conversations/{id}`：

```json
{"expectedRevision": 0, "title": "产品讨论", "status": "archived", "pinned": true}
```

除修订号外至少提供一项；显式 null 不接受。返回更新的会话，含 `pinnedAt` 和 `metadataRevision`。名称、归档、置顶操作不修改 `updatedAt` 或 `lastMessageAt`，不触发首页来信重新整理。

事务内检查设备与管理修订；重复提交相同目标状态不增加修订，过期的不同修改返回 409。新的用户消息自动恢复也增加管理修订，避免旧的管理操作覆盖恢复结果。沿用现有写入防护。

`GET /api/mindos/conversations` 接受：

- `status=active|archived|all`，默认 active。
- `q`，去除首尾空白，最多 100 字。`%`、`_`、反斜线按普通搜索字符处理，SQL 使用绑定参数。
- `limit` 默认 50，范围 1～200；`offset` 默认 0，范围 0～SQLite 64 位整数上限。前端每页 30 条。

返回 `{items,total,hasMore}`；total 为所有符合条件记录的真实数量。命中项额外含 `searchMatch:{field,messageId,snippet}`，片段最多 140 字。标题匹配优先，其余按最近活动排序；搜索中不以置顶干扰匹配顺序。

内部时间线读取仍使用原 `list_conversations` 的活动时间排序，不跟随置顶。首页将最近 50 条活跃或归档会话纳入原流程，若较老的首次认识未在这 50 条内则单独补取，避免归档导致重新初始化。

## 交互并发与安全

列表查询防抖并取消旧请求，响应另带客户端时序检查；分页支持加载更多。管理后的刷新只更新会话元数据，不替换正文、生成流或输入内容。所有搜索文本用普通文本渲染，不解释 HTML。

管理菜单使用已有原生 popover，以视口为边界避免窄屏滚动容器裁切；其他既有菜单保持原边界。重命名用轻量原生 dialog。归档撤销带对应修订，不覆盖后续修改；窄屏收起列表时，当前标题下仍可撤销。改名和置顶直接更新标题及标记，不叠加成功浮层遮挡输入。标题、归档或置顶变化不扩大任何模型权限，搜索不将内容发送给模型。

## 备份与验证

迁移前 SQLite 一致性备份（均通过完整性检查）：

- `data/db/conversations.pre-conversation-management-20260904-205229.db`
- `data/db/ontology.pre-conversation-management-20260904-205229.db`

验证入口：

- 后端 `tests.test_conversation_management_store`、`tests.test_conversation_management_api`、`tests.test_conversation_management_compat`。
- 前端 `tests/conversation-management.test.mjs`、`tests/conversation-management.e2e.mjs`。
- 兼容回归覆盖既有会话流、文件导入、首页、初始化、记忆整理、统一路由与来源授权、章程、校准和回复辅助。

浏览器验证使用隔离的 8772 合成数据服务；不会向用户真实会话插入测试消息或调用外部模型。

本次后端组合回归 404 项通过，前端 26 个测试文件通过，并完成桌面 1440×1000 与窄屏 390×844 的隔离浏览器验证。真实数据库迁移后通过 quick_check；28 条既有会话的名称与状态和备份一致，置顶时间为空、管理修订为 0。


## 2026-09-05 同步补充：事项绑定与发送恢复

核对源码：上游 `22dc9a3112058f06a1e4a385c1b2dc3175e39476`。以上归档、置顶、搜索存储语义及测试/备份数值保留为原实施记录；本次同步未重新执行那些验证，也未操作这些历史备份。

- 本轮没有修改 conversation-management 后端的列表/管理路由。列表中的搜索命中片段现在经 `stripLabels` 清理显示标记；搜索仍查询原正文，未重写数据库。显示清理不产生新的事实确认或授权。
- 新增独立事项和成果工作区，详情见 [接口契约第 19 节](zhijun-api-contract.md#19-事项与成果合同--版本-work-2026-09-05)。一段对话可用独立 `bindingRevision` 关联/切换/解除一个当前事项，一个事项可关联多段对话；事项不等于对话，也不以对话 metadataRevision 管理。归档对话不等于暂停事项，完成事项也不等于删除对话。
- 用户可以先创建事项、以后再明确点击继续讨论；`continueMatter` 优先使用仍存在的绑定会话，否则创建会话后绑定。仅打开事项不发送模型请求。准备沟通等起手文字追加到现有输入，仍由用户发送。
- 发送前的路由查询、预览及授权等待支持取消。失败或取消时按提交所属会话恢复输入；用户后来已写的内容优先保留，能安全合并才合并，否则另存未发送草稿供切换。恢复保留辅助来源和可撤销片段，跨会话的迟到回包不应写入当前正文。
- 草稿键目前仍按会话区分：`zhijun.reply-input.<id>` 与新增 `zhijun.reply-failed.<id>`，尚未具备账号/设备维度。此更新改善对话恢复，不代表桌面多账号/设备隔离已经完成；集成时需按 [集成方案](INTEGRATION-0905.md) 收敛状态与清理规则。

核对入口：[ConversationList.vue](../../frontend/mindos-web/src/components/conversation/ConversationList.vue)、[Composer.vue](../../frontend/mindos-web/src/components/conversation/Composer.vue)、[ConversationPage.vue](../../frontend/mindos-web/src/pages/ConversationPage.vue)、[matters.ts](../../frontend/mindos-web/src/services/matters.ts)。验证入口新增 `chat-send-recovery.e2e.mjs`、`chat-stream.test.mjs`、`matters.test.mjs`；这里只列入口，不声明本轮运行结果。
