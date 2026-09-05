# 2026-09-05 上游版本同步与审核

## 目标与版本

- 来源：`finewood2008/centaurAI-zhijun` 的 GitHub `main`。
- 上次源基线：`67161ed46296d7299e26cca522594d9a29c2bd82`。
- 本次固定源提交：`22dc9a3112058f06a1e4a385c1b2dc3175e39476`。
- 新增提交：`6235b40`（持续事项与可编辑成果）、`22dc9a3`（聊天发送稳定性与来源校验）。
- 目标工作分支：`dev/first-integrate-check-0905`；远程为本工程 Codeup `origin`。

## 执行计划与验收条件

1. [x] 确认目标远程分支、保留未提交架构文档，读取上游增量。
2. [x] 获取固定提交归档，按 GitHub tree 的 blob 哈希核验源码；以旧基线计算增量，保留目标仓库历史、忽略规则、权限及文档。
3. [x] 独立审核后端事项/成果/来源授权，前端发送恢复/新增页面，以及开发启动与测试隔离；记录并处理阻断问题。
4. [x] 运行前端类型检查/构建/全部单元测试、受影响后端模块及新增隔离测试，按变更需要运行新增浏览器 E2E。
5. [x] 更新全部受影响的架构、集成、审核、迁移、API/功能与运行文档及图示；保留历史记录的版本语义。
6. 提交前完成差异、源码完整性和文档检查；本记录随同步提交推送至下述目标分支，最终提交号与推送结果以 Git 远程记录为准。

受影响代码以两次上游提交的文件清单为准，集中于 `backend/mindos/matters_routes.py`、`stores/matters_store.py`、routing/context、Vue 事项工作区与聊天发送恢复、`scripts/dev_runtime.py`、`scripts/run_tests.py` 和相关测试。主代理负责同步、总体文档与最终集成；独立代理分别审核后端/运行设施、更新 5 份领域文档，以及修复 MatterWorkspace 和对应测试，写入文件不重叠。

依赖与边界：仅同步产品上游，本轮不实施 Electron SDK 或 data-engine 集成；相邻仓库只读复核。测试必须使用隔离数据/密钥目录，不能接触用户运行库。验收以已固定源码、适当回归、明确的未通过项、更新后的文档和远程分支一致为准，不把新增功能自动视为已经适配 SDK。

## 执行结果

GitHub Git HTTPS 连接超时后，使用官方 API 确认 `main`，从 codeload 下载固定提交归档，并逐文件校验完整 Git tree；同步末尾再次查询 `main`，仍为 `22dc9a3`。

- 源受管理文件从 684 增至 **715**，715 个 blob 哈希全部匹配 GitHub tree，未缺文件。
- 增量为 **76 个文件：45 修改、31 新增**；本地源 checkout 未改动，也未引入源 Git 历史。
- 归档 SHA-256：`07019689852a484de386590ae351f8d4848c87ebee94b8d00f5952fd2d96a9ff`。
- 唯一与既有代码调整重叠的源文件为 `.gitignore`，三方合并无冲突，增加 `/backups/` 且保留 `/secrets/` 和产物忽略规则。
- 保留 Codeup 历史、`.aliyun`、既有启动脚本权限和之前未提交的集成文档；本机依赖、运行数据、密钥、构建文件及新测试截图不入库。
- 新代码除下述文稿保护修复及其测试外与固定上游一致；其余内容差异为文档更新和已有迁移调整。`zhijun.sh` 保留源文件模式，通过 `bash zhijun.sh` 使用。

## 业务与架构变化

1. **持续事项和成果。** 新增 12 个方法/路径组合，支持事项列表/编辑、独立会话绑定、从完整回复保存文稿、编辑和历史；四张 work_* 表位于 ontology.db，不自动写为个人 Claim，也不自动授权模型使用。
2. **上下文和来源。** 增加 matter/artifact 来源、绑定版本、暂停/换题/显式恢复；编辑文稿保留原消息及祖先来源。展示清理引用标记不改变原始授权快照。
3. **发送恢复。** 新 chatStream 编排指定 409 的有限重预览，保留 requestId 与输入来源；来源变化、服务器错误、断网、已开始的流不自动重发。停止也覆盖发送前预览/授权阶段。
4. **页面和运行。** 首页显示推进中的事项，本体新增个人摘要，文稿可复制/下载；新增后端连接状态提示、只读恢复、开发 supervisor 和模块隔离测试入口。

## 审核发现与处理

