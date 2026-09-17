# 公司盒子外部 Agent 设置兼容修复

## 计划与验收

- [x] 用当前账号经正式连接只读复现设置请求，核对 192.168.0.7 的运行源码。
- [x] 核对 Gateway 错误码来源、桌面错误传递与未部署时的界面设计。
- [x] 保留精确的未注册操作错误；让设置页兼容 HTTP 与桌面桥接两种错误类型。
- [x] 回归旧盒子未部署、真实权限拒绝、服务故障、手动重试与正常管理界面；完成类型检查。
- [x] 复核实际盒端响应经过修复后保留精确诊断，整条桌面链路测试确认正确分类，记录发布边界。

影响文件：shell 的 `runtime/product-policy.cjs`、错误回归测试；web 的
`services/externalAgents.ts`、`components/settings/ExternalAgentsPanel.vue`、相关单元/桥接/UI 测试。
不修改授权规则，不自动开启 MCP 或创建授权。

## 现场证据

实际操作 `get_api_mindos_settings_external_agents` 在 Gateway 创建任务前返回
HTTP 403 / `WORKSPACE_OPERATION_DENIED`。该错误仅由未知 operation ID 触发。
公司盒子 `zhijun-integration-0907` 健康；运行清单仍为 182 个操作，没有 external_agents，
Worker 未注册管理路由，`backend/zhijun_mcp` 不存在，未配置 MCP resource URL。

- 运行 catalog SHA256：`3b4d0a8169334cb8d6548a51f44583c5c5e56e13e54ae19832b25dd722b8722d`。
- 运行 worker app SHA256：`5da5ef752c181e88229987071a446d7c0ae54cbd177dd663be1df8df2adc4a7b`。

客户端 shell 原先将全部 403 折叠成 `ACCESS_DENIED`，丢失具体码；设置组件又只识别
`ApiError`，无法处理桌面 `ProductFailure`。这导致旧盒子被显示为通用读取失败。
兼容修复只识别明确的未部署信号，普通权限拒绝与 5xx 仍显示失败。

完整 MCP 的生产配套依赖另见 `unified-mcp-deployment.md`；当前代码合并不代表
Admin OAuth、Gateway ACL、盒端 worker/MCP、TLS 与 relay 已完成部署。

## 修复与验证结果

仅对 HTTP 403 + 顶层 `WORKSPACE_OPERATION_DENIED` 保留该固定 remoteCode，
解析不超过 8192 字节，不透传远端正文；公共 ACCESS_DENIED 语义保持不变。
设置状态读取根据精确码判断旧 Gateway，兼容 ApiError 和 ProductFailure；
不把一般 403、401、5xx 或桌面任务不存在视为未部署，不对写入套用此兼容。

- shell operation/product/capacity：39 项通过。
- web external-agents/product-transport：35 项通过；包括真实
  `createProductSession → decodeJson → toPublicError → ProductFailure → API` 链路。
- Vue E2E：旧 Gateway、旧 Worker、权限拒绝、服务故障和手动重试通过；原有宽/窄屏管理场景通过。
- `npm --prefix frontend/mindos-web run build:desktop`（含类型检查）通过；`git diff --check` 通过。
- 公司盒子真实复测仍返回原 403；新 shell 保留正确的 remoteCode。未部署新盒端代码，
  未重启服务、开启外部访问或创建用户授权。

本次已完成源码修复和桌面 Web 构建，尚未重新制作安装包或替换已安装客户端。
完整 MCP 功能仍需按配套发布顺序上线；修正提示不等于完成公网 Agent 接通。

## 后续交付

同日根据用户后续要求完成公司盒更新，正式设置操作返回 200 与未开通状态，
详见 `company248-worker-mcp-update-20260917.md`。
已生成包含上述修复的桌面 0.1.34 正式安装包，签名、验包及独立启动通过，
详见 `release-0.1.34-external-agents.md`；未替换本机已安装客户端，公网 MCP 尚未开通。
