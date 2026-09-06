# D03 盒端部署与验收记录

日期：2026-09-06。客户端修复 `c7a9148`；Agent `0a004c9`；桥原始DE源码 `f6b1089`，测试权限修复 `a74d06e`；当前服务基线 `6b549ad` 上的合并提交 `c16dc17be81240285820b9e86076877911d153c4` 已推送 `dev/zhijun-business-bridge-0906-live`。本文不保存密码、私钥、令牌或资料正文。

## 执行计划与验收标准

- [x] 只读确认实际架构、设备身份、服务用户/配置/源码/数据路径、版本与补丁兼容性。
- [x] 保存程序/源码/配置备份，部署最小D03增量及独立Ed25519密钥；不迁移旧global资料。
- [x] 使用盒端实际依赖和临时数据完成55项桥测试，以及真实server.app中间件的5项合成正反向检查。
- [x] 验证运行服务健康、debug关闭、握手路由注册、未签名/伪造签名拒绝和源码哈希。
- [x] 新版知君真实登录，通过SDK验证context、空资料页GET、刷新、一次断开清理及重连。
- [ ] 完成第二轮完整登录退出、跨账号/设备、撤销/过期及非空资料归属验收。
- [x] 完成文档/代码提交推送及远程版本核对（提交记录与远程分支为交付依据）。

依赖：SSH与服务管理权限已具备；新版知君登录与家中盒子的Direct空资料读取已验，其他账号/设备及完整生命周期矩阵仍待验。主代理独占所有SSH变更，OS代理只读审核部署前提，DE代理在独立目录合并候选并验证源码差异。只有真实已授权SDK请求可以完成正向真机验收；健康检查、合成数据或空列表均不能证明历史资料归属已完成。

影响范围：Agent 可执行文件、应用清单和桥密钥配置；DE 的 `backend/server.py`、`mindos/{agent_bridge,bridge_materials,api_contracts,uploads}.py`、`mindos/stores/connectivity_store.py`；桌面已有 D03 客户端重新构建启动。本次仓库交付另更新部署输入脚本、版本基线、架构/集成/任务/验收文档。验收先核对源码与实际进程，再检查正式拒绝路径，最后才执行需要用户登录的 SDK 正向链路。

## 实际部署

| 项目 | 生效位置或结果 |
| --- | --- |
| 目标 | 用户指定的家中盒，Linux x86_64；与知君列表中的“AMD AI盒子”身份匹配 |
| Agent服务 | system `centauros-remote-agent.service`；服务用户 `centauros-remote-agent` |
| Agent二进制 | `/usr/lib/centauros/centauros-remote-agent`，Linux AMD64 SHA256 `7d21207a7dd54b4771546f382c4db82b378e87c2c5e064f62ead6c425f6fa476` |
| Agent配置 | `/etc/centauros/remote-agent.yaml`；新增独立桥密钥路径，应用清单指向 `remote-agent-applications.zhijun-0906.yaml` |
| 应用清单 | 从线上清单复制，仅给PC资料目标增加精确GET context；其余原有应用/路由保持 |
| 私钥 | `/var/lib/centauros/remote-agent/mindos-bridge-private.pem`；盒内openssl生成的PKCS8 Ed25519，服务用户所有、0600 |
| 公钥 | `/etc/centauros/mindos-bridge/public.pem`；root所有、0644，父目录0755；DE服务用户可读 |
| DE服务 | user `centaurAI-database.service`；用户 `user`，监听 `127.0.0.1:8618` |
| DE实际release | `/home/user/apps/centuarai-data-engine/releases/20260906T024325Z-6b549ad/backend` |
| DE环境增量 | `95-zhijun-business-bridge.conf` 设置独立公钥、实际Agent identity中的deviceId和 `MINDOS_LOCAL_WEB_DEBUG_ACCESS=0` |
| 持久资料目录 | `/home/user/centaurAI-database`；仅检查相关SQLite schema，未迁移或复制业务记录 |
| 最终DE安装时间 | 2026-09-06 10:57:23 CST；6个运行文件均与已验证合并候选哈希一致 |

首次候选按检查时运行的 `20260906T021541Z` 合并。准备期间，另一部署任务于10:47:28更新了90号release drop-in，指向 `20260906T024325Z-6b549ad`。首次重启后的context 404暴露了版本不一致；未将其记录为成功。已恢复不再运行的旧release源文件，重新合并当前release并部署。第二次在停止、启动前后核对90号drop-in哈希、NeedDaemonReload、WorkingDirectory、MainPID实际cwd及6个源码哈希，确认补丁在运行版本生效。

新release包含新的隐私中间件、错误边界、远程session权限、owner/scope分页与上传功能。三方合并保留这些行为；移除D03增量后，server/uploads/api_contracts的AST与线上原文件一致。桥仅开放独立hash scope的最小资料列表，不将原material_service已有资料导入该scope；今后迁移非空资料前还须对齐新版本的隐私与生命周期规则。

## 验证结果与边界

