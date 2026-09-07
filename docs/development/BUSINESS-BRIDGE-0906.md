# D03：盒内逐请求签名桥

状态：2026-09-06 三端代码及本地验证已完成，家中盒子 Agent/DE 已部署；真实 SDK context/空资料页及一次断开重连已通过UI链路；非空资料、退出登录第二轮及跨主体矩阵待完成，`M0-R=false`。此前截图中的旧main在SDK连接前返回 `BUSINESS_BRIDGE_REQUIRED`，根因是未注入业务桥。旧main现已退出，新版已通过 `start-desktop.sh --real` 启动并完成真实登录。部署和回退证据统一见[盒端部署记录](BOX-DEPLOYMENT-0906.md)。

## 计划、依赖及验收

- [x] 在 OS 和 data-engine 独立工作区实现桥签发/验签，主工程实现SDK握手和受控资料请求。
- [x] Agent 从当前有效授权快照取得Owner，仅为已验证的PC资料会话和两条GET路由签发短时证明；外部传入的身份头继续拒绝。
- [x] data-engine 使用本机配置的独立Ed25519公钥验签，核对设备、应用、路由、时间、nonce及撤销；不将loopback或P2P成功作为业务身份。
- [x] 客户端经同一SDK session握手并匹配account/client/device/application后进入ready；后续每个GET仍需盒端重新授权。
- [x] 运行跨语言向量、签名/重放/错主体/错路由负向测试及集成测试，再构建可部署补丁与回退说明。
- [x] 在家中真实盒部署 Agent/DE，核对当前release、密钥权限、debug=0、健康及正式负向请求。
- [x] 退出旧main并启动新版桌面；用户已真实登录，家中AMD盒context/空资料页/刷新及一次断开重连通过UI链路。
- [ ] 经真实SDK验证当前账号资料读取、断开后拒绝及跨主体矩阵；没有完整真实结果不关闭M0-R。

影响文件：知君 `frontend/shell/{main.js,production/,runtime/public-error.cjs,tests/,scripts/prepare-bridge-release.cjs}`、独立桌面Vue和集成文档；OS的 `remote-agent/internal/{mindosbridge,application,config,control,p2p}` 及应用清单；data-engine的gate、bridge验签/只读分页模块、合同、nonce store和测试。依赖：现有Admin授权快照、固定SDK/sidecar、盒端独立密钥与SSH部署入口。

验收标准：未经验证不能ready；错主体/过期/伪造/重放/越界均拒绝；查询只读且不触及旧global资料；退出/换盒后旧结果与迟到连接失效；真实成功必须有已授权SDK资料请求证据。

分工：主代理负责本工程desktop、共享合同、跨仓合流和最后验收；OS代理独占隔离OS工作区；服务端代理独占隔离data-engine工作区。原data-engine工作区含用户修改，保持不动。

## v1 合同

目标固定 `mindos-person-data-pc` / `person-data.read` / `remote.p2p` / electron / SOVEREIGN_DIRECT_ONLY / DIRECT_ONLY。只开放 `GET /api/mindos/connectivity/context` 和 `GET /api/mindos/materials`；此桥不开放写、上传、其他应用或legacy RemoteOps。

Agent在已通过连接票据、实时授权快照、应用路由与Direct路径校验后，给盒内HTTP请求增加 `X-Nexus-Mindos-Bridge`。该头不能由SDK或renderer提交，不经过外部header长度上限；Agent使用独立本机Ed25519私钥（PKCS8 PEM），data-engine只持SPKI PEM公钥。私钥仅盒内生成与保存，不能复用Consumer/设备/OTA密钥。

头值为 `v1.<base64url(payload JSON UTF-8，无padding)>.<base64url(Ed25519签名，无padding)>`，总长不超过4096字节。签名消息为ASCII `NEXUSAOS-MINDOS-BRIDGE-V1\n` 拼接payload原始字节；验签无需重序列化JSON。payload仅含下表字段，未知/重复JSON键拒绝。

