# 知君自动配置与真机验收

> **完整产品v2后续状态（2026-09-06）：** 本文保留早期M0/v1阶段证据与任务语义；当前15页/170项受控操作、独立UDS worker、DE Gateway及能力适配已形成实现与本地回归；隔离真盒hardware-candidate5五项、gateway-candidate6十项已通过；均为合成主体/输入，非正式Consumer/UI。OS `5f5f4c9`、Admin `44a0950`及知君 `58dac31`已提交推送，DE完整增量 `015c659`已提交推送，DE FD热修 `132b97d`已单独部署；盒端Agent/DE/worker/catalog已按clean heads知君 `735e341`（代码 `58dac31`）/DE `015c659`/OS `5f5f4c9`匹配部署；Admin `44a0950`尚未生产部署且发布入口未提供，正式Consumer/SDK/P2P/UI与实际麦克风验收仍pending。新目标是 `zhijun-desktop / zhijun.workspace`，本文旧 `mindos-person-data-pc / person-data.read` 参数仅用于历史只读合同。最新计划见[FULL-PRODUCT-INTEGRATION](FULL-PRODUCT-INTEGRATION-0906.md)，复核见[Gateway审核报告](../reports/FULL-PRODUCT-GATEWAY-REVIEW-0906.md)；170项清单不是170项UI实测。

日期：2026-09-06。自动配置起点：`689bb11`；D03修复起点：`0550383`。

## v1阶段计划与验收标准（历史证据保留）

- [x] 核查 Admin、Agent、data-engine 三端登记及 SDK 产物，区分目标应用 ID 和登录客户端 ID。
- [x] 将受信服务地址、PC 资料应用绑定及当前平台 sidecar 自动写入本地配置；固定哈希，不写入密码或令牌。
- [x] 验证真实 macOS safeStorage、原生 sidecar、HTTPS 服务和目标设备可达性，结果逐项记录。
- [x] 启动正式桌面并保留应用内登录入口。
- [x] 使用知君自身客户端登录并检查授权设备，看到两台在线已授权盒子。
- [x] 编码桌面/Agent/data-engine签名桥，完成本地正反向合同测试。
- [x] 在家中真实盒部署 Agent/DE 业务桥，验证正式服务健康、debug=0、无签名及伪造请求拒绝。
- [x] 退出旧桌面 main，通过 `start-desktop.sh --real` 启动新版，保留登录入口。
- [x] 新版真实登录，家中AMD盒SDK/context/空资料页及刷新、一次断开清理和重连通过UI链路。
- [ ] 验证非空资料、退出后重新登录的第二轮、跨账号/设备及撤销矩阵。
- [x] 更新架构、集成和任务状态，整理可复现脚本与脱敏证据；提交/推送状态由最终Git结果确认。

影响文件：`frontend/shell/config/`、`frontend/shell/scripts/`、`frontend/shell/production/`、`start-desktop.sh` 及集成文档；Agent/data-engine在各自独立worktree开发，原data-engine的用户修改保持不动。生成配置和二进制放在已忽略的 `data/desktop/`，自动配置阶段未部署；后续家中盒端部署已完成，未迁移或读取非空业务资料；真实桥资料API返回0条。

依赖：现有 Consumer 账号及授权设备、SDK 平台产物、系统钥匙串，以及 Agent → data-engine 可信业务身份桥。真实登录必须由用户在知君窗口输入；不读取别的应用的登录态。验收通过必须包含已授权资料读取，只有网络可达、原生进程或 UI 启动不能关闭 M0-R。

## 参数来源（v1只读阶段，不作为v2新应用参数）

`applicationId` 是 SDK 访问的盒端目标应用；`clientId` 是知君登录时独立创建的客户端身份。接入已登记的 PC 资料服务可使用 `mindos-person-data-pc`，不因此共用原 PC 应用的登录凭据。之前文档将独立客户端身份进一步推导为必须新建目标 applicationId，现予纠正。

| 参数 | 值 | 证据 |
| --- | --- | --- |
| Consumer | `https://boss.nexusaos.qitus.cn/prod-api` | data-engine `frontend/resources/connectivity-product.json` |
| applicationId | `mindos-person-data-pc` | Admin `RemoteOpsStreamService.CONNECTIVITY_APPLICATION_POLICIES` 和 Agent 应用清单 |
| purpose | `person-data.read` | Admin PC 签票策略 |
| requestedScopes | `["remote.p2p"]` | Admin 签票策略和 Agent requiredScopes；这是传输权限，不能替代业务授权 |
| gatewayHost / iceHost | `gateway.remote.qeeshu.com` | data-engine 受控产品配置 |
| profile / transportPolicy | `SOVEREIGN_DIRECT_ONLY` / `DIRECT_ONLY` | Admin 固定 PC 策略 |

