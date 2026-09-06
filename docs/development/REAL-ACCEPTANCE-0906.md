# 知君自动配置与真机验收

日期：2026-09-06。自动配置起点：`689bb11`；D03修复起点：`0550383`。

## 计划与验收标准

- [x] 核查 Admin、Agent、data-engine 三端登记及 SDK 产物，区分目标应用 ID 和登录客户端 ID。
- [x] 将受信服务地址、PC 资料应用绑定及当前平台 sidecar 自动写入本地配置；固定哈希，不写入密码或令牌。
- [x] 验证真实 macOS safeStorage、原生 sidecar、HTTPS 服务和目标设备可达性，结果逐项记录。
- [x] 启动正式桌面并保留应用内登录入口。
- [x] 使用知君自身客户端登录并检查授权设备，看到两台在线已授权盒子。
- [x] 编码桌面/Agent/data-engine签名桥，完成本地正反向合同测试。
- [ ] 在真实盒部署业务桥，验证 Direct 资料读取、断开和跨主体拒绝。
- [x] 更新架构、集成和任务状态，整理可复现脚本与脱敏证据；提交/推送状态由最终Git结果确认。

影响文件：`frontend/shell/config/`、`frontend/shell/scripts/`、`frontend/shell/production/`、`start-desktop.sh` 及集成文档；Agent/data-engine在各自独立worktree开发，原data-engine的用户修改保持不动。生成配置和二进制放在已忽略的 `data/desktop/`，未修改远程业务数据或部署。

依赖：现有 Consumer 账号及授权设备、SDK 平台产物、系统钥匙串，以及 Agent → data-engine 可信业务身份桥。真实登录必须由用户在知君窗口输入；不读取别的应用的登录态。验收通过必须包含已授权资料读取，只有网络可达、原生进程或 UI 启动不能关闭 M0-R。

## 参数来源

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

## 初次真实检查

2026-09-06 09:13 CST：本机 macOS 26.5.2 / ARM64；家中 `192.168.1.18:22` TCP 可达，办公盒 `192.168.31.248:22` 超时；家中 SSH 密钥认证被拒。使用系统信任链检查 Admin 和 Gateway，未禁用 TLS：Gateway live/ready HTTP 200；Admin 未认证设备请求 HTTP 200、业务 `code=401`、`success=false`，没有获得设备数据。

## 业务授权差额

用户截图中的报错来自旧main未注入真实业务桥。现已实现[D03逐请求签名桥](BUSINESS-BRIDGE-0906.md)：Agent使用独立盒内密钥签名，data-engine验证后返回业务上下文；桌面通过同一SDK session核对主体后才ready。新代码和目标盒端尚未联合部署，当前保留登录态的窗口仍运行旧main，不能声称截图故障已在真机消失。Admin P2P票据仍不等于data-engine专用JWT，没有通过放宽guard处理。

## 本次结果

| 项目 | 结果 | 实际边界 |
| --- | --- | --- |
| 自动配置 | 通过 | 完整JSON通过正式loader；固定PC目标参数；二进制复制后哈希一致，重复运行不覆盖配置 |
| macOS系统存储 | 通过，4组 | Electron 37.10.3真实safeStorage；临时生成的合成身份/令牌完成加密、重载、ECDSA签名、明文扫描及本地令牌删除；未登录真实账号 |
| macOS原生sidecar | 通过，6项 | 真正执行ARM64二进制，版本1.2.0/protocol1、哈希、缺配置/未知会话/错误协议拒绝及关闭幂等；没有发送connect |
| 云服务和设备网络 | 部分通过 | Admin/Gateway在线、未认证请求拒绝；家中22端口可达，办公盒超时；TCP可达不能证明设备Owner授权 |
| SSH部署检查 | 未通过 | 家中盒已有主机指纹匹配，但本机默认公钥认证拒绝；未获得远程shell，未变更服务 |
| 知君登录与授权设备 | 通过 | 用户在知君输入账号；实际UI确认已登录，并显示两台在线已授权设备；未记录手机号、令牌或资料正文 |
| D03本地代码验证 | 通过 | Go/Python共享签名向量、重放/错主体/路径/过期拒绝及临时SQLite隔离分页；均为合成场景 |
| Direct资料及跨主体拒绝 | 未完成 | D03已编码，等待盒端部署和重启新版客户端；未通过真实SDK读取资料 |
| 签名发布 / 其他桌面系统 | 未验 | sidecar为已有开发产物，manifest仍记dirty输入；macOS仅ad-hoc签名，不能称正式签名/公证或跨平台验收 |

真实系统检查时间：2026-09-06 09:16 CST。sidecar SHA-256：`e8d05b9c6cba3a59651a8c98679a26ecb1739ab6e6b8badb3b1a894884c263f2`；SDK发布manifest SHA-256：`8174505ddf815a2d6795101ea4cc6241c7b96df8ea9f070d66617b0ea140f37c`。系统存储测试只在临时目录写合成记录，结束后清理；未读取用户资料库。

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

登录和设备列表已验证；完成全部资料验收还需在盒端部署D03，并重启新版知君（按现有安全存储合同需重新登录）。当前SSH认证不可用，尚无法检查或部署盒端变更，已请用户提供现有可用的SSH入口。最终必须在关闭local-debug的环境完成资料读取、断开后拒绝及跨账号/设备用例，才能将 `realDeviceValidated` 改为true。新bridge scope不自动迁移旧global资料；空列表通过不能作为历史资料迁移或有数据读取的证据。

本次D03最终本地结果：宿主73项、Electron界面4项、DE相关101项及Agent全套/race通过；Desktop构建和4段Mermaid通过。Linux AMD64/ARM64 Agent部署输入已构建，12项文件哈希与ELF架构核对一致，重复运行构建器拒绝覆盖。OS `0a004c9`、DE `f6b1089` 已推送对应D03分支；没有执行远程部署或读取真实资料。
