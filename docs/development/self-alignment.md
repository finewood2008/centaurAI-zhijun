# 自我贴合度与持续校准

自我贴合度衡量用户对记录代表性的认同，不是事实真假、抽取置信度或潜意识真实性。

## 数据与状态

`claims.self_alignment_json` 是增量可空列。历史记录不回填等级。
`Claim.selfAlignment` 返回 level（null 或 0–4）、framing、reason、evidenceIds、
proposal、revision、claimVersion、evidenceVersion、calibratedAt、needsRecalibration 和最近 20 条 history。
完整修改记录保留于已有 `review_events`。

五档从低到高：不代表我、较少代表、部分代表、比较代表、很能代表。
framing 为 long_term / context_only / aspirational。用户选择仅当时或理想方向时，
原 claim 的适用范围/层同步限定；不撤销记录事实。情境记录不导出为长期 USER.md 画像。

模型只有 propose 权限。正式等级仅由明确校准接口写入。确认原记录、重申、时间流逝不会改分。
内容/层/范围/生命周期变化使旧校准失效；新增证据保留等级但让旧提议、外发授权失效。
同一 requestId 幂等；版本、证据指纹或旧提议不匹配返回 409，不覆盖用户的新修改。

## 接口（均在 /api/mindos/ontology 下）

- `POST /claims/{id}/alignment`：requestId、expectedRevision、claimVersion、evidenceVersion，
  action=calibrate/defer/clear，以及 level、framing、note，可带 proposalId、conversationId。
- `POST /claims/{id}/alignment/proposals`：conversationId、messageId，可带 feedback；异步本地提议。
- `GET /alignment/conversations/{id}`：本会话提议、授权所需的具体画像/历史快照、服务及处理状态。
- `POST /alignment/conversations/{id}/consent`：serviceId、refs（claimId、fingerprint）或 localOnly=true。
- `POST /claims/{id}/alignment/revoke`：撤销该画像所有服务的授权。

原 Claim/消息接口保持兼容。浏览器校准卡先预览，再通过独立确认按钮提交；普通聊天里的“对”不触发校准。
自然语言修正经过本地整理，仍是待确认提议。五档选择也支持不依赖模型的直接手动校准。

## 异步提议

复用 ontology worker 的 alignment job。抽取完成后仅挑一条相关的已确认理解；
已有深层画像的对话也可基于本轮真实用户原话形成新证据与提议，但不自动改变等级。
证据必须可追溯到用户消息、用户手写、复盘或当前可读的文件版本；助手输出不算新证据。
同一来源去重；单次行为不能被自动推为稳定内心。跳过后无新证据不重复提问。
模型提议固定使用本地模型，证据不足可弃权，模型故障显示暂停，手动校准仍可用。
服务重启把残留 queued 状态标为 paused；持久化任务可按现有租约机制恢复。

## 图谱和回答

已校准的长期点半径为 `140 - 15 * level`；未校准在 175–210，情境在 250–280；
待确认仍在朱砂边界外。时间只影响 ◷ 久未核对标记。理想方向保留独立视觉样式。

回答中先看相关性，贴合度仅参与相关候选的排序并提供明确的解释限定。
低贴合事实仍保留；当前用户要求优先。取消了“无关原则也必定注入”的旧锚点规则。
实际使用的 alignmentSources 随消息 meta/provenance 持久化，包含原始修订和服务回执关联。

## 隐私

授权绑定服务身份、具体画像快照、当前画像/证据指纹。改版本、换服务、撤销后重新核对。
历史回复可能含衍生内容：历史快照也需逐项授权，授权新版不会自动授权历史版。
撤回/替代或资料不可读时不能继续外发。原文件版本及正文快照仍需独立文件授权。

未授权的当前画像或历史会让这一轮走本地；发送前再核对一次，避免组装期间发生修改。
有深层画像来源的会话不走普通后台抽取、摘要或判断草稿链，防止衍生内容丢失来源后回流。
持续贴合度提议仍在专用本地任务中运行。模型不可用不静默外发。
通用上下文包和 USER.md 不增加 selfAlignment、模型理由、校准说明或证据原文。

## 验证

- 后端：`.venv/bin/python -m pytest tests/test_self_alignment.py -q`（在 backend 下运行）。
- 前端：`npm run test:p14-frontend`、`npm run build`。
- 浏览器：backend 下运行 `.venv/bin/python -m tests.alignment_fixture`；前端下运行
  `node tests/self-alignment.e2e.mjs`。fixture 仅监听 127.0.0.1:8769，数据库位于独立临时目录，
  请求绝不访问外部服务。截图在 data/diagnostics/self-alignment（gitignored）。
- 真本地模型：backend 下运行 `.venv/bin/python -m tests.alignment_local_smoke`。
  仅访问 127.0.0.1:11434 的 qwen3.5:9b，使用隔离合成数据，不操作真实记录。

上线前备份：`data/db/ontology.pre-self-alignment-20260904-implementation.db`。
没有为真实记录批量打分，也没有修改模型账号配置。
