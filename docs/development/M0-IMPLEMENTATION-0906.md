# 桌面 M0 实施记录

日期：2026-09-06。起始提交：`ee8cd96`；分支：`dev/first-integrate-check-0905`。

> 后续状态已更新：本文的“当前交付”和“下一步”均为 `3d9679b` 阶段历史。正式认证及 D03 家中盒端部署已在后续完成；新版桌面真实登录/context/空资料页及一次断开/重连已通过，非空资料与跨主体验收仍未完成，`M0-R=false`。最新事实见[盒端部署记录](BOX-DEPLOYMENT-0906.md)，不回写本阶段历史测试数量。

> 本文记录 `3d9679b` 的 M0-L 历史交付；后续正式认证与实际SDK装配见 [正式接入记录](M0-PRODUCTION-0906.md)。

## 本轮计划

- [x] 核对 shell、启动器、Vue 构建及 M0 合同，确认工作区干净。
- [x] 核查 SDK/sidecar 与正式身份桥的可用输入，记录开发版本。
- [x] 实现独立宿主、安全 preload/IPC、状态与代次、资料校验/调度/取消。
- [x] 实现独立 Vue 桌面入口与模拟只读资料流程；默认正式配置未就绪时拒绝连接。
- [x] 完成单元、构建边界与真实 Electron 隔离端到端验证，修复审核问题。
- [x] 更新任务状态、启动说明和合同差异。
- [x] 提交交付代码和文档至当前分支；远程同步结果由提交后的 Git 校验确认。

## 范围与依赖

本轮交付 M0-L：独立桌面宿主和前端、可注入适配器、显式模拟身份/设备/资料，以及正式配置未就绪的拒绝分支。覆盖 BASE-01/05 的本地子交付及 DESK-01/02/03/09/10/11/12/13/14；真实认证、业务桥和盒子验证按源码核查结果单独记录，不将模拟成功算成 M0-R。

拟影响 `frontend/shell/`、`frontend/mindos-web/` 的独立桌面入口、共享类型、根桌面启动器及相关检查/文档。既有 Web 入口仍单独构建；不启动 Python、不读取运行库或借用其他应用登录态。前端和策略、状态实现各自维护独立文件，主代理负责宿主与最终装配。

## 验收条件

1. 无本机业务服务时能打开独立 UI；不轮询或回退 8618，不带默认 `--no-sandbox`。
2. 默认显示正式配置未就绪；模拟需显式启用且持续可见标记，仅含合成数据。
3. preload 仅公开 v1 窄接口，主进程校验精确 sender/frame/URL、操作及参数，禁止任意网络、身份头或路径输入。
4. 连接仅在业务桥通过后 ready；登录/连接/退出竞态和旧代次结果不能恢复旧状态。
5. 资料包含 queued，校验 256 KiB 原始 body、分页与字段，剔除目录/正文等字段；2在途/8排队有界调度。
6. 取消只结束本地投递，Promise仅结算一次；实际在途完成前不释放槽，不为单读关闭整场会话。
7. 实际 Electron 中完成模拟登录→选盒→分页/筛选→断开/退出；覆盖缺配置、错误IPC、迟到回包与资源关闭。

验证只使用新建临时目录及合成身份/资料。记录实际命令、结果、版本和未验范围；需要真实配置的任务保持未验。

## 实际交付

当前可运行 **M0-L 本地桌面流程**，未接入真实 SDK 或调用真实 data-engine。宿主默认 `unconfigured`，只有开发模式显式 `--simulation` 才提供合成账号、两台设备及每台47条资料；打包模式禁止模拟。页面持续显示环境，未知模式不会开放正式连接。

| 模块 | 实际文件 | 行为 |
| --- | --- | --- |
| 启动与宿主 | [启动器](../../start-desktop.sh)、[main.js](../../frontend/shell/main.js)、[launcher](../../frontend/shell/launch.cjs) | 单实例、独立构建、Electron 37.10.3，不启动 Python 或轮询8618；关闭时有界清理 |
| 安全边界 | [preload](../../frontend/shell/preload.cjs)、[security](../../frontend/shell/security.cjs) | 窄 IPC、精确窗口/主frame/URL 校验；sandbox/contextIsolation；固定资源协议、CSP、导航/权限/外部网络拒绝 |
| 状态与适配器 | [runtime](../../frontend/shell/runtime/desktop-runtime.cjs)、[adapters](../../frontend/shell/runtime/adapters.cjs) | 登录、设备列表、连接、桥校验、断开和退出；generation/sequence 防旧结果覆盖；可注入端口 |
| 资料策略 | [materials](../../frontend/shell/runtime/materials.cjs)、[scheduler](../../frontend/shell/runtime/read-scheduler.cjs)、[public-error](../../frontend/shell/runtime/public-error.cjs) | 受控 GET 映射、256 KiB 原始响应限制、五种状态、最小字段投影；2在途/8排队、去重、取消与安全错误 |
| 独立页面 | [DesktopApp](../../frontend/mindos-web/src/desktop/DesktopApp.vue)、[controller](../../frontend/mindos-web/src/desktop/controller.ts)、[桌面构建](../../frontend/mindos-web/vite.desktop.config.ts) | 登录、选盒、每页20条、关键词/类型/状态筛选、刷新/取消、断开/退出；无旧路由或 API 回退 |
| 共享合同与构建检查 | [唯一类型源](../../frontend/shared/desktop-contract.ts)、[构建边界](../../frontend/mindos-web/build/boundaries.ts) | 文档类型重新导出生产类型；Web/桌面实际模块图检查，防止混入 Electron/SDK 或旧业务入口 |

