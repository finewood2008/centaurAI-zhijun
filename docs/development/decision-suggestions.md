# 判断草稿：可选的 AI 候选方向

“只有你能填的”改为“由你来决定”。用户可请求 2–3 个不同方向，每个方向同时包含选择、理由与取舍、预期观察，避免逐字段拼出互相矛盾的答案。

生成、选用、确认保存是三个独立动作：

- 生成不更新数据库，不新增消息、不形成用户事实，也不会选中默认答案。
- 选用只填入当前页面的编辑框；已有内容时明确询问“只补空白 / 替换三项 / 取消”。用户仍可改写或都不用。
- 把握由用户通过滑杆或快捷选项选择，不让模型评分；最终点击“记进判断簿”才走原有确认流程。
- 已选用的 AI 文字标注来源，`assistedFields` 保留在确认后的草稿和确认消息元数据里，不标成“你的原话”。
- 候选和未提交编辑不持久化；页面有明确刷新提示。确认后的结果刷新仍可查看。

接口：`POST /api/mindos/conversations/{id}/decision-draft/suggestions`，参数为 `draftId`、`expectedRevision`、`current`（三项正在编辑的文字）、`avoidChoices`（上一组选择，最多三项）。

复用现有本机 Ollama 配置和通道并发门；只取当前草稿、最近三条用户消息和正在编辑的文字，限制上下文长度。请求明确限定资料不是指令、候选不是用户的既有想法、不生成把握、不虚构证据或保证结果。仅本地生成，不读取额外文件、不请求外部模型，也不在故障时静默切换服务。

服务端校验设备作用域及草稿版本；生成期间草稿/会话变化则返回冲突。前端请求过期、换草稿、用户继续修改和网络/模型错误均不覆盖输入。后端错误释放模型通道；页面可以继续手动填写。

从受保护文件/深层画像会话确认的判断保留 `local_only_decision` 标记，不进入普通历史判断上下文；直接回访走本地并禁止普通后台派生抽取。保存用户选用的文字不等于授权外发其资料来源。

验证：

- 后端：`python -m pytest tests/test_decision_suggestions.py tests/test_zhijun_deliberate.py -q`。
- 前端：`node --experimental-strip-types tests/decision-draft.test.mjs`、`npm run build`。
- 浏览器：在 backend 运行 `python -m tests.decision_suggestions_fixture`（仅本机 8770，独立临时数据库），在前端运行 `node tests/decision-suggestions.e2e.mjs`。不修改真实判断记录。
- fixture 可设 `ZHIJUN_TEST_REAL_LOCAL=1` 验证本机 qwen3.5:9b；仍只使用合成数据。
