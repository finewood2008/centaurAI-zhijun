# 在线理解 403 修复与真机验收（2026-09-08）

## 现象与判定

公司盒 `AMD-A2A-248` 已连接，桌面显示“在线理解”，发送消息仍提示 `请求失败（403）`。逐层检查确认桌面、Connectivity SDK、Gateway 会话和 routing preview 均可用；失败发生在在线模型授权签发和后续模型流调用，不能把通用 403 直接解释为账号登录失效或模型密钥错误。

首次授权签发返回 `outbound_governance_disabled`，说明盒端未启用外发治理。启用 `MINDOS_STAGE6_ENABLED=true` 后，稳定错误码推进为 `remote_model_target_not_allowlisted`；只将实际供应商域名 `token.qeeshu.com` 加入 `MINDOS_EXTERNAL_LLM_ALLOWED_HOSTS` 后，routing grant 恢复 HTTP 200。HTTPS、DNS/IP、同源重定向和逐次内容授权检查均保留，没有使用通配符。

授权恢复后，模型任务选择 `openai / deepseek-v4-flash`，但在首个 token 前以 `MODEL_STREAM_INCOMPLETE` 结束。盒内不输出密钥和正文的探测证明：当前根地址 `https://token.qeeshu.com` 返回 HTTP 200 HTML；`https://token.qeeshu.com/v1/chat/completions` 使用同一配置返回 `text/event-stream`、10 个 data 事件和 `[DONE]`。因此根因是供应商地址缺少 `/v1`，不是模型不可用。

## 配置修复

修复前，生效的 chat provider revision 为 1、供应商 revision 为 3，且供应商草稿在激活后又被编辑，形成 `pendingActivation`：生效配置仍指向 `token.qeeshu.com`，草稿指向另一个未激活地址。修复先使用 SQLite 在线备份保存完整运行时数据库，再通过现有 `WorkspaceRuntimeProvider.save_profile` 与 `WorkspaceModelsStore.activate_profile` 更新并激活：

- provider：`openai`
- model：`deepseek-v4-flash`
- base URL：`https://token.qeeshu.com/v1`
- profile revision：5
- chat provider revision：2
- `pendingActivation`：false

修复复用盒内加密保存的现有供应商密钥，终端、文档和验收收据均未输出密钥。数据库备份名为 `runtime.db.backup-online-20260908-130022`；盒端收据 `/srv/zhijun-integration-0907/online-model-acceptance-20260908.json` 权限为 0600。

## 真机验收

桌面在同一已连接会话中点击“重试当前模式”，重新展示逐次在线发送授权。授权后，应用收到真实 DeepSeek 流式回复并完整结束；结果卡显示 `DeepSeek（外部）`，来源数为 0，没有自动切换到本地模型。盒端 `/internal/zhijun/model-stream` 返回 HTTP 200。

本轮同时补充桌面稳定错误码映射与行为测试。盒端返回 `outbound_governance_disabled`、`remote_model_target_not_allowlisted` 或模型流不完整时，页面显示对应的可操作中文提示；显式服务端 detail 仍优先，不被本地映射覆盖。Data Engine 也拒绝 HTTP 200 HTML 等明显不是模型事件流的外部响应，使地址配置错误不再退化成模糊的流不完整。

## 回退

盒端保留容器级回退版本：`zhijun-integration-0907-pre-stage6-20260908-203655` 与 `zhijun-integration-0907-pre-allowlist-20260908-2045`。配置数据库可使用上述 SQLite 备份恢复；恢复前必须停止当前 8620 实例并保留现有数据库副本，不能回滚用户在修复后产生的业务数据。运行时配置回退后需要重新连接桌面并复验 routing grant、模型流和三端口健康状态。
