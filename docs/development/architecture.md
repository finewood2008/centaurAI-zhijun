# 知君当前架构

## 组件

```text
frontend/mindos-web/              Vue 3 产品界面、SSE 客户端和桌面 transport
frontend/shell/                   Electron 宿主、登录/连接、权限与打包
frontend/shared/                  Renderer、preload、main 之间的公开合同
backend/mindos/                   Web API、资料能力与知君领域服务
backend/mindos/zhijun/            对话、上下文、模型、抽取、路由和回执
backend/mindos/stores/            SQLite 业务存储
backend/zhijun_worker/            盒端独立 workspace 领域进程
```

浏览器开发模式直接访问本机 FastAPI。桌面模式不允许 Renderer 自行访问网络：业务请求经 preload 的窄接口进入 Electron main，再通过安装的 Connectivity/Consumer SDK 到盒端 Gateway。Gateway 将知君领域操作交给独立 worker，将资料和模型基础能力交给 Data Engine。

## 已上线外部依赖

Admin 已正式发布上线，是当前生产连接链路的账号、应用登记与授权控制面。`zhijun-desktop` 以 `zhijun.workspace` 用途申请 `remote.p2p` 权限；Electron main 通过安装的 Connectivity/Consumer SDK 获取受限票据并建立设备会话，Renderer 不直接访问 Admin。

Consumer 合同中“不允许调用旧 Admin 接口”描述的是客户端隔离边界，不表示 Admin 服务未上线：生产客户端只能使用面向 Consumer 的窄接口和 SDK，不能绕过它们访问管理面。Admin、桌面安装包、盒端 Gateway/Data Engine、知君 worker 和 CentaurOS NPU 运行时是独立发布单元，状态不能互相替代。

## 数据边界

- 会话、消息和轮次回执存放在会话库。
- 用户理解、证据、来源授权和路由审计存放在本体相关存储。
- 章程、判断、结果与复盘有独立增长数据存储。
- 正式盒端按账号、设备、workspace 和所有权代次隔离；开发数据路径不能当作正式部署路径。
- Renderer 只能获得界面所需的投影，不持有账号密码、长期令牌或任意文件系统能力。

## 模型路由

对话先读取当前会话的权威路由状态，再准备来源预览和授权，最后提交消息。在线请求不会因为失败自动转为本地。用户要求本地时，实际本地 provider 由盒端配置决定；在 NPU-only 部署中，CentaurOS 必须禁用 CPU 服务和 CPU fallback。

判断某轮实际通道应读取消息回执中的 `provider`、`model` 和 `external`，或盒端路由审计。界面选择状态和回复正文中的模型自称都不是执行证据。

## 安全不变量

- 所有跨设备操作使用有限操作目录和参数校验，不提供任意 HTTP、Shell 或文件路径代理。
- 凭据只通过窄登录接口进入 main；持久化凭据使用系统安全存储，Renderer 不可读。
- 在线外发必须同时满足服务可用、路由模式、来源版本、用途和授权检查。
- 文件原件不因文本授权自动外发；来源失效、撤回或版本改变后不能复用旧授权。
- 连接、workspace 或窗口代次变化后，迟到结果不能写回新状态。
- 正式配网在缺少受信配置时 fail closed。

## 权威来源

- 产品 HTTP/SSE 路由：`backend/mindos/*routes.py`、`backend/mindos/conversations.py`。
- 桌面公开类型：`frontend/shared/desktop-contract.ts` 与产品操作目录。
- Electron 构建、签名和验证：`frontend/shell/package.json`、`electron-builder*.yml`、`scripts/prepare-macos-package.cjs`。
- Consumer 合同：`development/consumer-api-contract/`。
- 旧 v1 bridge 的兼容基线与生成器保留在 `frontend/shell/scripts/`，不属于当前架构或 CentaurOS 部署依据。

历史部署版本、单次验收数量和构建哈希不属于架构合同，通过 Git 历史或发布产物追溯。
