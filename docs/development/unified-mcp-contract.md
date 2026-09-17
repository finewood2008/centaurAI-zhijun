# 知君统一 MCP V1 合同

状态：知君仓库实现及本地隔离验证；尚未完成 CentaurOS / Data Engine Gateway / Admin 配套发布和 WorkBuddy 实机验收。不能把本文件或本地测试作为正式远程交付证明。

## 入口和责任

每台盒子的外部地址为 `https://<box-entry>/mcp`，MCP 2025-11-25、Streamable HTTP、只读。盒端 `python -m zhijun_mcp` 独立于桌面运行。当前运行实例显式绑定一个 account / box / workspace / ownershipEpoch；换主、解绑或工作区改变必须由管理器注销旧实例、重建绑定，不能修改旧数据根的身份。

| 服务 | 本仓库提供 | 配套仓库必须完成 |
|---|---|---|
| 知君 worker | 授权库、本体规则、RAG 适配、管理路由、五个工具调用合同 | 由 Gateway 以正确身份启动、续租 |
| 盒端 MCP | OAuth 资源服务器、浏览器授权页、Gateway UDS 客户端、TLS 服务、stdio 适配器 | 配置、服务账号、证书签发和生命周期 |
| 加密转发 | `zhijun_mcp.tunnel` relay / connector 实现 | 公网入口分配、mTLS 证书配置、监控、容量和可用性部署 |
| Admin / 账号 | `AccountBroker` 对接客户端与签名断言校验 | OAuth 授权码、S256 PKCE、注册、刷新、撤销，以及下述 consent API |
| Data Engine Gateway | 知君侧接收合同 | MCP 服务身份注册、UDS 两条路由、用户 ACL capability、worker 签名转发 |

默认关闭：管理器配置 `enabled=false`；每个工作区的授权库另有默认 false 的开关。配置齐备也不代表用户已经授权。旧 MCP token、全局“可带走”开关均不转换为新授权。

## 身份和令牌

账号服务签发 JWT access token：`typ=at+jwt`，签名 RS256 或 ES256，明确 `kid`。盒端从管理器下发的 JWKS 文件取公钥，忽略 token 的 `jku` / `x5u`，不根据未验证字段发起网络请求。

必需 claims：`iss, aud, sub, client_id, jti, iat, exp, box_id, workspace_id, ownership_epoch, grant_id, scope`。

- `sub` 是账号 ID；`client_id` 是 OAuth 客户端 ID，不采用用户输入的展示名称当身份。
- `aud` 必须是这台盒子的完整 MCP URL 字符串，拒绝多资源 audience 列表；scope 必须含 `zhijun:read`。
- access token 最长 900 秒；Admin 必须将实际 expiry 限制为 `min(now + 900, grant.expiresAt)`。
- refresh token 轮转、一次性授权码、重放检测和 PKCE S256 由 Admin 实现。刷新不得延长 grant 到期日。
- 盒端每次调用、正文交付前重新检查 grant、账号、Agent、盒子、工作区、ownership epoch、有效期和暂停/撤销状态。旧 access token 即使签名仍有效，也不能绕过本地撤权。
- `GET /.well-known/oauth-protected-resource/mcp` 由 SDK 提供，并通过 401 `WWW-Authenticate` 指向该元数据。
- OAuth 凭据在 MCP 服务终止，传给 worker 的只有已验证身份，不传给 Data Agent。

## Gateway 内部合同（需要在 Data Engine 配套实现）

MCP 进程仅可连接管理器指定的本机 UDS，不接受工具输入的 URL、路径、SQL 或任意 operation。

1. `POST /internal/zhijun/external-agent-tools`
   ```json
   {"principal":{"accountId":"account","boxId":"box","workspaceId":"sha256-workspace","ownershipEpoch":1,"agentId":"oauth-client","grantId":"gr_...","audience":"https://box.example/mcp","expiresAt":1893456000},"tool":"zhijun_get_personal_context","arguments":{"sections":["ways"],"limit":10}}
   ```
2. `POST /internal/zhijun/external-agent-owner`
   ```json
   {"subject":{"accountId":"account","boxId":"box","workspaceId":"sha256-workspace","ownershipEpoch":1},"action":"preview","arguments":{}}
   ```