新增环境字段和操作代次语义已同步[接口合同](DESKTOP-CONTRACT-0905.md)。`Result.generation` 属于操作，当前状态以最新快照为准。超时/取消仅结束本地投递，真实未完成的适配器工作仍占额度，不宣称远端执行已停止。

## 启动和验证

仓库根执行；首次需要安装两份依赖：

```sh
rtk proxy npm --prefix frontend/mindos-web ci
rtk proxy npm --prefix frontend/shell ci
rtk proxy bash start-desktop.sh --simulation
```

不带 `--simulation` 启动则显示“正式连接尚未配置”。模拟页已在真实 Electron 中检查：登录→选盒A→20/20/7条分页→queued筛选→断开→选盒B→退出，资料和身份按代次清理。

| 实际验证命令（除注明外在仓库根，2026-09-06） | 结果与证据 |
| --- | --- |
| `rtk proxy npm --prefix frontend/shell test` | 45项通过：资料7、调度8、runtime27、安全3；见 [shell/tests](../../frontend/shell/tests) |
| 在 `frontend/mindos-web`：`rtk proxy node --experimental-strip-types --test tests/*.test.mjs` | Node runner 47项通过：37个既有文件级项目、10个新增桌面 controller 用例；不是47个测试文件 |
| `rtk proxy npm --prefix frontend/mindos-web run build` | 类型检查与 Web 构建通过，2064模块；保留既有 taskRouting 静态/动态导入提示，不影响产物 |
| `rtk proxy npm --prefix frontend/mindos-web run build:desktop` | 类型检查与独立桌面构建通过，12模块；两个构建均执行实际模块边界插件 |
| `rtk proxy npm --prefix frontend/shell run test:e2e` | 真实 Electron 的3个模拟/拒绝流程通过；[用例](../../frontend/shell/tests/electron.e2e.cjs) 使用临时 userData 并回收自己创建的宿主 |
| `rtk proxy bash scripts/check-web-no-electron.sh` | Web源代码边界和启动链检查通过 |
| `rtk proxy bash -n start-desktop.sh` | 启动脚本语法通过 |

验证平台是 macOS ARM64；Node 23.11.0、Electron 37.10.3、TypeScript 5.9.3、Vite 5.4.21、Playwright 1.62.1。精确相邻仓库提交及归档哈希见[基线 JSON](integration-release-baseline.json)。

文档接口重新导出通过严格 TypeScript 检查；架构4段 Mermaid 全部解析并生成 SVG，第3节现状图已更新，另3图源码未变，目标架构 SVG 继续对应第5节。修改文档的本地链接、源码行号范围、代码围栏及 `git diff --check` 均通过。

审核修复了退出登录后紧接断开可能跳过身份清理的竞态，并补入先失败后通过的回归；同时修复 Promise 以 null/undefined/false/0 拒绝时误判成功。还覆盖迟到登录/连接、错误桥 binding、悬挂 close、超时后额度不提前释放、错误 IPC 和资源目录穿越/符号链接。

E2E 的 pageerror/request 监听在首窗口取得后建立，只证明该观察区间内没有未处理页面错误与外部请求；实现中的网络拦截在创建窗口前注册。未测试签名安装包、其他平台、真实凭据存储、网络断连恢复或 sidecar 残留；没有以模拟 session.close 代替真机进程回收证据。

## M0-01–10 证据与边界

| 合同用例 | 本轮证据 | 正式验收缺口 |
| --- | --- | --- |
| M0-01、03 | runtime 验证桥拒绝/错误 binding；默认缺配置拒绝，非法设备不连接 | 真实账号/应用授权、撤销及部署桥 |
| M0-02 | 合成资料47条；分页、queued、字段剔除经过真实 IPC/策略 | 正式身份与盒端响应、账号/设备数据范围 |
| M0-04、05 | 状态机及 controller 覆盖切设备、乱序、退出和迟到认证/连接 | Consumer refresh coordinator 与真实凭据生命周期 |
| M0-06、07 | 安全单元验证 sender/frame/URL；实际 IPC 非法参数拒绝；body预算/字段/编码测试 | 已发布 SDK/Agent/后端四层一致合同 |
| M0-08、09 | 排队/在途取消、去重、代次、超时、额度与错误拒绝 | 真实 Direct 失败、服务端额度与实际执行完成 |
| M0-10 | 临时 Electron 宿主退出；模拟资源清理、有界 close/dispose、迟到 session 回收 | sidecar真实进程、OS安全凭据、安装包升级重启 |

## 正式集成下一步

1. **D02 / BASE-02 / DESK-04–06**：落实独立应用注册、登录合同、受信 Consumer/Gateway/JWKS 配置、scope/purpose 和 OS 凭据 namespace，接入认证与设备 adapter。
2. **D03 / BASE-03 / 服务端桥任务**：交付 Agent 到 data-engine 的可信主体传递、应用路径授权、TTL/撤销及归属合同。现有 `X-MindOS-Session` 不在 Agent 外部头白名单中；不能复用 PC 登录态或直接添加信任头绕过。
3. **BASE-04 / D05 / DESK-07–08**：冻结服务端资料归属/有界投影和可重建 SDK/sidecar/Agent/后端组合，再接入真实 native host 与退出回收。SDK包虽可编译且归档匹配，sidecar manifest仍含旧dirty输入，data-engine工作区也有未提交内容，不能当作正式发布基线。
4. **DESK-15 / M0-R**：以获授权测试账号和真实盒子执行跨主体拒绝、真实资料读取和进程/凭据回收。之后才推进聊天、事项、上传等 M1/M2。

相邻仓库只作只读核查，本轮未修改它们、未启动业务服务、未读取运行数据库。任务级状态见[开发任务总表](DEVELOPMENT-TASKS-0905.md)，正式待办不能因为本地流程通过而关闭。
