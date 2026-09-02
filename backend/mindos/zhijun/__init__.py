"""知君对话与本体的 agent 层（P1：能聊、能记、能认）。

模块分工：
- provider    模型通道抽象（fake / ollama / openai-compatible / anthropic），流式与结构化输出
- gate        会话级互斥 + 通道并发门
- persona     系统提示：不可关闭的人格原则、来源标签契约、建档脚本
- context     每轮上下文组装（章程 / 已确认理解 / 工作理解 / 被纠正块 / 资料片段 / 近期轮次）+ 隐私过滤
- extract     从用户原话抽取候选理解（校验、去重、墓碑抑制、复述即确认）
- jobs        单线程本体 worker（抽取 / 摘要 / 投影）
- projection  把已确认本体投影成人类可读的 Markdown（ZHIJUN_PROFILE.md / USER.md）
- turn        一轮对话的完整流程，产出 SSE 事件
- confirm     对话内一键确认 → 状态机 + 系统备注消息

``mindos/agent/`` 是给第三方 Agent 的只读网关，与本包无关。
"""