上述登记对应 `127.0.0.1:8618` 上的 data-engine，而非尚未迁入盒端的知君领域 API。独立知君应用目录如后续需要，应由单独的路由/权限变更落实；当前最小资料链路无需凭空建立另一个目标。

## 初次真实检查（历史，SSH 阻塞已解决）

2026-09-06 09:13 CST：本机 macOS 26.5.2 / ARM64；家中 `192.168.1.18:22` TCP 可达，办公盒 `192.168.31.248:22` 超时；家中 SSH 密钥认证被拒。使用系统信任链检查 Admin 和 Gateway，未禁用 TLS：Gateway live/ready HTTP 200；Admin 未认证设备请求 HTTP 200、业务 `code=401`、`success=false`，没有获得设备数据。

## 业务授权差额

用户截图中的报错来自旧main未注入真实业务桥。现已实现[D03逐请求签名桥](BUSINESS-BRIDGE-0906.md)：Agent使用独立盒内密钥签名，data-engine验证后返回业务上下文；桌面通过同一SDK session核对主体后才ready。后续盒端已部署，新版已真实登录并连接家中AMD盒，context握手与空资料页已通过UI链路；刷新无错误，一次断开清理及重连通过。原 `BUSINESS_BRIDGE_REQUIRED` 场景已在该只读链路复验，完整M0-R仍待补齐。Admin P2P票据仍不等于data-engine专用JWT，没有通过放宽guard处理。

## 本次结果

| 项目 | 结果 | 实际边界 |
| --- | --- | --- |
| 自动配置 | 通过 | 完整JSON通过正式loader；固定PC目标参数；二进制复制后哈希一致，重复运行不覆盖配置 |
| macOS系统存储 | 通过，4组 | Electron 37.10.3真实safeStorage；临时生成的合成身份/令牌完成加密、重载、ECDSA签名、明文扫描及本地令牌删除；未登录真实账号 |
| macOS原生sidecar | 通过，6项 | 真正执行ARM64二进制，版本1.2.0/protocol1、哈希、缺配置/未知会话/错误协议拒绝及关闭幂等；没有发送connect |
| 云服务和设备网络 | 部分通过 | Admin/Gateway在线、未认证请求拒绝；家中22端口可达，办公盒超时；TCP可达不能证明设备Owner授权 |
| SSH部署检查 | 后续通过 | 初次默认公钥认证失败已解决；已核对实际release与服务身份，完成备份、最小部署及健康/权限检查，见[盒端记录](BOX-DEPLOYMENT-0906.md) |
| 知君登录与授权设备 | 通过 | 用户在知君输入账号；实际UI确认已登录，并显示两台在线已授权设备；未记录手机号、令牌或资料正文 |
| D03本地代码验证 | 通过 | Go/Python共享签名向量、重放/错主体/路径/过期拒绝及临时SQLite隔离分页；均为合成场景 |
| Direct资料及跨主体拒绝 | 局部通过 | 新版真实登录/context/0条资料及刷新、一次断开清理和重连通过；非空资料、退出登录第二轮与跨主体/撤销待验 |
| 签名发布 / 其他桌面系统 | 未验 | sidecar为已有开发产物，manifest仍记dirty输入；macOS仅ad-hoc签名，不能称正式签名/公证或跨平台验收 |

初次真实系统检查时间：2026-09-06 09:16 CST；后续部署检查单独记录，不沿用本次时间。sidecar SHA-256：`e8d05b9c6cba3a59651a8c98679a26ecb1739ab6e6b8badb3b1a894884c263f2`；SDK发布manifest SHA-256：`8174505ddf815a2d6795101ea4cc6241c7b96df8ea9f070d66617b0ea140f37c`。系统存储测试只在临时目录写合成记录，结束后清理；未读取用户资料库。

## 复现

从知君工程根目录执行：

```sh
rtk proxy node frontend/shell/scripts/prepare-real.cjs
rtk proxy node frontend/shell/scripts/verify-os-storage.cjs
rtk proxy node frontend/shell/scripts/verify-real-environment.cjs
rtk proxy bash start-desktop.sh --real
```

原生sidecar检查使用SDK现有独立脚本，从SDK根目录执行：

```sh
rtk proxy node scripts/verify-electron-sidecar-native.mjs --manifest release/electron-sidecars-1.2.0/manifest.json
```

