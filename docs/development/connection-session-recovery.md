# 知君桌面端连接会话恢复

更新日期：2026-09-12。范围：Electron 桌面端；不改变 Admin、Gateway 或盒端的短期授权时限。

## 问题与目标

账号登录保持 7 天与盒子连接存活时间是两回事。旧实现把 `CONNECTIVITY_SESSION_EXPIRED` 当成账号失效，清除登录状态并调用退出登录；短期连接到期、网络变化或 Remote 升级重启因此会将用户提前退出。

修复后，连接失效只恢复连接，账号真正失效才返回登录页。不把连接票据改成 7 天，也不依靠保存的密码静默重新登录来掩盖断连。

## 会话边界

| 对象 | 失效后的行为 |
| --- | --- |
| 账号 Access Token | 在 Refresh Session 有效时刷新，不要求重新输入密码 |
| 账号 Refresh Session / 本地固定 7 天截止时间 | 确认真正失效或被撤销时清除登录 token，要求重新登录 |
| 短期 Connectivity ticket / Direct、P2P 连接 | 保留账号及其原截止时间，申请新票据并恢复原盒子连接 |
| 盒端 workspace lease / 业务授权 | 重新校验，不复用旧 lease、不跳过权限验证；拒绝授权时停止恢复 |

`CONNECTIVITY_SESSION_EXPIRED` 是连接失效分类，并不证明一定是 TTL 到期；其公开恢复动作是 `user_reconnect`。`SESSION_EXPIRED` 才用于账号会话过期。账号服务网络错误不能自行解释为账号撤销。

## 恢复规则

1. 主进程接收当前连接的终止事件，立即撤销旧连接的业务可用状态，递增连接代次，使旧读取和晚到结果失效。
2. 保留当前账号、目标盒子和安全存储中的有效登录 token。自动恢复不请求 Consumer logout，不读取保存密码进行登录，不重置固定登录截止时间。
3. 对同一账号、同一盒子执行有上限的退避重连；每次申请新连接授权，并重新打开、校验 workspace。短期票据的申请和刷新只在可信主进程/适配器执行，令牌不得传给 renderer。
4. 恢复过程中禁止旧连接继续执行业务请求；校验成功后才能进入 `ready` 并重新加载读取数据。连续失败后保留登录与手动重新连接/选择盒子入口，不能无限快速请求服务端。
5. 显式退出、切换账号、切换盒子、关闭应用时取消恢复任务与计时器；晚到连接必须关闭，不能重新覆盖当前状态。普通关闭应用不得当成主动退出账号。
6. 账号真正失效时停止恢复并要求登录；盒子解绑、应用权限被撤销时不得自动绕过拒绝或改连另一台盒子。

默认连续恢复最多 3 次，依次退避 0.5、1.5、5 秒；成功连接稳定 60 秒后才恢复重试预算，防止连接反复抖动造成无限申请票据。首次手动连接失败不进入自动恢复。权限拒绝、合同不匹配、会话配额耗尽均停止自动恢复，保留手动处理入口；不通过自动换票绕过配额。

固定 7 天边界在受保护的 Consumer 请求、刷新前后，以及已建立连接的业务请求和心跳前分别检查。刷新 Access Token 时保留原登录截止时间，并将本地 Access Token 截止时间限制在该边界内，避免临近第 7 天刷新后重新启动客户端出现存储校验不一致。

## 操作安全

- 不自动重放对话发送、修改、删除、上传等写操作，即使新连接成功也不补发。
- 已发送但结果未知的操作保留 `WRITE_OUTCOME_UNKNOWN` 语义，提示用户先检查盒端结果，再决定是否重新提交。
- renderer 在取消旧作用域请求前捕获在途写操作，并在连接控制层保留静态核对提醒；业务页面卸载或自动重连成功不得掩盖提醒。只保留账号、设备、工作区归属元数据，不保留正文或待重发队列；退出账号、明确切换设备或工作区后清除。
- 断开前的读取结果不得投递到新账号、设备或连接代次。新连接确认就绪后可以重新加载只读页面。
- 日志仅记录脱敏错误分类和连接阶段；不得记录 token、保存密码、票据、请求签名、业务正文或完整 ICE 凭据。