`X-Zhijun-MCP-Proof` 使用现有 `zhijun_worker.auth` 的 V1 HMAC 编码、5 秒有效期、一次性 nonce、method/path/body SHA256/subject 绑定；**使用独立注册的 MCP 服务密钥，绝不能复用或下发 worker 密钥**。

Gateway 必须：验证 UDS peer UID + 注册服务密钥 + 活跃 ownership/lease；校验 envelope identity 与注册实例一致；校验 operation 白名单；按工作区解析 worker；用 worker 自己的签名密钥转发至 `/v1/external-agent-tools` 或 `/v1/external-agent-owner`。两个新路径不能经桌面通用 product API 暴露。

Owner action 只有 `status, preview, create_grant, request_preview, decide, revoke_grant`。MCP 进程必须先消费账号服务的一次性用户票据并验证签名断言，才能调用这些 action；外部 Agent token 不授予 owner 权限。

worker 新 capability：

- `external_agents.materials.describe({materialIds: string[]}) → {items:[{id,title,version,updatedAt}]}`：最多 1000 个 ID；以当前工作区账号执行用户 ACL，未授权的 ID 不返回，也不返回其存在性、标题、总数或内部路径。
- `external_agents.materials.list({limit:1000}) → {items:[...]}`：仅用于盒端登录用户的授权选择，必须执行同样的用户 ACL。生产接入应返回完整有界选择范围；超过范围须扩展分页后再开放给大规模资料库，不能将未返回资料自动加入 grant。
- capability 的调用 operation ID 为 `external_agent_tools` 或 `external_agent_owner`，管理页面经现有 product catalog 的 `get_api_mindos_settings_external_agents*` / grant 操作调用。

Search / Confirm / Evidence Resolve 仍使用当前 App REST `/v1/agent/apps/*` 和盒端专用 App credential 文件，无旧版 DE 内容 API、SQL 直连或新检索数据库。

## 账号和盒端浏览器授权合同（需要在 Admin 配套实现）

Agent OAuth 授权码 + PKCE 请求由 Admin 校验 resource、注册 client、精确 redirect URI、state、S256 challenge。用户在 Admin 登录。Admin 将短期一次性 opaque ticket 重定向到盒端 `/external-agents/authorize?ticket=...`；票据不含资料内容或名称，不是 access token，10 秒以上的任何有效期都必须有一次性消费保障，建议 60 秒。

盒端使用配置的 mTLS 机器证书调用：

- `POST /v1/zhijun/consents/exchange {ticket,resource}` → `{assertion}`。原子消费 ticket，拒绝重放、错误机器证书、账号/盒子不匹配、过期/换主；返回 `typ=zhijun-consent+jwt`、最长 300 秒的签名断言。
- 断言 claims：标准 `iss,aud,sub,iat,exp,jti` 加 `box_id,workspace_id,ownership_epoch,client_id,client_name,consent_id`；敏感确认登录额外包含 `request_id,state`。client 名称必须来自经过核验的注册记录。
- `POST /v1/zhijun/consents/complete {consentId,resource,grantId,expiresAt}`：绑定原来的 authenticated account/client/resource；只接收授权 ID 与期限，不接收选中类别、条目内容、文件名或预览。取消时 grantId / expiresAt 为 null。必须幂等处理同一 consent，并拒绝以另一个 grant 覆盖结果。
- 浏览器返回 `GET /zhijun/resume?consentId=...`；Admin 恢复原 OAuth 请求并签发一次性 code，不允许浏览器提供任意 redirect URL、scope 或 grant 身份。
- 敏感确认入口：`GET /zhijun/login?resource=...&callback=...&state=...&requestId=...`。Admin 校验 callback 属于注册盒端 origin，登录后产生上面的同类一次性 ticket，断言回传 state/requestId。盒端绑定 HttpOnly 登录 cookie，防止换请求。

盒端浏览器页采用短期 `__Host-` Secure/HttpOnly/SameSite cookie、CSRF token、严格 Origin、HTML escaping、no-store/CSP；授权页中有个人类别、当前可用内容、排除项、工作资料、1/7/30 天期限及外部处理披露。默认不选类别或资料。取消不创建 grant。没有永久授权。