准备脚本首次从相邻SDK复制产物，之后核验并复用本工程 `data/desktop/sidecar/` 副本，不要求相邻仓库持续存在。固定哈希直接来自本次核验结果，不信任相邻manifest临时改写的哈希。自动准备暂限macOS/Linux，实际验收平台为macOS ARM64；Windows权限与启动未验。已有显式配置环境变量时 `--real` 使用它；否则生成默认配置。若已生成内容被修改，拒绝覆盖，需使用另一份显式配置。目录及其祖先不接受软链接，已有二进制还须可执行。`--real` 是正式适配器的开发验收入口，不代表签名发布或完整真机通过。

本轮独立审核发现并修复已有二进制执行权限、输出目录祖先软链接及显式配置优先级问题。在隔离临时目录用真实SDK产物验证：生成成功、不可执行拒绝、软链接祖先在写入前拒绝、自定义配置保留；所有检查通过。测试没有对远程资料作修改。

自动配置阶段本地验证：宿主64项测试通过；桌面构建通过（12模块），正式登录页可用；文档本地链接及JSON/差异格式检查通过。已额外验证相邻SDK目录不存在时仍能复用已核验的本地sidecar。后续用户已登录，根代理通过界面确认两台在线授权设备；本次D03测试和交付物见[签名桥记录](BUSINESS-BRIDGE-0906.md)。

## 后续必需条件

新版登录、家中AMD盒同一SDK context及空资料响应已通过UI链路；刷新无错误，一次断开后资料区消失、重新选盒后就绪并返回0条也已通过。SSH认证、旧main和未部署已不再是阻塞。最终必须在关闭local-debug的环境完成资料读取、断开后拒绝及跨账号/设备用例，才能将 `realDeviceValidated` 改为true。新bridge scope不自动迁移旧global资料；空列表通过不能作为历史资料迁移或有数据读取的证据。

首次D03本地结果（历史）：宿主73项、Electron界面4项、DE相关101项及Agent全套/race通过；Desktop构建和4段Mermaid通过。Linux AMD64/ARM64 Agent部署输入已构建，12项文件哈希与ELF架构核对一致，重复运行构建器拒绝覆盖。OS `0a004c9`、DE `f6b1089` 已推送对应D03分支；该阶段未执行远程部署或读取真实资料；后续部署结果如下。

## 家中盒端v1部署增量与当时状态

2026-09-06 10:57:23 CST 已完成正式部署：Agent `0a004c9` active、Gateway connected、授权快照新鲜；DE 保留并发新发布 `6b549ad`，运行文件对应 `dev/zhijun-business-bridge-0906-live` / `c16dc17be81240285820b9e86076877911d153c4`。DE 保持 debug=0；health 200，无签名 context/materials 401，伪造格式及错误签名 401；两个服务 NRestarts=0。实际 release、并发发布处理、权限修复、6文件哈希和回退步骤见[盒端部署记录](BOX-DEPLOYMENT-0906.md)。