| 字段 | 约束 |
| --- | --- |
| v | 整数1 |
| accountId / clientId / deviceId / sessionId | 1–128 ASCII `[A-Za-z0-9._:-]`，首位字母或数字 |
| applicationId | 固定 `mindos-person-data-pc` |
| ownershipEpoch / authVersion | 正整数，不大于JS安全整数 |
| requestedScopes | 恰为 `["remote.p2p"]` |
| iat / exp | UNIX整数秒，0 < exp-iat <= 5；exp不得超过P2P会话与grant到期二者；允许iat最多超前2秒，exp<=now即过期 |
| nonce | 24字节安全随机数的base64url，无padding（32字符）；验签后原子消费，重复拒绝，不淘汰仍有效nonce来接纳新请求 |
| method | 固定GET |
| relativePath | 原始path+query，匹配实际HTTP请求目标，<=2048；只允许上述两条GET路由 |
| bodySha256 | 空GET body的SHA256十六进制小写 |

Agent每次签发重新读取受信授权快照并绑定同一条有效grant与Owner；快照不新鲜、grant撤销、epoch/authVersion变化、连接关闭/过期立即停止新签发。data-engine逐请求验证签名/路由/设备及本地撤销状态；证明最多5秒有效，不能承诺控制面撤销传播本身零延时。

握手 `GET /api/mindos/connectivity/context` 经同一 gate 验证后返回纯JSON（无envelope）：

```json
{"version":1,"accountId":"account-vector","clientId":"client-vector","deviceId":"device-vector","applicationId":"mindos-person-data-pc","capabilities":["materials.read"],"expiresAt":1893456005}
```

客户端上限8KiB，严格检查字段、200/application-json、绑定及有效期；请求证明在Agent内生成，客户端不持业务token。握手仅证明该时点身份通过，后续资料请求仍逐请求验签。退出/换盒关闭SDK session并使旧适配器失效；不留下可复用业务session token。

资料归属：bridge请求必须使用由已验account/device/ownershipEpoch导出的隔离scope，不能读取或自动归属旧 `global` 或其他Owner的资料。已有资料迁移需单独明确归属；空列表是合法响应，但不能把空列表当作历史资料迁移已完成。业务写暂不通过此桥。

本机路径配置：Agent增加可选 `mindosBridgePrivateKeyFile`（绝对路径）；data-engine使用 `MINDOS_AGENT_BRIDGE_PUBLIC_KEY_FILE`、已有 `MINDOS_DEVICE_ID`。未配置时保持拒绝；原专用JWT/session路线不删除，提供了无效bridge头时不能退回旧session或debug路线。


## 已交付代码与审核结论

| 仓库 | 分支 | 提交 |
| --- | --- | --- |
| 知君 | `dev/first-integrate-check-0905` | 本记录随客户端修复提交；起点 `0550383` |
| OS / Agent | `dev/zhijun-business-bridge-0906` | `0a004c9`，基线 `9f7354e` |
| data-engine（早期实现） | `dev/zhijun-business-bridge-0906` | `f6b1089`，基线 `ec2854e`；测试权限修复 `a74d06e`；原dirty工作区未改 |
| data-engine（当前部署对应） | `dev/zhijun-business-bridge-0906-live` | `c16dc17be81240285820b9e86076877911d153c4`，基线 `6b549ad3371b5250a4b6e6f2afd7cd06c61ef6b0` |

完整提交和SDK产物哈希保存在[版本基线](integration-release-baseline.json)。盒端实现分支已推送远程；当前家中部署事实另以[现场记录](BOX-DEPLOYMENT-0906.md)为准。并发新发布的权限、分页、错误边界及Pocket改动保留；运行中6个D03文件与live交付一致，原调研上传协议仍是历史基线，不表示新版本上传四层集成已验收。

- Agent在Offer时绑定Owner，逐请求从同一新鲜快照重新核对Owner和grant；顶层authVersion是所有grant的最大值，不能与每客户端版本强行相等。快照限1MiB，密钥限8KiB，安全读取拒绝软链接/FIFO及不安全权限；grant可选expiresAt参与证明期限。
- DE公钥只接受单个Ed25519 SPKI PEM，限4KiB，拒绝可被组/其他用户写入的文件。合法签名仍要验证本机device、路由、时间、本地ACL/撤销与持久nonce。失效证明不能退回local-debug或旧session。
- 本地旧epochGeneration不是ownershipEpoch；仅使用本地epoch更新时刻作为保守失效屏障，iat在同秒或更早即拒绝。nonce存储跨重启保持，原子消费，最多10,000条活记录；容量满拒绝新请求，不驱逐活记录。
- 资料scope为 `bridge:v1:` 加UTF-8紧凑JSON数组 `[deviceId,accountId,ownershipEpoch]` 的SHA256小写十六进制。SQL先按scope过滤，再计数/分页；只读连接和query_only阻止旧status_of修复任务的写副作用。新scope没有pipeline任务时显示uploaded，不使用global/watcher回退。
- 资料只返回5个字段；query仅limit/offset/keyword/type/status，重复或未知字段、越界先拒绝。context和资料实际序列化分别限制8KiB和256KiB。关键词过滤、queued映射、分页和时间戳使用真实临时SQLite回归。
- 桌面只将精确 `SDK_CONNECTION_CLOSED` 映射为会话失效，退出ready并关闭资源；普通403仍为访问拒绝，不冒称令牌撤销。connect在读取身份前捕获epoch，退出后的迟到身份不能启动新native连接。

