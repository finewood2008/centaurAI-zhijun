# Admin 上线验证与 Data Engine master 合并

## 任务与验收条件

用户确认 Admin 已上线，要求真实桌面验证；并要求将 `CentuarAI-web-20260904` 和两次列出的 `dev/zhijun-business-bridge-0906-live` 合入 master。重复项已询问，在答复前只处理明确的两个不同分支。

- 实际桌面重新选择家庭 AMD AI 盒子，验证账号票据、Direct、工作区 context 与页面加载；区分源码合并、服务部署和真实业务结果。
- 核对新远端引用与手工合并状态；在独立 worktree 解决冲突，保留用户原工作区未提交内容。
- 合并后 master 包含两个指定来源完整提交历史，保留 master 原桌面行为及 live 盒端能力；不用整仓 ours/theirs 覆盖。
- 运行受影响前后端和连接合同回归、必要构建；记录环境或外部服务限制。
- 推送 master，确认远端提交一致；更新本报告与连接验收文档。

## 已确认依赖与受影响范围

- 目标仓库 `nexusaos-data-engine`：origin/master `236e100`，web 分支 `5c45a44`，live 分支 `015c659`。web 为 live 祖先。
- 原工作区位于 `dev/integrate-sdk-20260902`，有用户未提交前后端改动，没有未完成的 Git 合并；这些改动不进入本次分支合并。
- 独立工作区 `.worktrees/zhijun-master-0906`，集成分支 `merge/zhijun-master-0906` 从 origin/master 创建。
- 预期范围：Data Engine 后端 mindos/连接网关、旧桌面前端及 shell、相关测试和文档；具体冲突通过三方差异确定。
- Admin origin/master 已更新至 `2ca211e`；是否包含应用登记由独立复核确认，是否实际可用由桌面链路验证。

## 分工与步骤

主代理负责实际桌面、合并编辑、冲突决策、集成验证与推送。独立代理分别只读核对 Admin 登记、分支/冲突语义、测试范围；并行代理不改代码。

发现冲突后细分独立文件归属：前端代理处理 `frontend/mindos-web/**`，后端代理处理 `backend/**`，主代理处理 scripts 与合并提交。实际 Admin 验证已越过应用授权，新的 `BUSINESS_BRIDGE_REQUIRED` 无 HTTP 状态码、DE 无相应 context 请求；已确认 Agent `HandleOffer` 只给旧应用填入 owner，新 v2 session 为空，随后签名装饰必然拒绝。连接代理改独立 OS 工作树的 `remote-agent/internal/p2p/manager.go` 及连接生命周期回归；主代理负责候选核验、备份更新家庭盒子和真实复测，保留同一授权快照校验。

- [x] 获取最新引用，检查原工作区与分支关系。
- [x] 创建隔离合并工作区与验收计划。
- [x] 实际 Admin → SDK → AMD 工作区连接、资料页面验证。
- [x] 解决 master 合并冲突并复核语义。
- [x] 必要测试与构建。
- [x] 推送集成分支并创建远端合并请求 #11。
- [ ] 推送并核对远端 master。
- [x] 更新验收结果与遗留事项。
- [ ] 任务回收修复生效后的真实本体页面与重连复验。

## Admin 上线与真实连接结果

Admin 远端 master `2ca211e` 已完整包含应用登记提交 `44a0950`，两者源码树相同：`1aa7ecb749bc733bde8de840b815b46de4589e79`。固定策略为 `zhijun-desktop / zhijun.workspace / remote.p2p`，Electron、Direct-only、30 分钟票据，未借用旧只读应用权限。

通过用户授权账号在实际 Electron 登录并选择家庭 **AMD AI盒子**，应用授权已不再返回 `APPLICATION_AUTHORIZATION_DENIED`。随后遇到的 `BUSINESS_BRIDGE_REQUIRED` 来自盒端 Agent：新应用的 P2P 会话未保存 Owner，而 v2 工作区签名装饰要求会话 Owner 与当前业务主体一致，导致请求尚未到达 DE 就被拒绝。

修复只将已批准的新应用加入 Owner 绑定条件，Owner 仍来自同一受信任业务快照。未接受客户端指定 Owner，未取消主体变化、签名或应用白名单校验。OS 分支 `dev/zhijun-business-bridge-0906` 提交 **`644b1c2` 已推送并部署到家庭盒子**。

真实 UI 结果：账号登录、在线设备列表、票据、Direct、v2 context 均已通过，界面显示“已连接”，主导航正常。引导页跳过后进入对话空状态；`/data` 与 `/materials` 实际工作区页面加载成功，当前资料为 0。没有通过填写个人本体、上传个人文件或发送真实对话来扩大本次验证范围。

## Agent 更新与回退回执

