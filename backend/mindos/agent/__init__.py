"""外部 Agent 集成开放平台 Gateway（AG-01 起）。

- auth.py      # token 解析、scope、Agent Principal
- policy.py    # 资源访问、写入审批、下载权限策略（AG-02 起逐步启用）
- audit.py     # 审计记录、traceId、脱敏
- schemas.py   # Agent API Pydantic 请求/响应模型
- service.py   # 调用既有 MindOS 服务的适配层，禁止重复业务逻辑
- router.py    # /v1/agent REST 路由
- mcp_server.py# MCP tools 到 service.py 的薄适配（AG-04）
- rate_limit.py# 按 clientId 的限流与并发控制
"""