用户关闭桌面不影响以上流程。盒子离线时 TLS 连接失败；不能转向云端缓存、另一个盒子或旧桌面凭据。

## 五个工具

| 工具 | 输入（除这些字段外全部拒绝） | 输出 |
|---|---|---|
| `zhijun_get_access` | 无 | 类别、授权资料 ID、期限、工具能力 |
| `zhijun_get_personal_context` | sections 可省略、limit 1..100 默认 30、purpose ≤300 字 | 有界本体条目，nature/scope/validity/source/version |
| `zhijun_search_work_data` | query 1..1000 字、limit 1..20 默认 5、purpose ≤300 字 | 工作片段或 pending/requestId/confirmationUrl |
| `zhijun_read_work_evidence` | reference | 已有不透明引用对应的有界原文 |
| `zhijun_get_request_status` | requestId | pending / ready / cancelled / failed 等状态；ready 时重新解析可交付依据 |

统一外层 `type, version, updatedAt, receipt`；工作来源 `materialId, reference, location`。不返回 DE 的 `erv2_` / `scf_`、路径、App 密钥。模型输入中的 purpose 不参与授权判断。

个人筛选：confirmed、当前有效、全局 long_term、属于已授权类别、非 challenged / retracted / superseded、public/private、通过 SourcePolicy 和 alignment visibility。保留自述/观察/愿望性质。第一次启用适配时记录全部历史 claim ID（含未确认条目）；旧条目必须在用户预览后明确纳入。未来新条目按类别自动生效。旧 review event 中明确关闭外发形成持续拒绝项；父条目、替换链、决定来源及显式派生 claim 引用的拒绝项继续生效。此机制不改写历史 claim/export flag。

工作读取：当前用户 ACL ∩ grant.materialIds ∩ DE 交付规则。空集合立即返回空；每批 ≤100 个 ID，topK ≤20，总结果有界；来源版本在检索、确认、Evidence Resolve 和交付阶段再次核对。资料 ID 集合固定，已选资料后续版本遵循当时的权限/规则，新增资料不自动授权。暂不开放检测未完成的原文风险放行。

敏感确认：request 绑定 grant/revision/资料版本、最长 10 分钟且不超过 DE token/grant 的期限。只有 owner 路由能 Confirm。确认开始前记为 processing，失败/崩溃不自动重试一次性许可。允许后仅保存证据句柄，领取时再次 Resolve。撤权、暂停、范围更新清理旧引用和请求。

审计在工作区 `db/external_agents.db`：记录 Agent、grant、操作、资源 ID/版本、时间、交付方式、结果和随机回执，无查询/正文/token。审计提交成功是交付前的线性化点；网络断开可能导致 Agent 未收到，回执不是对方已阅读的证明。审计失败拒绝正文。

## 数据边界和运维要求

公网转发端只处理 TCP；盒子主动建立独立 mTLS 控制/数据通道，内层业务 TLS 穿过两者并在盒端终止。relay 的证书只用于外层隧道认证，不能用作业务域名证书。每个 Entry 绑定证书指纹和固定公网监听地址；不能提交任意目标地址。首版实现每入口独立监听 IP/端口，不含共享 443 的 SNI 路由编排；公网部署必须分配符合客户端支持的稳定地址。

该实现不保留磁盘正文缓存、不记录转发字节。临时内存中只有内层 TLS 密文。常规 TLS 信任依赖客户端对证书/DNS/CA 的验证；不能把此测试宣称为抵抗控制域名和 CA 的主动中间人。域名证书由盒端生成私钥/CSR，管理器负责签发与轮转，私钥不得送入云端日志、备份或配置。

授权库由单个持有工作区锁的 worker 拥有；禁止多进程同时直接打开同一库处理授权。MCP edge 不直接打开本体数据库或授权库。公网页面预览也是通过 Gateway 读取绑定工作区。

参考：[MCP 2025-11-25 授权](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)、[WorkBuddy MCP 文档](https://www.workbuddy.ai/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide)。实际兼容性以验收记录为准。