## 首次本地验证（历史统计）

| 范围 | 结果与证据 |
| --- | --- |
| 共享签名合同 | [合成向量](contracts/mindos-bridge-v1.json)；Go逐字生成相同header，Python验签并校验持久重放；公开测试seed仅用于fixture |
| 桌面宿主 | 73项全部通过，包含握手/失效/迟到连接回归；新增两个竞态用例先失败、修复后通过 |
| Electron | 4项通过；独立临时userData，模拟分页/切盒/退出与正式密码输入边界；没有读取已登录窗口凭据 |
| Agent | `TMPDIR=/tmp go test ./...` 全套通过；签发/Registry/P2P的race测试通过；Linux ARM64编译通过。初次macOS临时socket路径过长，改用/tmp后通过 |
| data-engine | 7个相关测试模块共101项通过；签名、重放、nonce并发/容量/重启、错主体、无降级gate及只读分页隔离 |
| 构建与文档 | Desktop构建通过；4段Mermaid已渲染，目标SVG已更新；最终差异/链接/JSON检查随提交执行 |

这些是本地或合成验证。真实Consumer登录和两台在线授权设备列表已在用户窗口确认；该阶段真实Direct资料、跨账号/设备隔离及断开拒绝尚未验；后续真实UI增量见下节。

## 当前部署与新增验证

2026-09-06 10:57:23 CST 家中盒子已加载新桥。Agent active / Gateway connected / 授权快照新鲜；DE debug=0、health200，无签名context/materials401、伪造格式及错误签名401，服务NRestarts=0。盒端Python3.14 / cryptography49.0 / FastAPI0.138.1 / pydantic2.13.4下，临时合成环境55项桥测试和实际server.app 5项检查通过；live分支本轮明确8模块集合的隔离回归111项+6个subtests通过。后续用户已真实登录，SDK context/空资料响应及一次断开重连通过UI链路；隔离测试和这次空页均不替代非空资料及完整M0-R矩阵。

新部署输入的12项哈希、ELF架构和6运行文件匹配已通过；manifest SHA-256为 `ae24b1f7b11242caaecfae153f3b83c7cca441c9d631d3d7d34c6641c7e4de25`。`manifest.deployed=false`表示这个包本身未执行安装：现场部署的是经合并核对、内容相同的6运行文件和Agent，应用清单仅补context，未整包覆盖。

## 当前 live 分支回归复现

以下命令在 `nexusaos/.worktrees/zhijun-bridge-data-engine-current/backend` 执行，复用原DE的venv作为Python解释器，仅运行当前隔离检出的测试；不启动服务、不进入应用lifespan、不读取用户运行数据库。8个模块明确列在命令中；所有数据/密钥路径在导入前置于新临时目录，保留umask002以回归合法公钥显式权限。