## 验证与发布

代码入口：`frontend/shell/runtime/desktop-runtime.cjs` 管理恢复状态与取消；`frontend/shell/production/adapter.cjs` 管理连接和账号授权边界；`frontend/shell/runtime/public-error.cjs` 管理公开错误语义；renderer 只显示状态并接受手动操作。

自动化验收至少覆盖：连接关闭后保留登录且不调用 logout、有限重试、恢复新连接、新旧代次隔离、显式退出/换账号/换设备抢占恢复、真实账号过期、权限拒绝、写结果未知不重放。UI 回归确认连接失效保留账号上下文、账号失效不使用保存密码静默登录。

发布需重建并安装修复后的知君客户端；只升级 Remote 或修改线上 TTL 不会更新已安装客户端的错误处理。现场验收分别执行盒端 Remote 重启、网络切换、超过短连接时限及真实账号截止时间场景。单元测试通过不等于跨网或长时间运行实测通过，发布记录应分别标注。

### 更新客户端后的现场验收

1. 登录并连接盒子，记录登录时间和目标盒子。正常对话、查询资料应不受影响。
2. 在允许中断的测试盒子上重启 Remote，或短暂断开客户端网络。预期进入连接恢复，恢复成功后仍是原账号、原盒子，不要求再次输入密码；服务端不应出现由这次连接故障触发的 Consumer logout。
3. 网络持续不可用时等待有限重试结束。预期显示连接失败并保留账号，允许手动恢复，不循环快速申请票据。
4. 在途提交时断网。预期提醒先核对写入结果；恢复后对话、上传等不自动补发。请使用可丢弃的测试数据，不能据此判断服务端一定未写入。
5. 恢复期间退出登录或选择另一盒子，确认旧连接晚到时不能将界面切回旧盒子，也不能展示旧工作区的数据。
6. 7 天截止边界由注入时钟的自动化测试覆盖；现场长时间验证另行记录，不修改系统时间或生产 TTL 来伪造通过。

### 本次源码验证记录（2026-09-12）

- `frontend/shell`：`node --test --test-concurrency=2 --test-timeout=60000 tests/*.test.cjs`，241/241 通过。
- `frontend/mindos-web`：`npm run test:desktop`，35/35 通过；包含真实编译 `DesktopApp.vue` setup 的重连接线回归，外部服务使用模拟依赖。
- `frontend/mindos-web`：`npm run test:product`，24/24 通过。
- `frontend/mindos-web`：`npm run build:desktop`，类型检查和 Vite 构建通过；保留已有静态/动态混合导入警告，不影响构建完成。
- `frontend/shell`：`node --test --test-timeout=60000 tests/electron.e2e.cjs`，4/4 通过，使用隔离的测试用户目录验证启动和权限隔离；不是线上账号跨网恢复实测。
- 独立审阅已完成。首次默认并发重跑曾在图标测试输出完成后挂起，终止该测试进程后，单文件复核与上述显式并发/超时的全量重跑均通过，未修改图标测试或业务代码来绕过问题。

交付状态：源码修复及桌面前端构建完成，**尚未重新打包发布或覆盖已安装知君应用**；未改变线上 Admin、Remote 或盒端配置。现场长时间验收待安装修复版客户端后进行。

Admin 配套规范见 `../nexusaos-admin/docs/consumer-iot-production-runbook.md` 的“Consumer 登录会话期限”（相对工作区仓库根目录）。

### 外网首次连接预算与诊断（2026-09-13）

0.1.16 正式包将 `connectivity.directConnectTimeoutMs` 设为 4000；0.1.17 起改为
8000，优先保留较慢但可成功的直连机会，并随包交付支持
`--direct-connect-timeout-ms` 的原生 sidecar。配置及原生端都限制整数范围
2000–8000；旧配置不传该参数，保持原生 8000ms 默认值。只调整 Direct
尝试预算，不改变 12 秒 IPC watchdog、TURN 超时、失败来源绑定或工作区授权。
不能将 IPC watchdog 当作 Direct 回退计时器：其超时会终止整个 exchange。

