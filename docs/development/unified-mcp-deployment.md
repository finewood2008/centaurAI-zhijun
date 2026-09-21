# 知君统一 MCP 部署与调用

> **2026-09-21 失效声明（PRD V2 的 P0「删盒端耦合」）。** 本文描述的是盒端 + 云端隧道部署。
> 支撑它的五个模块已从仓库删除：`zhijun_mcp/__main__.py`（CentaurOS 管理的进程入口）、
> `gateway.py`（盒端 IPC）、`account.py`（账号服务适配）、`tunnel.py`（反向 TCP 隧道）、
> `stdio.py`（指向公网 HTTPS 的 stdio 适配）。因此**第 3、5 步与第 74、100 行的命令已经跑不起来**。
>
> 仍在仓库里、且是目标态基础的部分：`management.py`（`server.py:1281` 注册的唯一活路由）、
> `server.py`、`browser.py`（授权页渲染，[MCP 契约](../product/ZHIJUN_MCP_CONTRACT.md) 5.2 要复用）、
> `auth.py`、`store.py`、`service.py`、`personal.py`、`models.py`、`runtime.py`、`http.py`。
>
> 目标态是本机 stdio + `127.0.0.1` HTTP + 本地 token，没有云控制面，见 [MCP 契约](../product/ZHIJUN_MCP_CONTRACT.md) 第 3 节。
> 本文暂留作盒端部署的历史记录与合同参照，不要照着它部署。

本仓库代码曾提供 box server、只读适配、浏览器授权页、stdio adapter 和密文 relay/connector。**不能直接启用到现有生产盒子**：先按 [统一合同](unified-mcp-contract.md) 在 Data Engine Gateway / Admin 实现对应路由、身份注册、ACL 和 OAuth 服务；当前信令链路不等同于本次 TCP 隧道。

## 配套发布顺序

1. Admin 实现标准 OAuth 授权码 + S256 PKCE、token 最长 15 分钟、refresh 轮转/期限限制，以及合同里的三个 browser/consent 流程。核验 client identity，精确登记 callback 和 resource。
2. Gateway 注册独立 MCP service key / peer UID / subject；添加两条受限 UDS 入口以及 ACL capabilities；更新 product operation catalog。不得给 MCP 进程 worker proof key 或桌面 bearer 凭据。
3. 盒端安装仓库锁定的 Python 依赖（MCP 1.29.0），在隔离运行环境打包 `zhijun_mcp` / `zhijun_worker`，由 OS 管理器启动/续租 worker。
4. 盒端生成业务 TLS 私钥和 CSR，签发受客户端信任、与固定入口域名匹配的证书。私钥及所有服务 key/config 文件 owner-only 0600、父目录 0700。TLS 业务 key 只存在盒端。
5. 云端部署 `python -m zhijun_mcp.tunnel relay --config /etc/zhijun/relay.json`；盒端部署 connector 和 `python -m zhijun_mcp --config /etc/zhijun/mcp.json`。业务 TLS 服务仅监听 127.0.0.1:8643。
6. 完成外网、云端 Agent、WorkBuddy、桌面退出、盒子离线、撤权等验收后，灰度打开管理器配置；用户仍须在「设置 → 外部 Agent」主动启用并逐个授权。

## 配置示例（占位域名，默认关闭）

MCP 盒端：

```json
{
  "enabled": false,
  "subject": {"accountId":"ACCOUNT","boxId":"BOX","workspaceId":"WORKSPACE_SHA256","ownershipEpoch":1},
  "resource":"https://box.example.com/mcp",
  "issuer":"https://accounts.example.com",
  "issuerJwksFile":"/etc/zhijun/issuer-jwks.json",
  "gatewaySocket":"/run/centauros/zhijun-gateway.sock",
  "gatewayKeyFile":"/etc/zhijun/mcp-gateway.key",
  "accountClientCertFile":"/etc/zhijun/account-client.pem",
  "accountClientKeyFile":"/etc/zhijun/account-client.key",
  "tlsCertFile":"/etc/zhijun/business-chain.pem",
  "tlsKeyFile":"/etc/zhijun/business.key",
  "port":8643
}
```