```sh
rtk proxy python3 - <<'PY'
import os
import subprocess
import tempfile
from pathlib import Path

python = Path('../../../nexusaos-data-engine/backend/.venv/bin/python').resolve()
modules = [
    'test_agent_bridge', 'test_agent_bridge_server_app',
    'test_connectivity_session', 'test_connectivity_admin',
    'test_connectivity_state', 'test_mobile_mindos_bridge',
    'test_mindos_local_web_debug', 'test_mindos_connectivity_ticket',
]
with tempfile.TemporaryDirectory(prefix='zhijun-live-repro-') as root:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('CENTAUR', 'MINDOS_')):
            env.pop(key)
    env.update(
        CENTAURAI_DATABASE_DATA_ROOT=root + '/data',
        CENTAUR_SECRET_STORE_DIR=root + '/secrets',
        CENTAUR_METADATA_DB=root + '/data/metadata.db',
        CENTAUR_GBRAIN_HOME=root + '/data/gbrain',
        CENTAUR_MCP_DATA_DIR=root + '/data/mcp/data',
        CENTAUR_MCP_CONFIG_DIR=root + '/data/mcp/config',
        MINDOS_DELETION_LEDGER_DB=root + '/secrets/deletion.db',
        MINDOS_RUNTIME_ENV='production',
        MINDOS_LOCAL_WEB_DEBUG_ACCESS='0',
        PYTHONDONTWRITEBYTECODE='1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
    )
    os.umask(0o002)
    result = subprocess.run([
        str(python), '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
        *['tests/' + module + '.py' for module in modules],
    ], env=env)
    raise SystemExit(result.returncode)
PY
```

文档收尾复跑上述明确集合为 **111项 + 6个subtests通过**（Python3.11、FastAPI0.141.1、cryptography50、pydantic2.13.4），另有现有httpx和pkg_resources弃用提示。这是本轮明确8模块集合的可复现结果，不与旧基线101项相加。真实server.app用例覆盖合法context、重放、无签名、无效桥不降级与窄空资料页；缺失临时资料文件仅证明新scope合法空页，不证明历史资料迁移。

## 部署输入、验收与回退

从知君工程根目录构建与固定提交对应的本地部署输入：

```sh
rtk proxy node frontend/shell/scripts/prepare-bridge-release.cjs
```

默认从两个隔离worktree构建，要求干净且HEAD与版本基线完全一致。也可传入三个绝对路径：OS工作区、DE工作区、新输出目录。父目录需已存在且不含软链接，已有输出不会覆盖。当前默认使用 DE live worktree，输出在忽略目录 `data/desktop/bridge-release-0906-live/`，含Linux AMD64/ARM64 Agent、两仓补丁、6个DE运行文件、应用清单参考及SHA256 manifest。该包不含盒内私钥、不执行SSH、不安装服务，不是签名OTA产物。

1. 每次部署前只读核查盒子架构、实际deviceId、Agent/DE版本、systemd服务用户、ExecStart、配置/授权快照路径和数据库路径。本次SSH认证已解决且部署完成；须复核90 drop-in哈希、NeedDaemonReload、MainPID cwd与实际源码哈希，防止并发发布使旧release补丁未生效。
2. 核对manifest和线上基线；部署前保存原二进制、变更源码、应用清单及环境配置。只在相容源码上应用补丁，不用整份参考清单覆盖其他应用登记，也不覆盖盒内未核对的业务改动。
3. 在盒内生成独立Ed25519密钥对：Agent读取owner-only PKCS8私钥，DE只读取不可由组/其他用户修改的SPKI公钥。分别设置Agent的 `mindosBridgePrivateKeyFile` 和DE的 `MINDOS_AGENT_BRIDGE_PUBLIC_KEY_FILE`、实际 `MINDOS_DEVICE_ID`。不使用测试seed或Consumer/设备/OTA密钥。轮换时替换公钥并重启Agent使旧签名失效，新会话重新握手。
4. 保持 `MINDOS_LOCAL_WEB_DEBUG_ACCESS` 关闭，验证配置后有序重启盒端服务，再重启新版知君并由用户重新登录。本次旧main已退出、新版已真实登录并通过空资料UI链路；只刷新renderer无法加载主进程修复。
5. 验证账号→授权设备→Direct→context四主体一致→资料分页/筛选→断开/重连；对错主体、已撤销/过期和跨设备分别保存脱敏结果。不能以健康接口、空列表或合成资料证明历史资料已归属；历史global记录没有自动迁移，需要单独确定其合法Owner与所有权代次。
6. 如配置或验收失败，恢复已备份的二进制、源码、应用清单和环境并重启原服务。新增nonce表属于增量schema，不删除数据库或清除用户资料；恢复旧客户端/服务后，资料桥保持关闭。不要用开debug作为回退方式。

当前待办是非空资料、退出后重新登录的第二轮及跨账号/设备/撤销矩阵；SSH认证失败及未部署均为已解决历史。[真机记录](REAL-ACCEPTANCE-0906.md)在完成完整验收前保持M0-R未通过。
