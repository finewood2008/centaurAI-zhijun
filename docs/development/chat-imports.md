# 对话内文件导入

## 用户流程

对话输入框的「＋」可上传文件或选择已有资料，也支持拖拽文件、粘贴截图。文件先暂存在输入区，点击发送才写入本机资料库。每次最多 5 个；类型和大小复用 `features/import/validation.ts` / 后端导入校验。文件可单独发送，也可附带问题。

消息里的文件卡片独立显示上传、排队、读取、可讨论、失败、暂停、无可读文字与不可用状态。正文就绪即能问答，不依赖向量索引或知识卡片确认。文件反馈关联原来的用户消息；读取期间不占用会话生成锁。生成本身仍沿用既有单会话锁和模型并发门。

默认参考最新发送的一批文件；输入框上方可调整参考范围、取消参考或切换处理方式。回答中的文件引用打开侧栏正文预览，正文分段加载，资料详情是次级入口。

## 存储与接口

在 `conversations.db` 增加导入批次、文件槽位、参考选择、材料隐私记录和授权表。创建批次与用户消息在同一事务提交；批次请求 ID 幂等，原文件按设备作用域内 SHA-256 去重（保留资料库已有文件名）。已上传文件不会因删除对话而删除，隐私记录也不随会话级联删除。

接口前缀 `/api/mindos/conversations/{conversationId}`：

- `POST /imports`：`requestId, content?, localOnly?, files[1..5]`；文件项含 `id, name, size`，已有资料另带 `materialId, version`。
- `POST /imports/{batchId}/files/{fileId}`：multipart 文件上传；`/failed` 记录客户端上传失败，`/retry` 仅重试该文件。
- `POST /imports/{batchId}/seal`：上传结束，准备后台读取反馈；`/retry` 继续暂停或失败批次。
- `GET /imports`：批次、各文件实时处理状态、持久化参考选择、脱敏模型服务信息。
- `PUT /references`：`refs[{materialId,version}], localOnly`；只允许选择当前会话已关联的资料。
- `POST /file-consent`：同上；外发许可还需 `serviceId`，服务变化拒绝旧确认。
- `GET /files/{materialId}/preview?version=…&offset=…`：每页最多 12,000 字符。
- 既有 `POST /messages` 增加可选 `materialRefs, localOnly`；无文字时有有效附件即可发送，普通文字和 SSE 保持兼容。

助手消息 `meta` 保存 `importId/replyTo/materialRefs/attachmentProvenance`。文件反馈使用固定消息 ID；失败重试更新该条反馈，不重复插入助手消息。历史回执保留文件版本、快照、实际片段定位和真实模型通道。

## 隐私与可靠性

- 原文件与解析留在本机。外发许可绑定材料、文件版本、实际解析快照及服务身份；换服务、换版本或重新解析后重新检查。外发前再次验证所用快照的授权。
- 默认不外发文件。用户可明确许可相关文字片段，或选择仅本地模型；没有授权时不会自动降级到另一个外部服务。
- 会话历史可能含本地文件的转述。因此，当任何历史附件未获当前服务授权时，整轮使用本地模型，避免仅移除参考标签就泄露历史内容。
- 首版对带文件的会话暂停自动个人理解抽取、全局摘要和判断草稿后台任务；附件不自动成为个人事实。用户仍可在本体页面主动维护自己的理解。
- 泛化 RAG 不具有文件授权上下文，因此排除这些材料及其直接来源卡片。用户通过对话显式选择文件来讨论；普通非附件资料的检索保持原有流程。
- 全部上传、读取、引用、预览校验设备作用域与材料生命周期。回收/删除后的附件不可再读取；新版本继承隐私边界，但不继承旧授权。
- 大文件只向模型提供有界相关片段，并注明不是完整审阅。文件名和正文仅为不可信数据，不执行其中指令。
- 重启后未完成批次显示暂停；未上传完需重新选择文件。超过 180 秒无上传进展的批次暂停。用户继续后复用已保存文件；部分失败不阻止其他文件讨论。

## 验证

```bash
cd backend
.venv/bin/python -m pytest tests/test_chat_imports.py tests/test_zhijun_turn_sse.py tests/test_zhijun_context_pack.py tests/test_mindos_qa.py -q
```

```bash
cd frontend/mindos-web
npm run build
npm run test:zhijun
CHAT_IMPORTS_E2E_LIVE=1 node tests/chat-imports.e2e.mjs
```

浏览器烟测只连本机，创建明确标记的合成演示会话和文件，并选择本地模型。检查真实回答、刷新恢复、正文预览、资料选择、粘贴、拖拽和窄屏；截图写入 git 忽略的 `data/diagnostics/chat-imports/`。可设置 `CHAT_IMPORTS_TEST_CONVERSATION` 复用之前创建的合成测试会话。