| 检查 | 结果 | 能证明的范围 |
| --- | --- | --- |
| SSH与系统检查 | 通过 | 用户提供的认证可用；未在仓库或配置保存密码 |
| Agent候选配置 | 通过 | 以实际服务用户和credentials环境执行validate-config；另验证桥私钥可读，不能仅凭validate-config认定握手成功 |
| 盒端55项桥测试 | 通过 | Python3.14、cryptography49、FastAPI0.138.1实际环境，临时数据及公开合成向量 |
| 当前DE合并回归 | 111项及6个subtests通过 | 独立worktree基于6b549ad，umask002，8个模块；包含正式server.app中间件回归，6个运行文件与盒端候选字节一致 |
| 默认umask差异 | 已修复测试 | 合法fixture显式0644，umask002下55项通过；未放宽运行时安全权限要求 |
| 完整应用中间件5项 | 通过 | server.app真实路由与中间件下，未签名、无效证明、有效合成context、重放拒绝及空资料列表；使用临时库与合成身份 |
| 生产HTTP健康 | HTTP200 | 服务已启动 |
| 生产context路由 | 已注册；未签名HTTP401 | 新代码已经生效，访问未被debug放行 |
| 生产资料路由 | 未签名HTTP401 | 正式资料gate关闭未授权访问 |
| 生产伪造证明 | HTTP401 BRIDGE_HEADER_INVALID | 格式无效证明拒绝 |
| 生产错误密钥证明 | HTTP401 BRIDGE_SIGNATURE_INVALID | 正式服务成功读取受信公钥并拒绝合成测试密钥，未读取生产私钥 |
| 稳定性/版本 | Agent与DE running，NRestarts=0；授权快照持续更新；NeedDaemonReload=no | 部署检查时服务稳定，实际代码/配置一致 |
| 新版桌面 | 真实登录/握手/空资料页通过 | 用户截图与主代理CUA核对正式入口，已连接到家中盒；刷新无报错，返回总数0 |
| 真实断开/重连 | 一次通过 | 断开后显示授权选盒页，资料区消失；重连同一盒后恢复ready和空资料页，未退出账号 |
| 完整M0-R | 未通过 | 空资料页与一次重连已通过；完整退出登录循环、跨账号/设备、撤销/过期与历史资料归属仍未完成 |

第一次生成公钥目录受root的umask077影响成为0700，独立可读性检查发现DE用户不可遍历；已显式改为root所有0755，公钥保持0644，私钥仍0600。此后错误签名探针返回BRIDGE_SIGNATURE_INVALID，确认正式公钥读取路径可用。原始密码和私钥内容从未作为诊断输出。

## 备份与回退

备份根：`/var/backups/centauros/mindos-bridge-20260906T025300Z`，root私有目录；其中时间后缀为此次备份标识，实际安装时间以上表及receipt为准。

- 根目录保存Agent旧二进制、原配置/清单、最初release源文件及drop-in快照；最初release源文件已恢复。
- `current-release-6b549ad/`保存最终生效release的4个原有文件备份及2个新增文件的缺失基线、release drop-in快照、输入哈希和deployment receipt。
- 回退前先核对当前运行release和源码哈希，避免覆盖之后的部署。停止DE，恢复该release的4个原有文件，移除本次新增的2个桥模块；恢复Agent旧二进制与配置后重启。新增清单和桥密钥在旧配置下不使用。
- 不删除持久数据库、nonce表或用户资料。未经重新验收不恢复debug访问；保留95号drop-in的debug=0即可让旧服务继续拒绝未授权业务请求。若撤销桥的环境配置，只移除桥公钥/设备新增项，保留原有设备配置并核对来源。

用户随后在新版知君完成登录，提供“连接已就绪、共0条”截图。主代理通过CUA确认真实 `zhijun://desktop/desktop.html` 正式入口，执行资料刷新、断开、重连“AMD AI盒子”：选盒态资料区消失，重连后恢复已连接和0条资料。结合入口中只允许context验证成功后ready、只允许成功资料响应后展示页码的实现，本次已取得实际SDK只读空页及一次重连证据。未读取非空业务正文，未保存账号标识或原始截图到仓库，也未将空页视为历史迁移成功。

当前还缺完整退出再登录循环、跨主体和撤销/过期矩阵。用户反馈的导航缺失另属于桌面仍使用独立资料验收入口的问题，原产品主布局/路由尚未接入，不能将本次授权桥修复算作完整产品迁移完成。

## 可复现输入

当前源码分支已推送；部署输入脚本默认读取 `zhijun-bridge-os` 与 `zhijun-bridge-data-engine-current` 两个干净worktree，生成新的 `data/desktop/bridge-release-0906-live`，不覆盖旧包。12项文件哈希、两个Linux ELF架构及6个DE运行文件与实际候选的一致性均核验通过。清单SHA256：`ae24b1f7b11242caaecfae153f3b83c7cca441c9d631d3d7d34c6641c7e4de25`。

本地包是后续重现使用的部署输入，包本身未执行整包安装、未签名OTA；实际盒端只合并必要配置和6个匹配运行文件。`integration-release-baseline.json` 分别记录部署事实与输入包状态，不能把 `bundle.deployed=false` 理解成盒端桥仍未部署。