worker 环境新增 `ZHIJUN_MCP_RESOURCE_URL=https://box.example.com/mcp`，沿用已经隔离的工作区目录、subject、Data Agent credential 和 Gateway capabilities。不要在单机 `server.py` 的默认全局数据根开放外部 MCP。

Relay：

```json
{
  "enabled":false,
  "host":"0.0.0.0", "port":7443,
  "caFile":"/etc/zhijun/tunnel-ca.pem",
  "certFile":"/etc/zhijun/relay-tunnel.pem", "keyFile":"/etc/zhijun/relay-tunnel.key",
  "entries":[{"box_id":"BOX","fingerprint":"CONNECTOR_CERT_SHA256_HEX","host":"PUBLIC_BIND_IP","port":443}]
}
```

Connector：

```json
{
  "enabled":false,
  "relayHost":"relay.example.com", "relayPort":7443, "localPort":8643,
  "caFile":"/etc/zhijun/tunnel-ca.pem",
  "certFile":"/etc/zhijun/connector.pem", "keyFile":"/etc/zhijun/connector.key"
}
```

防火墙只开放公网业务监听端口与 mTLS 隧道端口。禁止在 relay 前加终止业务 TLS 的 HTTP ingress / CDN / API gateway。管理器负责机器证书指纹注册、轮转时断开旧隧道、解绑时停止服务、外层证书过期、连接容量与离线告警。

## OS 生命周期模板

```ini
[Unit]
Description=Zhijun read-only MCP endpoint
After=network-online.target centauros-zhijun-gateway.service
Requires=centauros-zhijun-gateway.service

[Service]
User=zhijun-mcp
WorkingDirectory=/opt/zhijun/backend
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/zhijun/venv/bin/python -m zhijun_mcp --config /etc/zhijun/mcp.json
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

这是对接模板，真实服务名由 CentaurOS 管理器确定。不能将模板中占位的 Gateway 单元视为已部署。connector 使用同等限制的独立单元，ExecStart 改为 tunnel connector。云 relay 使用单独服务账号与外层隧道 key。

## Agent 使用

支持远程 MCP 的 Agent：添加标准 Streamable HTTP 地址 `https://<box-entry>/mcp`，按 OAuth 浏览器流程完成授权。无需在配置中复制长期 bearer token。

stdio 客户端示例：

```json
{
  "mcpServers": {
    "zhijun": {
      "command":"/opt/zhijun/venv/bin/python",
      "args":["-m","zhijun_mcp.stdio","https://box.example.com/mcp"],
      "env":{"PYTHONPATH":"/opt/zhijun/backend"}
    }
  }
}
```

参考适配器调用 SDK OAuth/PKCE、使用临时 loopback callback 和内存 token storage；退出后不保留凭据。无浏览器环境应优先使用支持远程 OAuth 的宿主客户端；第一版适配器不提供复制密码/固定 token 的旁路。

同一连接先调用 `zhijun_get_access`，再按需调用：

```json
{"name":"zhijun_get_personal_context","arguments":{"sections":["principles","ways"],"limit":10,"purpose":"按我的偏好整理方案"}}
{"name":"zhijun_search_work_data","arguments":{"query":"项目交付验收范围","limit":5}}
{"name":"zhijun_read_work_evidence","arguments":{"reference":"ev_FROM_SEARCH_RESULT"}}
{"name":"zhijun_get_request_status","arguments":{"requestId":"rq_FROM_PENDING_RESULT"}}
```

如果返回 pending，用户打开 confirmationUrl 决定脱敏交付、原文交付或取消；Agent 只能查询状态。证据过期、授权变更或资料版本变化后重新检索，不重放确认。

## 验收命令

从仓库根目录：

```bash
PYTHONPATH=backend python -m pytest backend/tests/test_unified_mcp.py backend/tests/test_unified_mcp_http.py backend/tests/test_unified_mcp_tunnel.py -q
```

从 `frontend/mindos-web`：

```bash
npm run test:all
node tests/external-agents.e2e.mjs
npm run build
npm run build:desktop
```

TLS 测试生成临时证书、启动 loopback TCP 和 mTLS 通道，需要允许本机监听；测试不接触真实盒子/用户资料。当前验收记录见 [兼容性记录](unified-mcp-acceptance.md)。
