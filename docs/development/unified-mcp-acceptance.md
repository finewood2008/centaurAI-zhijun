# 知君统一 MCP V1 验收记录

日期：2026-09-17。范围：当前知君工作树的本地实现、隔离数据、参考 MCP 协议客户端与临时 TLS 网络。**未发布到真实盒子或云端；不代表 WorkBuddy 已正式接通。**

## 已验证

| 项目 | 结果与证据 |
|---|---|
| 新 MCP + 旧工作区/RAG/Context Pack 回归 | 78 个后端测试通过；命令见部署文档 |
| 前端完整回归 | 255 个测试通过，包含原对话、设置与新增外部 Agent / 桌面桥接断言 |
| 桌面壳回归 | 359 个测试通过；2 个 Windows 专属测试在 macOS 上跳过 |
| 协议版本、OAuth metadata、认证挑战 | MCP SDK **1.29.0**，2025-11-25 initialize 协商通过；无 bearer 返回 401/资源元数据；错误 Origin 拒绝 |
| 同一 MCP HTTP 会话读取两类数据 | 临时真实本体 SQLite + RAG V2 合同 double；个人与工作数据均返回来源和回执；随后撤权，旧引用返回错误且无正文 |
| 多调用方隔离 | Alice/Bob 20 次并发 HTTP 调用各自保留身份；跨账号、盒子、工作区、ownership epoch、Agent、audience 均拒绝 |
| 权限与迁移 | 默认关闭；旧 token 无迁移；类别未来合格条目自动生效；旧条目未确认不提供；敏感/本地/过期/明确禁止/派生限制持续生效；旧禁止项独立持久化 |
| 工作检索范围 | 空集合不调用 Search；101 个资料分成 100+1 个范围下推；有界合并；新资料不自动开放；无结果/被策略拦截返回空，不泄露下游数量 |
| 敏感确认 | masked/original/cancel；Agent 无批准工具；超时、资料版本变化、撤销、跨 Agent 请求拒绝；脱敏请求遇到原文交付标记直接失败 |
| 证据失效 | Evidence Resolve 重查策略版本、检测器版本、有效期、材料版本、敏感状态和安全定位字段；旧引用不绕过更新 |
| 审计 | 调用回执、资源 ID/版本和交付方式保留；审计不含查询/结果正文/下游令牌；写入失败停止交付 |
| 盒端授权页 | 默认不选类别；个人/资料预览仅在盒端页面；CSRF/Origin/跨账号票据/一次性提交检查；取消不创建 grant；账号服务只收到 opaque grant 元数据 |
| 密文转发 | 临时真实 TLS 1.3 + mTLS TCP 测试通过；Agent 验证到盒端证书；转发字节中未出现测试查询或结果明文；未连接盒子时连接关闭 |
| 设置管理界面 | 实际 Vue 组件在 1440px / 390px 验证调整、历史条目确认、取消、暂停、撤销、无横向溢出；测试 API 均为模拟 |
| 桌面管理桥接 | 新管理 API 穿过 renderer catalog 和真实 shell policy；账号参数不能经 query 伪造；内部 MCP dispatch 路径不对 renderer 开放 |
| 现有设置/本体导航 | 原模拟盒子切换、断开/恢复、本体全景入口 E2E 通过 |
| 构建 | Web 与 Desktop 的 TypeScript 检查及 Vite build 均通过 |

本机最初的共享 Python 环境实际装的是 MCP 1.26.0。本次在 `/private/tmp/zhijun-mcp-qa-venv` 安装仓库锁定的 1.29.0 与 PyJWT 2.13.0 后执行最终测试，没有改写当前应用共享 Python 环境。

本地证据日志（临时路径，不随仓库发布）：

- `/private/tmp/zhijun-mcp-acceptance-tests.log`
- `/private/tmp/zhijun-mcp-frontend-tests.log`
- `/private/tmp/zhijun-mcp-ui.log`
- `/private/tmp/zhijun-mcp-settings-regression.log`
- `/private/tmp/zhijun-mcp-web-build.log`
- `/private/tmp/zhijun-mcp-desktop-build.log`
- `/private/tmp/zhijun-external-agents/settings-1440.png`
- `/private/tmp/zhijun-external-agents/settings-390.png`

## 尚未通过正式验收

| 待完成项 | 原因/下一步 |
|---|---|
| 真实 Gateway 的 MCP 身份注册、UDS 转发及用户 ACL capabilities | 本地未找到当前 Data Engine / CentaurOS 配套仓库；按统一合同实现并联合发布 |
| Admin OAuth 授权码 + PKCE、refresh/revocation、票据交换 | 本仓库实现的是资源服务器和账号服务客户端；真实账号端尚未实现/联调，不能以测试票据代替 |
| WorkBuddy OAuth 登录、刷新、重新授权 | 只确认接入方向与提供合同，尚未在真实 WorkBuddy 客户端完成流程 |
| 异地网络、云端测试 Agent | 临时 loopback TLS 测试不等于异地/NAT/公网证书/云端连通性；需真实部署后验证 |
| 关闭知君桌面后持续可用 | 运行时已不依赖桌面代码/凭据，但 CentaurOS 常驻服务尚未安装到盒子，未做真实关桌面验收 |
| 云端进程/日志/缓存检查 | 当前 relay 实现仅转发内层密文，本地测试通过；仍须审查实际公网部署的入口、监控、日志、备份配置和私钥位置 |
| 生产 RAG 联调与灰度 | 测试复用现有 V2 校验器及协议 double；真实盒子资料 ACL、策略更新和 Confirm 必须联合验证 |

当前交付为可审阅、可运行隔离验收的知君端实现及配套转发代码。生产启用开关保持关闭；没有提交虚构用户资料、扩大旧授权、修改历史消息或进行公网部署。