新增验证与早期 DE 101 项分开计：盒端 Python 3.14 环境使用临时合成数据通过 55 项桥测试及实际 `server.app` 中间件链路 5 项检查；新 live 分支在本机隔离环境通过 8 个模块共 111 项测试及 6 个 subtests，复现命令见[D03记录](BUSINESS-BRIDGE-0906.md#当前-live-分支回归复现)。这些正向是隔离合成请求，不是正式SDK用户请求。

用户已完成新版登录并提供连接家中AMD盒、0条资料的截图。随后主代理通过CUA检查真实 `zhijun://desktop/desktop.html` 窗口，点击刷新后无错误仍0条；断开后回选盒页且资料区消失；再次选择AMD盒后连接就绪并返回0条。此为真实SDK/context/空资料UI链路和一次断开重连证据。**`M0-R=false`、`realDeviceValidated=false`**：尚未验证非空资料、退出后重新登录的第二轮、跨账号/设备及撤销矩阵。新 scope 合法空页不证明历史资料迁移或非空资料读取。

新可复现部署输入位于忽略目录 `data/desktop/bridge-release-0906-live/`，12 项哈希、ELF 架构及6运行文件比对通过，manifest SHA-256 为 `ae24b1f7b11242caaecfae153f3b83c7cca441c9d631d3d7d34c6641c7e4de25`。新包的 `manifest.deployed=false` 表示该包本身未执行安装；现场已部署的是经合并核对的同内容运行文件，应用清单仅补 context，未整包覆盖。

## 完整产品v2与FD热修后续状态

OS `5f5f4c9`与Admin `44a0950`已提交推送；Gateway完整166项本地回归、8项审核修复及consent/取消/idle worker增量复验已完成。上述结果均不填入本记录的真实用户通过项目。隔离硬件脚本最终通过，逐项结果为：PDF46字符、DOCX48字符、OCR110字符；voice API40字符、0资料、两个指定短语均匹配。最终hardware-candidate5为5/5，safe material正文82、摘要43字符、实体2、关系0；gateway-candidate6为10/10，60请求/21个completed操作，知识CRUD/confirm/search/purge通过。均为合成主体/输入，非正式Consumer/UI；首次导入/FD/evidence_invalid失败与后续修复保留于审核报告。后续Agent/DE/worker/catalog已完成正式匹配部署；Admin生产发布入口未提供，正式Consumer/SDK/P2P/UI与实际麦克风验收仍待完成。

DE `132b97d`只修复connectivity.db连接泄漏并已单独部署；新PID3144495四次30秒样本总FD52/55/52/52，connectivity FD0/0/0/0，HTTP200，NRestarts0，见[热修报告](../reports/CONNECTIVITY-FD-HOTFIX-0906.md)。该线上恢复不改变原M0-R未完整验收，也不代表新领域/大文件/模型/语音已通过。完整业务状态统一看[执行计划](FULL-PRODUCT-INTEGRATION-0906.md)和[完整验收清单](FULL-PRODUCT-ACCEPTANCE-0906.md)。

### 配额复验与硬件证据分层

shell最终117项Node与vue-tsc通过，独立15项配额验证已包含在117内。真实JS模块+内存严格Agent配额+虚拟时钟下，200MiB/400块在243秒虚拟时间完成，滚动60秒最多101请求；这不是实际SDK/P2P吞吐或时延证据。资料相关最新189项回归包含后续有界纠错范围，既有172项是历史批次，均不与上述硬件结果相加为UI通过数。实际麦克风、正式模型授权及Consumer/UI/SDK流程仍需补验；详见[硬件报告](../reports/FULL-PRODUCT-HARDWARE-0906.md)。

## v2正式盒端匹配部署（验收未关闭）

知君clean head `735e341`（代码`58dac31`）、DE`015c659`与OS`5f5f4c9`已匹配发布，363个文件与已验candidate源码一致。已备份16个SQLite和旧Agent二进制/manifest/90/95配置；现场manifest保留原2个应用，仅增加zhijun-desktop。release为`/home/user/apps/centuarai-data-engine/releases/20260906T051053Z-zhijun-015c659`。

DE PID3184701、Agent PID3184868 active，live8618健康200、未签名v2 context401，来源强制enforce、debug=0。这一轮是正式运行服务的部署和基础检查，前述5/5与10/10则来自隔离合成主体测试，两者不合并为Consumer/UI全功能验收。Admin`44a0950`仍未生产部署且缺发布入口；正式Consumer/SDK/P2P/UI和实际麦克风继续pending。具体哈希、备份和稳定性证据见[部署回执](../reports/FULL-PRODUCT-DEPLOYMENT-0906.md)及[原始receipt](../reports/evidence/deployment-receipt.json)。

## 2026-09-08 公司盒章程在线模型复验

用户在 `AMD-A2A-248` 的章程对话中选择“在线理解”与 `deepseek-v4-flash`。原请求在 1024 token 输出预算下运行约 21 秒后返回 `MODEL_RESPONSE_EMPTY`：流正常结束，但没有可展示正文；这不是盒子断连、Admin 授权或 Provider 403。

知君提交 `493fda8` 将明确章程意图的聊天输出预算提高为 4096 token，并保持普通聊天 1024 token；能力适配器同时为 `MODEL_RESPONSE_EMPTY` 返回“没有可显示正文、原消息和草稿仍保留”的受控提示。相关命令分别通过 30 项（含 5 subtests）和 123 项后端回归。

修复部署到 `/srv/zhijun-integration-0907/release-493fda8-charter/zhijun`，原容器保留为 `zhijun-integration-0907-rollback-20260908-model-empty`。新容器健康状态为 `healthy`。桌面重新登录、连接公司盒、打开原失败会话并点击“重试当前模式”，在一次性来源授权后得到可见回复。盒端审计记录：`state=complete`、`provider=openai`、`model=deepseek-v4-flash`、`taskContext=charter`、`max_tokens=4096`、输入/输出 1847/220 token、耗时 9565 ms。回复只提出一个必要问题；章程工作稿继续标记“尚未生效”，没有自动发布。

本次关闭的是该在线章程对话的空正文故障。章程建议合并、发布、修订冲突以及 170 项完整产品矩阵仍按完整验收清单分别执行，不能由这一条真机通过替代。