只有已验证的 `DIRECT_TIMEOUT` / `ICE_FAILED` 及匹配失败会话，才会申请
独立 TURN 票据。更短 Direct 预算可能让部分弱网直连更早改走中继；不能将
预算减少 4 秒表述为所有外网连接都固定快 4 秒。

主进程在用户数据目录写 `connection-timing.jsonl`，单文件上限 256 KiB，
最多保留一份轮转文件，权限 0600；写日志失败不得阻塞连接。默认 macOS 路径为
`~/Library/Application Support/zhijun-desktop/connection-timing.jsonl`。
字段仅包含本地随机 attemptId、阶段、毫秒、状态、安全错误码及最终 DIRECT/RELAY，
0.1.17 增加 directBudgetMs 与白名单 fallbackReason（DIRECT_TIMEOUT / ICE_FAILED），
不记录账号、盒子标识、URL、票据、令牌、签名或正文。

阶段包括 runtime_prepare、direct_ticket、direct_connect、relay_ticket、
relay_connect、context_authorize、total_connect。Direct/Relay 的 connect
记录标记 `includesTicket: true`，包含对应 ticket 时间，不能与 ticket 简单相加；
total_connect 从 adapter 开始连接计到工作区授权结束，不含界面点击前的操作。

发布验证分三层：Core 与 sidecar 参数/回退测试；桌面签名、配置、阶段日志和
合法回退测试；安装后的真实外网连接。最后一层应分别记录直连成功和转中继，
按 attemptId 比较各阶段，不能用模拟测试代替真实网络延迟结论。

### 直连优先验收与本地统计（0.1.17）

- 正式配置和原生参数使用 8000ms Direct 预算，明确 ICE 失败仍即时进入合法回退，
  不强制等满预算。不可直连的网络可能比 0.1.16 多等待约 4 秒才尝试 TURN。
- 不因慢连接自动开放路由器端口、关闭防火墙、取消鉴权或绕过资料授权。
- 本地真实 WebRTC 回归人为延迟 answer 4.5 秒：4 秒预算为 SDK_CONNECT_TIMEOUT，
  不错误请求 TURN；8 秒预算真实 Direct 成功并完成 HTTP 请求，仅使用 Direct 票据。
  这是信令延迟回归，不是公司/家庭外网 NAT 成功率实测。
- 另一组及时 answer、双向候选交换延迟 4.5 秒的真实 WebRTC 回归：4 秒预算出现
  SDK_DIRECT_UNAVAILABLE 并申请绑定失败会话的 DIRECT_TIMEOUT TURN 票据；8 秒预算
  约 4.53 秒完成 Direct 和 HTTP，仅申请 Direct 票据。两组均经 race 检查重复两次通过，
  证明增加预算能避免此类不必要的中继申请；测试不分配真实公网 TURN 服务。
- 当前检查未发现候选丢失、禁止对端 IPv6 或等待完整 ICE 收集的实证，未修改相关生产逻辑。

安装后进行多次真实外网连接，再在仓库根目录运行：

```sh
node frontend/shell/scripts/connection-report.cjs
```

可传入明确的日志路径。脚本只读本地日志及一份轮转文件，按 Direct 预算汇总：
成功 Direct / Relay 数、失败、取消、缺少终态的次数、成功连接总耗时 P50/P95。
`directSuccessShare` 是成功连接中的 Direct 占比，不含失败/取消；零成功时为 null。
这里的成功以工作区授权完成为准，不是纯 ICE 成功率。
仅在观察到 relay_connect 时统计回退原因，不将单独 Direct 错误误算为中继连接。
日志轮转可能导致样本缺失，统计不代表全量历史；脚本不上传数据，也不输出身份、地址或原始错误。

0.1.17 正式包已生成于 `frontend/shell/release/`，桌面全量测试、前端桌面测试、Electron
端到端测试、构建、签名校验、DMG/ZIP 验包和搬移后启动冒烟均通过。
DMG SHA256：`18b9f8b643bd40bd177d79063aa8d5cbe5d833396e26cad4f34987eb4c2abd45`。
沿用现有发布配置（Developer ID 签名，未做 Apple notarization）。未覆盖已安装客户端，
本轮未改盒端配置；真实外网连接指标仍待用户安装后采样。