| 状态 | 问题与触发条件 | 处理 / 后续验收 |
| --- | --- | --- |
| **已修复** | 编辑文稿 A 未保存，关闭抽屉后从回复 B 再次进入并保存，saveReply 会覆盖 A 的输入 | saveReply 在 documentDirty 时阻止操作并提示保存/放弃；真实组件测试先在原代码失败，再验证修复通过、无保存B请求、A正文保留、保存A后可保存B |
| 待修：重试合同 | 保存成果未传 title，首次成功后事项标题或原回复变化，相同 body/requestId 重试可能 409 | fingerprint 含可变标题/正文，见 matters_routes/create_artifact 与 MattersStore.save_artifact；后续改用稳定原请求指纹和安全结果回放，不用新 requestId 掩盖问题 |
| 集成前必须处理 | 事项/成果/历史未分页，历史包含每版正文；累计可能超过 SDK 16 MiB 响应上限 | 定义分页、正文单独读取与预算；补大历史、并行读取和超限测试 |
| 产品合同待定 | 旧本体 purge 不清 work_*，删除原对话后成果和历史仍可读；新接口无 DELETE | 明确保留、独立删除、备份/恢复和幂等记录处置；原来源失效仍禁止不安全模型复用，不能将其描述成清除了副本 |
| 平台边界已说明 | dev_runtime 依赖 fcntl/POSIX/lsof，测试含 `/private/tmp`；不是 Windows 通用入口 | 当前只验证 macOS；Linux 修正路径假设并补平台检查，Windows沿用已有入口；本次不更改上游平台逻辑 |
| 隔离操作已落实 | conftest 不覆盖所有独立存储环境，开发入口还写真实项目 data/run/dev | 本次测试强制临时 secret store 并清独立metadata/gbrain/MCP覆盖；未运行默认开发 supervisor；运行文档补准确示例 |

本次新增功能未发现明确的新跨设备越权路径，但不代表完整安全审计通过。旧 SDK 身份桥、目录隔离、owner/device 归属、Direct-only、上传协议与 sidecar 来源缺口仍按 [集成方案](INTEGRATION-0905.md) 处理。

## 本次验证结果

| 检查 | 结果 |
| --- | --- |
| 前端 `npm run build`（含 vue-tsc） | 通过；本地文稿修复后再次构建通过。保留 taskRouting 静态/动态混合导入的非阻断打包提示 |
| `node --experimental-strip-types --test tests/*.test.mjs` | **37 个测试文件全通过**；文稿保护新增用例在未修代码上失败、修复后该文件再次通过 |
| 后端 `scripts/run_tests.py --isolated-modules … -- -q --tb=short` | **36/36 模块通过，JUnit 522 项，0 failure/error/skipped**；含事项、来源快照、上下文、routing、表达辅助、开发入口、隔离runner及知君核心模块，非140模块全量 |
| `chat-send-recovery.e2e.mjs` + 专用 fixture 8775 | 通过：真实隔离API、有限409恢复、500与来源变化保留输入/来源、候选撤销、390px交互、无真实模型传输 |
| `matters.e2e.mjs` + 专用 fixture 8774 | 通过：只读打开、创建/绑定、准备但不发送、完整文稿/编辑/刷新/下载、迟到响应隔离、390px与首页继续、无模型调用 |
| `today-layout.e2e.mjs` | 1440 / 1060 / 820 / 390 / 320px 全通过，不连接后端 |
| Web / shell 边界 | `check-web-no-electron.sh`、`bash -n zhijun.sh` 通过 |
| 架构图 | 4 张 Mermaid 重新渲染；SVG 从新版目标图导出并视觉检查 |
| 文档与差异 | 13 份 Markdown 的 168 处本地链接/行号有效，代码块闭合、无尾随空白，SVG XML 有效；提交前 Git 差异检查通过 |

测试服务仅绑定 loopback，启动前检查端口空闲，校验专用 fixture 身份，结束后回收本次子进程。数据/密钥/截图均为临时目录；未启动正常用户数据上的应用，未连接真实账号、盒子或模型。原首次迁移 E2E 的自动确认断言问题此次未重跑，历史记录不改写为已通过。

## 文档覆盖

- 总览与基线：README、[迁移记录](MIGRATION.md)、本文。
- 架构与集成：[架构及 SVG](ARCHITECTURE-0905.md)、[集成方案](INTEGRATION-0905.md)、[原审核及更新复核](REVIEW-0905.md)。
- 领域合同：[API](zhijun-api-contract.md)、[对话管理](conversation-management.md)、[任务路由](task-routing.md)、[个人上下文](personal-context-plan.md)、[表达辅助](reply-assistance.md)。
- 运行与上游记录：[本机运行](local-runtime.md)、[上游工作流验收来源说明](executive-workflow-20260905.md)。

共更新/新增 13 份受影响 Markdown 文档与 1 张 SVG。未涉及的历史 PRD、原型与部署比较文档保留原版本含义；上游记录的 140 模块/2345项等数量不当作当前工程测试结论。

## 远程交付

目标为 `origin/dev/first-integrate-check-0905`，继续保留当前集成分支。同步开始时该远程分支与本地代码基线 `4515a7e` 一致，无已有分支变更需要解决；推送前再检查远程。未指定发布或主分支合并，因此不改 `master`、不发布 SDK、不部署服务。最终提交以此记录所在提交及远程分支为准。