| 项目 | 结果 |
| --- | --- |
| Agent 源码 | `644b1c2`；通过真实 `Manager.Offer` 生命周期回归，旧代码可复现 Owner 缺失；v1/v2、Owner 变化拒绝、签名缺失拒绝与其他应用边界通过 |
| Go 检查 | 短临时目录下 `go test ./...` 通过，新增回归 race 检查通过；macOS 默认长临时目录不适合 Unix socket 用例 |
| 新 Linux amd64 二进制 SHA256 | `55299dd3f84ff4045c14dd6a43562c4ea29efa2b508a2eed13486c3d0f231fd7` |
| 原二进制 SHA256 | `1b44da2a688b5acd64121ad55c14b178968de85bef89a223858d009725e6c574` |
| 盒端备份目录 | `/home/user/apps/centuarai-data-engine/patch-backups/20260906-agent-owner-644b1c2`，保留 `.previous` 与 `.candidate` |
| 安装方式 | 校验候选与现有 SHA，保留原件，原子替换 `/usr/lib/centauros/centauros-remote-agent`，仅重启 Agent |
| Agent 状态 | active，PID 3498896，NRestarts 0 |
| 应用 manifest | 未修改；SHA256 `41a54e50b26df4e3baac153ab2f47aa65e3f7fce0d3b8e5c86921d6c7834d552` |
| DE 状态 | 原 `015c659` release 不变，PID 3184701，active，NRestarts 0，盒内 health HTTP 200 |

回退时先确认仍运行本候选 SHA，将该目录的 `centauros-remote-agent.previous` 以 root/0755 原子恢复到同一安装位置，重启 Agent 并检查 health/连接。不回滚 DE 数据或授权配置。本次没有读取私钥、记录密码/票据或操作麦克风。

## 页面复验发现与修复

连接成功后，`/me` 初次加载仍显示“读取请求过多，请稍后重试”。复现表明是主进程本地任务历史占位：完成 11 个任务后，同时开始 3 个读取，旧 `trimJobs()` 未将 `pendingStarts` 计入回收条件，因此仅第一个读取被接受，即使传输空闲也会拒绝后两个。

`frontend/shell/runtime/product-session.cjs` 现按与准入一致的 `jobs.size + pendingStarts` 回收已结束历史。仍限制 12 个活跃任务、8 个 native 请求并发和 600ms 间隔；取消未确认、结果未知的写入继续占位，不能自动重放。

新增回归挂起三个远端 ACK，验证 11 个终态历史后全部三次读取均派发，并验证第 13 个活跃任务仍零派发拒绝。任务与配额测试 **34/34**，整个 Shell 测试 **128/128，无跳过**。此修复无需部署 DE，需重启实际 Electron 后再次访问本体页面。

最终 UI 复验暂未完成：CUA 先连续两次浏览器连接超时，随后 Chrome 与 Electron 均返回 `cgWindowNotFound`。已请求用户恢复可交互桌面，没有把单元测试写成修复后真机页面通过。

## Data Engine 合并交付

两个来源在隔离工作区完成三方冲突解决，保留 master 的 PC 宿主、窄 API 与分页权限语义，接入 live 的受控媒体、资料、上传、删除协调及工作区能力。合并提交 **`770da8f95f91b5c06a105044601f2964e1973725`** 已推送到 **`merge/zhijun-master-0906`**，本地 `master` 已在隔离 worktree 快进至该提交；原开发工作区与其未提交修改保持原样。

直接更新 master 被服务端保护规则拒绝：“权限被拒绝：不允许推送该分支”。使用云效支持的推送评审流程成功创建 [合并请求 #11](https://codeup.aliyun.com/667637d76d5f7b05cc5d9ed7/nexusaos/nexusaos-data-engine/change/11)，评审 head 与已验证的集成分支完全一致。远端 master 当前仍为 **`236e100`**；尚未宣称合并完成，待桌面恢复后完成正常网页合并流程。

合并验证：核心后端 **377 passed**；广域后端 **2329 passed + 26 subtests**；运行包/数据根 **9 passed**；Web/Desktop 构建、26 个相关前端测试文件、10 个 Chromium 场景、宿主 `test:connectivity` 与 `desktop:verify` 均通过。

广域后端剩余 16 个失败及 1 个收集错误全部因仓库外三份正式合同/夹具缺失；15 个跳过为 Windows 用例及已退役旧索引机制。没有伪造文件或改弱安全校验令全套变绿。完整来源 SHA、冲突决策与限制见 Data Engine 分支中的 `docs/development/MASTER-MERGE-0906.md`。

本次未把 Data Engine 合并候选自动部署到家庭盒子，也未完成正式签名、公证安装包与 Dock 外观验收。
